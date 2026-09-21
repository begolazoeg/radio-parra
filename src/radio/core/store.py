"""
Capa de persistencia SQLite con WAL para Radio Parra.
Usa solo sqlite3 de stdlib, sin SQLAlchemy.
La escritura atómica se garantiza externamente: add_segment recibe la ruta final
del audio (ya renombrada con os.replace) e inserta solo la fila en BD.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Esquema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS segments (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    title       TEXT NOT NULL,
    duration_s  REAL NOT NULL DEFAULT 0.0,
    audio_path  TEXT,
    producer    TEXT NOT NULL,
    source_url  TEXT,
    script      TEXT,
    voice_id    TEXT,
    tags        TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id  TEXT NOT NULL REFERENCES segments(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS producer_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    producer    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'ok',
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS universe_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unread'
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Clase principal ───────────────────────────────────────────────────────────

class DB:
    """
    Acceso a la base de datos SQLite de Radio Parra.
    Inicializa con WAL y aplica todas las migraciones al arrancar.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        """Aplica el esquema completo (idempotente gracias a IF NOT EXISTS)."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── Segmentos ─────────────────────────────────────────────────────────────

    def add_segment(
        self,
        *,
        id: str,
        kind: str,
        status: str = "pending",
        created_at: str | None = None,
        title: str,
        duration_s: float = 0.0,
        audio_path: Path | None = None,
        producer: str,
        source_url: str | None = None,
        script: str | None = None,
        voice_id: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """
        Inserta un segmento en la BD.
        La ruta audio_path debe ser la ruta final (ya renombrada atómicamente).
        """
        self._conn.execute(
            """
            INSERT INTO segments
              (id, kind, status, created_at, title, duration_s, audio_path,
               producer, source_url, script, voice_id, tags)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                id,
                kind,
                status,
                created_at or _now(),
                title,
                duration_s,
                str(audio_path) if audio_path else None,
                producer,
                source_url,
                script,
                voice_id,
                json.dumps(tags or []),
            ),
        )
        self._conn.commit()

    def get_segment(self, id: str) -> dict[str, Any] | None:
        """Devuelve un segmento por id o None si no existe."""
        row = self._conn.execute(
            "SELECT * FROM segments WHERE id = ?", (id,)
        ).fetchone()
        return dict(row) if row else None

    def list_segments(
        self,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lista segmentos con filtros opcionales por kind y/o status."""
        query = "SELECT * FROM segments WHERE 1=1"
        params: list[Any] = []
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_segment_status(self, id: str, status: str) -> None:
        """Actualiza el status de un segmento existente."""
        self._conn.execute(
            "UPDATE segments SET status = ? WHERE id = ?", (status, id)
        )
        self._conn.commit()

    # ── Plays ─────────────────────────────────────────────────────────────────

    def log_play(
        self,
        segment_id: str,
        started_at: str | None = None,
        ended_at: str | None = None,
        interrupted: bool = False,
    ) -> int:
        """Registra la reproducción de un segmento. Devuelve el rowid."""
        cur = self._conn.execute(
            """
            INSERT INTO plays (segment_id, started_at, ended_at, interrupted)
            VALUES (?,?,?,?)
            """,
            (segment_id, started_at or _now(), ended_at, int(interrupted)),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # ── Producer runs ─────────────────────────────────────────────────────────

    def log_producer_run(
        self,
        producer: str,
        started_at: str | None = None,
        ended_at: str | None = None,
        status: str = "ok",
        detail: str | None = None,
    ) -> int:
        """Registra una ejecución de un producer. Devuelve el rowid."""
        cur = self._conn.execute(
            """
            INSERT INTO producer_runs (producer, started_at, ended_at, status, detail)
            VALUES (?,?,?,?,?)
            """,
            (producer, started_at or _now(), ended_at, status, detail),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # ── Universe state ────────────────────────────────────────────────────────

    def get_universe_state(self, key: str) -> Any | None:
        """Devuelve el valor JSON almacenado para key, o None."""
        row = self._conn.execute(
            "SELECT value FROM universe_state WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_universe_state(self, key: str, value: Any) -> None:
        """Inserta o reemplaza un valor en universe_state."""
        self._conn.execute(
            """
            INSERT INTO universe_state (key, value, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value), _now()),
        )
        self._conn.commit()

    # ── Signals ───────────────────────────────────────────────────────────────

    def add_signal(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Inserta una señal de control. Devuelve el rowid."""
        cur = self._conn.execute(
            "INSERT INTO signals (kind, payload, created_at) VALUES (?,?,?)",
            (kind, json.dumps(payload or {}), _now()),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # ── Inbox ─────────────────────────────────────────────────────────────────

    def add_inbox(self, source: str, content: str) -> int:
        """Añade un mensaje al inbox. Devuelve el rowid."""
        cur = self._conn.execute(
            "INSERT INTO inbox (source, content, created_at) VALUES (?,?,?)",
            (source, content, _now()),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def list_inbox(self, status: str | None = None) -> list[dict[str, Any]]:
        """Lista mensajes del inbox, opcionalmente filtrados por status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM inbox WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM inbox ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_inbox_status(self, id: int, status: str) -> None:
        """Actualiza el status de un mensaje del inbox."""
        self._conn.execute(
            "UPDATE inbox SET status = ? WHERE id = ?", (status, id)
        )
        self._conn.commit()
