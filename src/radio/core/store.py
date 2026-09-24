"""
Capa de persistencia SQLite de Radio Parra (§3.2 de ARCHITECTURE.md).

- Solo ``sqlite3`` de la stdlib. WAL y claves foráneas activadas.
- Versión de esquema en ``PRAGMA user_version`` (actual: ``SCHEMA_VERSION``).
  Una BD de desarrollo anterior (tabla ``plays``, ``user_version = 0``) se rechaza
  con un error claro: no hay datos que conservar, basta con borrarla.
- La API devuelve modelos tipados (``radio.core.models``), nunca filas crudas.
- Fechas: se exigen *aware* y se guardan como ISO 8601 normalizado a UTC con
  microsegundos (``2026-01-05T09:00:00.000000+00:00``), de modo que la comparación
  de texto en SQL coincide con la cronológica. Se leen como datetimes UTC.
- Escritura atómica (§3.3): ``add_segment`` solo inserta la fila; el audio ya debe
  estar en su ruta final (ver ``radio.core.paths.commit_audio``).
- Cada método confirma su propia escritura, salvo dentro de ``transaction()``, que
  agrupa varias en una sola (p. ej. segmento + ``state_delta`` de ficción, §4.2).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from radio.core.models import (
    INBOX_STATUSES,
    STATUSES,
    InboxItem,
    InboxStatus,
    PlayLogEntry,
    ProducerRun,
    Segment,
    SignalReading,
    Status,
    StockView,
)

SCHEMA_VERSION = 1

# Esquema §3.2, tal cual
_SCHEMA_V1 = """
CREATE TABLE segments (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, factual INTEGER NOT NULL,
  status TEXT NOT NULL, path TEXT NOT NULL, duration_s REAL NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT, priority INTEGER DEFAULT 0,
  parent_id TEXT REFERENCES segments(id), voice_id TEXT,
  producer TEXT NOT NULL, prompt_version TEXT, summary TEXT, meta_json TEXT
);
CREATE INDEX idx_segments_kind_status ON segments(kind, status);

CREATE TABLE play_log (
  id INTEGER PRIMARY KEY, segment_id TEXT REFERENCES segments(id),
  kind TEXT NOT NULL, mode TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, skipped INTEGER DEFAULT 0
);

CREATE TABLE producer_runs (
  id INTEGER PRIMARY KEY, producer TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, ok INTEGER,
  n_segments INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  tts_chars INTEGER, cost_eur REAL, error TEXT
);

