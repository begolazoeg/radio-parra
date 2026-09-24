"""
Tests unitarios para la capa de persistencia (store.py, §3.2).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from radio.core.models import Segment
from radio.core.store import (
    DB,
    SCHEMA_VERSION,
    LegacySchemaError,
    StaleUniverseState,
)

MADRID = ZoneInfo("Europe/Madrid")
T0 = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


@pytest.fixture
def db() -> DB:
    """Base de datos en memoria para cada test."""
    return DB(":memory:")


def seg(seg_id: str, kind: str = "music", **kw: object) -> Segment:
    data: dict[str, object] = {
        "id": seg_id, "kind": kind, "factual": False, "path": Path(f"/x/{seg_id}.mp3"),
        "duration_s": 100.0, "created_at": T0, "producer": "test",
    }
    data.update(kw)
    return Segment(**data)  # type: ignore[arg-type]


# ── Esquema ───────────────────────────────────────────────────────────────────

def test_schema_matches_architecture(db: DB) -> None:
    tables = {
        r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert tables == {"segments", "play_log", "producer_runs", "universe_state", "signals", "inbox"}
    indexes = {
        r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        if not r[0].startswith("sqlite_autoindex")
    }
    assert indexes == {"idx_segments_kind_status"}
    cols = [r[1] for r in db._conn.execute("PRAGMA table_info(segments)")]
    assert cols == [
        "id", "kind", "factual", "status", "path", "duration_s", "created_at", "expires_at",
        "priority", "parent_id", "voice_id", "producer", "prompt_version", "summary", "meta_json",
    ]
    assert db.schema_version == SCHEMA_VERSION == 1
    assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_file_db_uses_wal_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with DB(path) as first:
        assert first._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        first.add_segment(seg("a"))
    with DB(path) as again:
        assert again.get_segment("a") is not None
        assert again.schema_version == 1


def test_legacy_db_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "radio.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(LegacySchemaError, match="bórrala"):
        DB(path)
    assert isinstance(LegacySchemaError("x"), RuntimeError)


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="v99"):
        DB(path)


def test_foreign_keys_enforced(db: DB) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.log_play_start("no-existe", "music", "default", T0)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_segment(seg("intro", "host_intro", parent_id="no-existe"))
    # Emisión fuera de stock (p. ej. bucle de emergencia): segment_id NULL
    assert db.log_play_start(None, "emergency", "default", T0) > 0


# ── Segmentos ─────────────────────────────────────────────────────────────────

def test_add_and_get_segment_roundtrip(db: DB) -> None:
    created = datetime(2026, 1, 5, 11, 0, 0, 123456, tzinfo=MADRID)
    original = seg(
        "s1", "weather", factual=True, created_at=created,
        expires_at=created + timedelta(hours=6), priority=2, voice_id="locutor_principal",
        prompt_version="weather/v1", summary="Sol y nubes",
        meta={"title": "El tiempo", "tags": ["hour:2026-01-05T11"], "sources": [{"id": "om"}],
              "script": "Hoy, sol.", "ñ": "sí"},
    )
    db.add_segment(original)
    loaded = db.get_segment("s1")
    assert loaded == original          # fechas aware: igualdad por instante
    assert loaded is not None
    assert loaded.created_at.tzinfo is not None
    assert loaded.created_at.utcoffset() == timedelta(0)   # se guardan en UTC
    assert db.get_segment("nope") is None


def test_parent_id_link(db: DB) -> None:
    db.add_segment(seg("m"))
    db.add_segment(seg("i", "host_intro", factual=True, parent_id="m"))
    intro = db.get_segment("i")
    assert intro is not None and intro.parent_id == "m"


def test_naive_datetimes_rejected(db: DB) -> None:
    with pytest.raises(ValueError, match="aware"):
        db.add_segment(seg("a", created_at=datetime(2026, 1, 5, 10, 0)))
    with pytest.raises(ValueError):
        db.stock_view(datetime(2026, 1, 5))


def test_invalid_status_rejected(db: DB) -> None:
    with pytest.raises(ValueError):
        db.add_segment(seg("a", status="done"))
    db.add_segment(seg("b"))
    with pytest.raises(ValueError):
        db.update_segment_status("b", "playing")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        db.update_segment_status("nope", "retired")


def test_list_by_kind_status(db: DB) -> None:
    db.add_segment(seg("s1", "weather", created_at=T0 + timedelta(seconds=2)))
    db.add_segment(seg("s2", "consultorio", status="pending_review"))
    db.add_segment(seg("s3", "weather", status="pending_review", created_at=T0 + timedelta(seconds=1)))

    assert [s.id for s in db.list_segments(kind="weather")] == ["s3", "s1"]   # por created_at
    assert {s.id for s in db.list_segments(status="pending_review")} == {"s2", "s3"}
    assert [s.id for s in db.list_segments(kind="weather", status="pending_review")] == ["s3"]
    assert len(db.list_segments()) == 3


def test_update_status(db: DB) -> None:
    db.add_segment(seg("a"))
    db.update_segment_status("a", "quarantined")
    got = db.get_segment("a")
    assert got is not None and got.status == "quarantined"


def test_find_by_meta_scalar_and_list(db: DB) -> None:
    db.add_segment(seg("a", meta={"guid": "npr-1", "tags": ["artist:x"]}))
    db.add_segment(seg("b", meta={"guid": "npr-2", "tags": ["artist:y", "hour:2026-01-05T11"]},
                       created_at=T0 + timedelta(seconds=1)))
    db.add_segment(seg("c", "time_signal", meta={"tags": ["hour:2026-01-05T11"]}, status="expired"))

    found = db.find_by_meta("music", "guid", "npr-2")
    assert found is not None and found.id == "b"
    assert db.find_by_meta("music", "guid", "npr-3") is None
    assert db.find_by_meta("jingle", "guid", "npr-1") is None
    tagged = db.find_by_meta("music", "tags", "artist:x")
    assert tagged is not None and tagged.id == "a"
    ts = db.find_by_meta("time_signal", "tags", "hour:2026-01-05T11")
    assert ts is not None and ts.id == "c"
    assert db.find_by_meta("time_signal", "tags", "hour:2026-01-05T11", status="ready") is None
    with pytest.raises(ValueError):
        db.find_by_meta("music", "x') OR 1=1 --", "a")


def test_find_by_meta_numbers(db: DB) -> None:
    db.add_segment(seg("a", meta={"episode": 7}))
    found = db.find_by_meta("music", "episode", 7)
    assert found is not None and found.id == "a"


def test_find_by_path(db: DB) -> None:
    db.add_segment(seg("a", path=Path("/x/a.mp3")))
    found = db.find_by_path("/x/a.mp3")
    assert found is not None and found.id == "a"
    assert db.find_by_path(Path("/x/a.mp3")) is not None
    assert db.find_by_path("/x/none.mp3") is None


# ── Stock ─────────────────────────────────────────────────────────────────────

def test_stock_view_excludes_non_ready_and_expired(db: DB) -> None:
    db.add_segment(seg("m1"))
    db.add_segment(seg("m2", status="retired"))
    db.add_segment(seg("n1", "news", expires_at=T0 + timedelta(hours=4)))
    db.add_segment(seg("n0", "news", expires_at=T0))                  # justo caduca
    db.add_segment(seg("w", "weather", status="pending_review"))
    # Otra zona horaria: la comparación es por instante, no por texto local
    db.add_segment(seg("n2", "news", expires_at=datetime(2026, 1, 5, 11, 30, tzinfo=MADRID)))

    view = db.stock_view(T0)
    assert view.kinds() == {"music", "news"}
    assert view.count("music") == 1
    assert {s.id for s in view.get("news")} == {"n1", "n2"}
    later = db.stock_view(datetime(2026, 1, 5, 12, 0, tzinfo=MADRID))   # 11:00 UTC
    assert {s.id for s in later.get("news")} == {"n1"}


def test_status_counts_and_next_expirations(db: DB) -> None:
    db.add_segment(seg("m1"))
    db.add_segment(seg("m2", status="quarantined"))
    db.add_segment(seg("n1", "news", expires_at=T0 + timedelta(hours=4)))
    db.add_segment(seg("n2", "news", expires_at=T0 + timedelta(hours=1)))
    db.add_segment(seg("n3", "news", expires_at=T0 - timedelta(hours=1)))
    assert db.status_counts() == {
        "music": {"quarantined": 1, "ready": 1},
        "news": {"ready": 3},
    }
    assert [s.id for s in db.next_expirations(T0)] == ["n2", "n1"]
    assert [s.id for s in db.next_expirations(T0, limit=1)] == ["n2"]


def test_expire_segments(db: DB) -> None:
    db.add_segment(seg("n0", "news", expires_at=T0 - timedelta(minutes=1)))
    db.add_segment(seg("n1", "news", expires_at=T0 + timedelta(minutes=1)))
    db.add_segment(seg("t0", "time_signal", expires_at=T0 - timedelta(minutes=1)))
    db.add_segment(seg("r", "news", expires_at=T0 - timedelta(minutes=1), status="retired"))

    assert db.expire_segments(T0, kind="time_signal") == 1
    assert db.expire_segments(T0) == 1
    statuses = {s.id: s.status for s in db.list_segments()}
    assert statuses == {"n0": "expired", "n1": "ready", "t0": "expired", "r": "retired"}


# ── pick_ready ────────────────────────────────────────────────────────────────

def test_pick_ready_prefers_never_played_then_least_recent(db: DB) -> None:
    db.add_segment(seg("m0", created_at=T0))
    db.add_segment(seg("m1", created_at=T0 + timedelta(days=1)))
    later = T0 + timedelta(days=30)
    assert db.pick_ready("music", now=later).id == "m0"  # type: ignore[union-attr]
    db.log_play_start("m0", "music", "default", later)
    assert db.pick_ready("music", now=later).id == "m1"  # type: ignore[union-attr]
    db.log_play_start("m1", "music", "default", later + timedelta(hours=1))
    assert db.pick_ready("music", now=later).id == "m0"  # type: ignore[union-attr]
    assert db.pick_ready("consultorio", now=later) is None


def test_pick_ready_tiebreak_by_id(db: DB) -> None:
    db.add_segment(seg("b"))
    db.add_segment(seg("a"))
    assert db.pick_ready("music", now=T0).id == "a"  # type: ignore[union-attr]


def test_pick_ready_skips_expired_and_non_ready(db: DB) -> None:
    db.add_segment(seg("old", "news", expires_at=T0))
    db.add_segment(seg("q", "news", status="quarantined"))
    assert db.pick_ready("news", now=T0) is None
    db.add_segment(seg("fresh", "news", expires_at=T0 + timedelta(hours=1)))
    assert db.pick_ready("news", now=T0).id == "fresh"  # type: ignore[union-attr]


def test_pick_ready_exclude_tags(db: DB) -> None:
    db.add_segment(seg("a", meta={"tags": ["artist:x"]}))
    db.add_segment(seg("b", meta={"tags": ["artist:y"]}))
    db.log_play_start("b", "music", "default", T0)
    assert db.pick_ready("music", now=T0, exclude_tags=["artist:x"]).id == "b"  # type: ignore[union-attr]
    assert db.pick_ready("music", now=T0, exclude_tags=("artist:x", "artist:y")) is None


def test_pick_ready_max_duration(db: DB) -> None:
    db.add_segment(seg("long", duration_s=600, created_at=T0))
    db.add_segment(seg("short", duration_s=200, created_at=T0 + timedelta(seconds=1)))
    assert db.pick_ready("music", now=T0).id == "long"  # type: ignore[union-attr]
    assert db.pick_ready("music", now=T0, max_duration_s=300).id == "short"  # type: ignore[union-attr]
    assert db.pick_ready("music", now=T0, max_duration_s=100) is None


# ── play_log ──────────────────────────────────────────────────────────────────

def test_play_log_start_end_and_list(db: DB) -> None:
    db.add_segment(seg("a", duration_s=5))
    start = datetime(2026, 1, 5, 11, 0, tzinfo=MADRID)
    pid = db.log_play_start("a", "music", "tinydesk", start)
    open_entry = db.list_play_log()[0]
    assert open_entry.ended_at is None and open_entry.skipped is False
    db.log_play_end(pid, start + timedelta(seconds=5), skipped=True)

    entries = db.list_play_log(since=start - timedelta(minutes=1))
    assert len(entries) == 1
    e = entries[0]
    assert (e.id, e.segment_id, e.kind, e.mode) == (pid, "a", "music", "tinydesk")
    assert e.started_at == start and e.ended_at == start + timedelta(seconds=5)
    assert e.skipped is True and e.duration_s == 5.0
    assert db.list_play_log(since=start + timedelta(seconds=1)) == []
    with pytest.raises(KeyError):
        db.log_play_end(999, start)


def test_play_log_order_across_offsets(db: DB) -> None:
    db.add_segment(seg("a"))
    # 10:30 en Madrid (09:30 UTC) es anterior a 09:45 UTC aunque el texto local sea mayor
    db.log_play_start("a", "music", "default", datetime(2026, 1, 5, 9, 45, tzinfo=UTC))
    db.log_play_start("a", "music", "default", datetime(2026, 1, 5, 10, 30, tzinfo=MADRID))
    entries = db.list_play_log()
    assert [e.started_at.hour for e in entries] == [9, 9]
    assert entries[0].started_at < entries[1].started_at
    assert entries[0].started_at.minute == 30


# ── producer_runs ─────────────────────────────────────────────────────────────

def test_producer_runs_lifecycle(db: DB) -> None:
    assert db.last_producer_run("weather") is None
    rid = db.start_producer_run("weather", T0)
    running = db.last_producer_run("weather")
    assert running is not None and running.ok is None and running.ended_at is None
    db.finish_producer_run(
        rid, ended_at=T0 + timedelta(seconds=30), ok=True, n_segments=2,
        tokens_in=1200, tokens_out=300, tts_chars=900, cost_eur=0.042,
    )
    run = db.last_producer_run("weather")
    assert run is not None
    assert (run.ok, run.n_segments, run.tokens_in, run.tokens_out, run.tts_chars) == (
        True, 2, 1200, 300, 900
    )
    assert run.cost_eur == pytest.approx(0.042) and run.error is None
    assert run.ended_at == T0 + timedelta(seconds=30)

    rid2 = db.start_producer_run("weather", T0 + timedelta(hours=1))
    db.finish_producer_run(rid2, ended_at=T0 + timedelta(hours=1), ok=False, error="boom")
    last = db.last_producer_run("weather")
    assert last is not None and last.ok is False and last.error == "boom"
    assert [r.id for r in db.list_producer_runs()] == [rid, rid2]
    assert db.list_producer_runs("other") == []
    with pytest.raises(KeyError):
        db.finish_producer_run(999, ended_at=T0, ok=True)


def test_month_cost(db: DB) -> None:
    for day, cost in ((31, 1.0), (1, 0.5), (15, 0.25)):
        month = 12 if day == 31 else 1
        year = 2025 if day == 31 else 2026
        rid = db.start_producer_run("p", datetime(year, month, day, 12, tzinfo=MADRID))
        db.finish_producer_run(rid, ended_at=datetime(year, month, day, 12, tzinfo=MADRID),
                               ok=True, cost_eur=cost)
    db.start_producer_run("p", datetime(2026, 1, 20, tzinfo=MADRID))   # en curso: coste NULL
    assert db.month_cost_eur(datetime(2026, 1, 1, tzinfo=MADRID)) == pytest.approx(0.75)
    assert db.month_cost_eur(datetime(2026, 2, 1, tzinfo=MADRID)) == 0.0


# ── universe_state ────────────────────────────────────────────────────────────

def test_universe_state_optimistic_versioning(db: DB) -> None:
    assert db.get_universe_state("liga") is None
    assert db.put_universe_state("liga", {"jornada": 1}, expected_version=None, updated_at=T0) == 1
    assert db.get_universe_state("liga") == (1, {"jornada": 1})

    with pytest.raises(StaleUniverseState):
        db.put_universe_state("liga", {"jornada": 9}, expected_version=None, updated_at=T0)
    assert db.put_universe_state("liga", {"jornada": 2}, expected_version=1, updated_at=T0) == 2
    with pytest.raises(StaleUniverseState):
        db.put_universe_state("liga", {"jornada": 3}, expected_version=1, updated_at=T0)
    assert db.get_universe_state("liga") == (2, {"jornada": 2})


def test_transaction_is_atomic(db: DB) -> None:
    with pytest.raises(StaleUniverseState), db.transaction():
        db.add_segment(seg("liga-1", "liga"))
        db.put_universe_state("liga", {"x": 1}, expected_version=5, updated_at=T0)
    assert db.get_segment("liga-1") is None

    with db.transaction():
        db.add_segment(seg("liga-1", "liga"))
        with db.transaction():   # anidada: confirma la externa
            db.put_universe_state("liga", {"x": 1}, expected_version=None, updated_at=T0)
    assert db.get_segment("liga-1") is not None
    assert db.get_universe_state("liga") == (1, {"x": 1})


# ── signals ───────────────────────────────────────────────────────────────────

def test_signals(db: DB) -> None:
    assert db.latest_signal("parra", "humedad") is None
    db.add_signal("parra", "humedad", "41", T0)
    db.add_signal("parra", "humedad", "38", T0 + timedelta(hours=1))
    db.add_signal("parra", "temp", "20", T0 + timedelta(hours=2))
    latest = db.latest_signal("parra", "humedad")
    assert latest is not None
    assert (latest.value, latest.at) == ("38", T0 + timedelta(hours=1))


# ── inbox ─────────────────────────────────────────────────────────────────────

def test_inbox(db: DB) -> None:
    a = db.add_inbox("telegram", "12345", "Para mi madre", created_at=T0)
    b = db.add_inbox("api", "local", "Felicidades", created_at=T0 + timedelta(minutes=1))
    items = db.list_inbox()
    assert [i.id for i in items] == [a, b]
    assert all(i.status == "pending" for i in items)
    assert items[0].channel == "telegram" and items[0].created_at == T0

    db.set_inbox_status(a, "approved")
    assert [i.id for i in db.list_inbox("pending")] == [b]
    assert [i.id for i in db.list_inbox("approved")] == [a]
    with pytest.raises(ValueError):
        db.set_inbox_status(a, "leído")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        db.set_inbox_status(999, "used")
    # created_at por defecto: ahora (aware)
    c = db.add_inbox("api", "local", "Hola")
    assert db.list_inbox("pending")[-1].id == c