CREATE TABLE universe_state (
  universe TEXT PRIMARY KEY, version INTEGER NOT NULL,
  state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE signals (
  source TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, at TEXT NOT NULL
);

CREATE TABLE inbox (
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# Claves de meta admitidas en find_by_meta (evita inyección en la ruta JSON)
_META_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class LegacySchemaError(RuntimeError):
    """La BD es de un esquema anterior incompatible (desarrollo previo a §3.2)."""


class StaleUniverseState(RuntimeError):
    """La versión esperada de ``universe_state`` no coincide (escritura concurrente)."""


# ── Conversión de fechas ──────────────────────────────────────────────────────

def _ts(dt: datetime) -> str:
    """datetime aware → ISO 8601 en UTC con microsegundos (comparable como texto)."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"Se requiere una fecha con zona horaria (aware): {dt!r}")
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _ts_opt(dt: datetime | None) -> str | None:
    return None if dt is None else _ts(dt)


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_opt(value: str | None) -> datetime | None:
    return None if value is None else _parse(value)


def _row_to_segment(row: sqlite3.Row) -> Segment:
    meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
    return Segment(
        id=row["id"],
        kind=row["kind"],
        factual=bool(row["factual"]),
        path=Path(row["path"]),
        duration_s=float(row["duration_s"]),
        created_at=_parse(row["created_at"]),
        producer=row["producer"],
        status=cast(Status, row["status"]),
        expires_at=_parse_opt(row["expires_at"]),
        priority=int(row["priority"] or 0),
        parent_id=row["parent_id"],
        voice_id=row["voice_id"],
        prompt_version=row["prompt_version"],
        summary=row["summary"],
        meta=meta,
    )


def _row_to_run(row: sqlite3.Row) -> ProducerRun:
    return ProducerRun(
        id=int(row["id"]),
        producer=row["producer"],
        started_at=_parse(row["started_at"]),
        ended_at=_parse_opt(row["ended_at"]),
        ok=None if row["ok"] is None else bool(row["ok"]),
        n_segments=int(row["n_segments"] or 0),
        tokens_in=int(row["tokens_in"] or 0),
        tokens_out=int(row["tokens_out"] or 0),
        tts_chars=int(row["tts_chars"] or 0),
        cost_eur=float(row["cost_eur"] or 0.0),
        error=row["error"],
    )


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"Status no válido: {status!r} (válidos: {sorted(STATUSES)})")


_SEGMENT_ORDER = " ORDER BY created_at ASC, id ASC"


# ── Clase principal ───────────────────────────────────────────────────────────

class DB:
    """
    Acceso a la base de datos SQLite de Radio Parra.
    Al abrir activa WAL y claves foráneas y crea o valida el esquema.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def _migrate(self) -> None:
        """Crea el esquema v1 en una BD vacía o valida la versión existente."""
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"La BD {self._path} tiene esquema v{version}, más nuevo que el del código "
                f"(v{SCHEMA_VERSION}). Actualiza Radio Parra."
            )
        tables = {
            r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if tables:
            legacy = " (con la tabla antigua plays)" if "plays" in tables else ""
            raise LegacySchemaError(
                f"La BD {self._path} es de un esquema antiguo de desarrollo sin versión"
                f"{legacy}. No contiene datos que merezca la pena migrar: bórrala (junto "
                "con sus ficheros -wal y -shm) y vuelve a arrancar para crear el esquema nuevo."
            )
        try:
            self._conn.executescript(
                "BEGIN;" + _SCHEMA_V1 + f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
            )
        except BaseException:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> DB:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # ── Transacciones ─────────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """
        Agrupa varias escrituras en una sola transacción (todo o nada).
        Se puede anidar: solo la más externa confirma o deshace.
        """
        self._tx_depth += 1
        try:
            yield
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.rollback()
            raise
        self._tx_depth -= 1
        if self._tx_depth == 0:
            self._conn.commit()

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self._conn.commit()

    def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """Ejecuta una escritura; fuera de transacción, deshace si falla y confirma si no."""
        try:
            cur = self._conn.execute(sql, tuple(params))
        except BaseException:
            if self._tx_depth == 0:
                self._conn.rollback()
            raise
        self._commit()
        return cur

    # ── Segmentos ─────────────────────────────────────────────────────────────

    def add_segment(self, seg: Segment) -> None:
        """
        Inserta un segmento. ``seg.path`` debe ser la ruta final del audio,
        ya completa (escritura atómica, §3.3).
        """
        _check_status(seg.status)
        self._write(
            """
            INSERT INTO segments
              (id, kind, factual, status, path, duration_s, created_at, expires_at,
               priority, parent_id, voice_id, producer, prompt_version, summary, meta_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                seg.id, seg.kind, int(seg.factual), seg.status, str(seg.path),
                float(seg.duration_s), _ts(seg.created_at), _ts_opt(seg.expires_at),
                seg.priority, seg.parent_id, seg.voice_id, seg.producer,
                seg.prompt_version, seg.summary,
                json.dumps(seg.meta, ensure_ascii=False, sort_keys=True),
            ),
        )

    def get_segment(self, id: str) -> Segment | None:
        """Segmento por id, o None si no existe."""
        row = self._conn.execute("SELECT * FROM segments WHERE id = ?", (id,)).fetchone()
        return _row_to_segment(row) if row else None

    def list_segments(
        self, kind: str | None = None, status: Status | None = None
    ) -> list[Segment]:
        """Segmentos (filtros opcionales), en orden de creación (created_at, id)."""
        query = "SELECT * FROM segments WHERE 1=1"
        params: list[Any] = []
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        rows = self._conn.execute(query + _SEGMENT_ORDER, params).fetchall()
        return [_row_to_segment(r) for r in rows]

    def update_segment_status(self, id: str, status: Status) -> None:
        """Cambia el status de un segmento. KeyError si no existe."""
        _check_status(status)
        cur = self._write("UPDATE segments SET status = ? WHERE id = ?", (status, id))
        if cur.rowcount == 0:
            raise KeyError(f"No existe el segmento {id!r}")

    def update_segment_meta(self, id: str, meta: dict[str, Any]) -> None:
        """Sustituye ``meta`` de un segmento (p. ej. medida de loudness). KeyError si no existe."""
        cur = self._write(
            "UPDATE segments SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False, sort_keys=True), id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"No existe el segmento {id!r}")

    def find_by_meta(
        self,
        kind: str,
        key: str,
        value: str | int | float,
        *,
        status: Status | None = None,
    ) -> Segment | None:
        """
        Primer segmento (por created_at, id) de ``kind`` cuyo ``meta[key]`` vale
        ``value``; si ``meta[key]`` es una lista, basta con que contenga ``value``
        (sirve para ``guid`` y para etiquetas como ``tags`` = ``hour:...``).
        """
        if not _META_KEY_RE.fullmatch(key):
            raise ValueError(f"Clave de meta no válida: {key!r}")
        query = """
            SELECT * FROM segments
            WHERE kind = ?
              AND EXISTS (SELECT 1 FROM json_each(segments.meta_json, ?) AS j
                          WHERE j.value = ?)
        """
        params: list[Any] = [kind, f"$.{key}", value]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        row = self._conn.execute(query + _SEGMENT_ORDER + " LIMIT 1", params).fetchone()
        return _row_to_segment(row) if row else None

    def find_by_path(self, path: Path | str) -> Segment | None:
        """Segmento cuyo audio está en ``path`` (comparación exacta), o None."""
        row = self._conn.execute(
            "SELECT * FROM segments WHERE path = ?" + _SEGMENT_ORDER + " LIMIT 1",
            (str(path),),
        ).fetchone()
        return _row_to_segment(row) if row else None

    def stock_view(self, now: datetime) -> StockView:
        """Stock emitible en ``now``: status ready y (sin caducidad o caduca después)."""
        rows = self._conn.execute(
            "SELECT * FROM segments WHERE status = 'ready'"
            " AND (expires_at IS NULL OR expires_at > ?)" + _SEGMENT_ORDER,
            (_ts(now),),
        ).fetchall()
        return StockView.from_segments((_row_to_segment(r) for r in rows), now)

    def status_counts(self) -> dict[str, dict[str, int]]:
        """Número de segmentos por kind y status: ``{kind: {status: n}}``."""
        rows = self._conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM segments GROUP BY kind, status"
            " ORDER BY kind, status"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
        return out

    def next_expirations(self, now: datetime, limit: int = 10) -> list[Segment]:
        """Segmentos ready que caducarán después de ``now``, los más próximos primero."""
        rows = self._conn.execute(
            "SELECT * FROM segments WHERE status = 'ready' AND expires_at > ?"
            " ORDER BY expires_at ASC, id ASC LIMIT ?",
            (_ts(now), limit),
        ).fetchall()
        return [_row_to_segment(r) for r in rows]

    def expire_segments(self, now: datetime, kind: str | None = None) -> int:
        """Pasa a ``expired`` los ready con ``expires_at <= now``. Devuelve cuántos."""
        query = "UPDATE segments SET status = 'expired' WHERE status = 'ready' AND expires_at <= ?"
        params: list[Any] = [_ts(now)]
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        return int(self._write(query, params).rowcount)

    def pick_ready(
        self,
        kind: str,
        *,
        now: datetime,
        exclude_tags: Iterable[str] = (),
        max_duration_s: float | None = None,
    ) -> Segment | None:
        """
        Elige un segmento emitible de ``kind`` (ready y sin caducar en ``now``):
        primero los nunca emitidos, luego el emitido hace más tiempo (según
        ``play_log``); desempata por created_at y después id.
        ``exclude_tags`` descarta los que tengan alguna de esas etiquetas en
        ``meta["tags"]``; ``max_duration_s`` descarta los más largos.
        """
        query = """
            SELECT s.*, MAX(p.started_at) AS last_played_at
            FROM segments s LEFT JOIN play_log p ON p.segment_id = s.id
            WHERE s.kind = ? AND s.status = 'ready'
              AND (s.expires_at IS NULL OR s.expires_at > ?)
        """
        params: list[Any] = [kind, _ts(now)]
        if max_duration_s is not None:
            query += " AND s.duration_s <= ?"
            params.append(max_duration_s)
        query += """
            GROUP BY s.id
            ORDER BY last_played_at IS NOT NULL, last_played_at ASC,
                     s.created_at ASC, s.id ASC
        """
        excluded = set(exclude_tags)
        for row in self._conn.execute(query, params):
            seg = _row_to_segment(row)
            if excluded.isdisjoint(seg.tags):
                return seg
        return None

    # ── play_log ──────────────────────────────────────────────────────────────

    def log_play_start(
        self, segment_id: str | None, kind: str, mode: str, started_at: datetime
    ) -> int:
        """Abre una emisión en ``play_log``. Devuelve su id."""
        cur = self._write(
            "INSERT INTO play_log (segment_id, kind, mode, started_at) VALUES (?,?,?,?)",
            (segment_id, kind, mode, _ts(started_at)),
        )
        return int(cur.lastrowid or 0)

    def log_play_end(self, id: int, ended_at: datetime, skipped: bool = False) -> None:
        """Cierra una emisión con su hora real de fin (y si se saltó/cortó)."""
        cur = self._write(
            "UPDATE play_log SET ended_at = ?, skipped = ? WHERE id = ?",
            (_ts(ended_at), int(skipped), id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"No existe la emisión {id}")

    def list_play_log(self, since: datetime | None = None) -> list[PlayLogEntry]:
        """Emisiones (más antigua primero); ``since`` filtra por started_at >= since."""
        query = """
            SELECT p.*, s.duration_s AS seg_duration_s
            FROM play_log p LEFT JOIN segments s ON s.id = p.segment_id
        """
        params: list[Any] = []
        if since is not None:
            query += " WHERE p.started_at >= ?"
            params.append(_ts(since))
        query += " ORDER BY p.started_at ASC, p.id ASC"
        return [
            PlayLogEntry(
                id=int(r["id"]),
                segment_id=r["segment_id"],
                kind=r["kind"],
                mode=r["mode"],
                started_at=_parse(r["started_at"]),
                ended_at=_parse_opt(r["ended_at"]),
                skipped=bool(r["skipped"]),
                duration_s=None if r["seg_duration_s"] is None else float(r["seg_duration_s"]),
            )
            for r in self._conn.execute(query, params)
        ]

    # ── producer_runs ─────────────────────────────────────────────────────────

    def start_producer_run(self, producer: str, started_at: datetime) -> int:
        """Abre una ejecución de ``producer`` (``ok`` queda NULL). Devuelve su id."""
        cur = self._write(
            "INSERT INTO producer_runs (producer, started_at) VALUES (?, ?)",
            (producer, _ts(started_at)),
        )
        return int(cur.lastrowid or 0)

    def finish_producer_run(
        self,
        id: int,
        *,
        ended_at: datetime,
        ok: bool,
        n_segments: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        tts_chars: int = 0,
        cost_eur: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Cierra una ejecución con su resultado y su consumo."""
        cur = self._write(
            """
            UPDATE producer_runs
            SET ended_at = ?, ok = ?, n_segments = ?, tokens_in = ?, tokens_out = ?,
                tts_chars = ?, cost_eur = ?, error = ?
            WHERE id = ?
            """,
            (_ts(ended_at), int(ok), n_segments, tokens_in, tokens_out,
             tts_chars, float(cost_eur), error, id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"No existe la ejecución {id}")

    def last_producer_run(self, producer: str) -> ProducerRun | None:
        """Última ejecución de ``producer`` (por started_at, id), o None."""
        row = self._conn.execute(
            "SELECT * FROM producer_runs WHERE producer = ?"
            " ORDER BY started_at DESC, id DESC LIMIT 1",
            (producer,),
        ).fetchone()
        return _row_to_run(row) if row else None

    def list_producer_runs(self, producer: str | None = None) -> list[ProducerRun]:
        """Ejecuciones (más antigua primero), opcionalmente de un solo productor."""
        query = "SELECT * FROM producer_runs"
        params: list[Any] = []
        if producer is not None:
            query += " WHERE producer = ?"
            params.append(producer)
        query += " ORDER BY started_at ASC, id ASC"
        return [_row_to_run(r) for r in self._conn.execute(query, params)]

    def month_cost_eur(self, month_start: datetime) -> float:
        """Gasto acumulado (€) de las ejecuciones empezadas desde ``month_start`` (§4.2)."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_eur), 0.0) FROM producer_runs WHERE started_at >= ?",
            (_ts(month_start),),
        ).fetchone()
        return float(row[0])

    # ── universe_state ────────────────────────────────────────────────────────

    def get_universe_state(self, universe: str) -> tuple[int, dict[str, Any]] | None:
        """``(version, state)`` del universo, o None si aún no tiene estado."""
        row = self._conn.execute(
            "SELECT version, state_json FROM universe_state WHERE universe = ?", (universe,)
        ).fetchone()
        if row is None:
            return None
        return int(row["version"]), cast(dict[str, Any], json.loads(row["state_json"]))

    def put_universe_state(
        self,
        universe: str,
        state: dict[str, Any],
        *,
        expected_version: int | None,
        updated_at: datetime,
    ) -> int:
        """
        Guarda el estado con control optimista: ``expected_version`` debe ser la
        versión leída (None si el universo aún no existía). Devuelve la nueva
        versión; lanza ``StaleUniverseState`` si otro proceso escribió antes.
        """
        state_json = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if expected_version is None:
            try:
                self._write(
                    "INSERT INTO universe_state (universe, version, state_json, updated_at)"
                    " VALUES (?, 1, ?, ?)",
                    (universe, state_json, _ts(updated_at)),
                )
            except sqlite3.IntegrityError as exc:
                raise StaleUniverseState(f"El universo {universe!r} ya tiene estado") from exc
            return 1
        cur = self._write(
            "UPDATE universe_state SET version = version + 1, state_json = ?, updated_at = ?"
            " WHERE universe = ? AND version = ?",
            (state_json, _ts(updated_at), universe, expected_version),
        )
        if cur.rowcount == 0:
            raise StaleUniverseState(
                f"Versión de {universe!r} distinta de la esperada ({expected_version})"
            )
        return expected_version + 1

    # ── signals ───────────────────────────────────────────────────────────────

    def add_signal(self, source: str, key: str, value: str, at: datetime) -> None:
        """Registra una lectura (sensor, etc.)."""
        self._write(
            "INSERT INTO signals (source, key, value, at) VALUES (?,?,?,?)",
            (source, key, value, _ts(at)),
        )

    def latest_signal(self, source: str, key: str) -> SignalReading | None:
        """Última lectura de ``source``/``key`` (por ``at``), o None."""
        row = self._conn.execute(
            "SELECT * FROM signals WHERE source = ? AND key = ?"
            " ORDER BY at DESC, rowid DESC LIMIT 1",
            (source, key),
        ).fetchone()
        if row is None:
            return None
        return SignalReading(
            source=row["source"], key=row["key"], value=row["value"], at=_parse(row["at"])
        )

    # ── inbox ─────────────────────────────────────────────────────────────────

    def add_inbox(
        self,
        channel: str,
        sender: str,
        payload: str,
        *,
        created_at: datetime | None = None,
    ) -> int:
        """Añade un mensaje con status ``pending`` (nada se emite sin aprobar)."""
        cur = self._write(
            "INSERT INTO inbox (channel, sender, payload, status, created_at)"
            " VALUES (?,?,?,'pending',?)",
            (channel, sender, payload, _ts(created_at or datetime.now(UTC))),
        )
        return int(cur.lastrowid or 0)

    def list_inbox(self, status: InboxStatus | None = None) -> list[InboxItem]:
        """Mensajes (más antiguo primero), opcionalmente filtrados por status."""
        query = "SELECT * FROM inbox"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC, id ASC"
        return [
            InboxItem(
                id=int(r["id"]),
                channel=r["channel"],
                sender=r["sender"],
                payload=r["payload"],
                status=cast(InboxStatus, r["status"]),
                created_at=_parse(r["created_at"]),
            )
            for r in self._conn.execute(query, params)
        ]

    def set_inbox_status(self, id: int, status: InboxStatus) -> None:
        """Cambia el status de un mensaje. KeyError si no existe."""
        if status not in INBOX_STATUSES:
            raise ValueError(f"Status de inbox no válido: {status!r}")
        cur = self._write("UPDATE inbox SET status = ? WHERE id = ?", (status, id))
        if cur.rowcount == 0:
            raise KeyError(f"No existe el mensaje {id}")
