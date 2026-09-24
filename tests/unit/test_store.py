"""
Tests unitarios para la capa de persistencia (store.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radio.core.store import DB


@pytest.fixture
def db() -> DB:
    """Base de datos en memoria para cada test."""
    return DB(":memory:")


# ── test_add_and_get_segment ──────────────────────────────────────────────────

def test_add_and_get_segment(db: DB) -> None:
    """Inserta un segmento y lo recupera por id."""
    db.add_segment(
        id="seg_001",
        kind="factual",
        title="Test segment",
        duration_s=90.0,
        producer="factual_producer",
    )
    seg = db.get_segment("seg_001")
    assert seg is not None
    assert seg["id"] == "seg_001"
    assert seg["kind"] == "factual"
    assert seg["title"] == "Test segment"
    assert seg["status"] == "pending"


def test_get_nonexistent_segment_returns_none(db: DB) -> None:
    """get_segment devuelve None si el id no existe."""
    assert db.get_segment("nonexistent") is None


# ── test_list_by_kind_status ──────────────────────────────────────────────────

def test_list_by_kind_status(db: DB) -> None:
    """Inserta 3 segmentos distintos y filtra por kind y status."""
    db.add_segment(id="s1", kind="factual", status="ready", title="F1", duration_s=60.0, producer="p")
    db.add_segment(id="s2", kind="fiction", status="pending", title="Fic1", duration_s=120.0, producer="p")
    db.add_segment(id="s3", kind="factual", status="pending", title="F2", duration_s=45.0, producer="p")

    # Filtro por kind
    factuals = db.list_segments(kind="factual")
    assert len(factuals) == 2
    assert all(s["kind"] == "factual" for s in factuals)

    # Filtro por status
    pending = db.list_segments(status="pending")
    assert len(pending) == 2

    # Filtro combinado
    factual_pending = db.list_segments(kind="factual", status="pending")
    assert len(factual_pending) == 1
    assert factual_pending[0]["id"] == "s3"


# ── test_atomic_write_pattern ─────────────────────────────────────────────────

def test_atomic_write_pattern(db: DB) -> None:
    """
    Test conceptual: add_segment solo inserta la fila, no copia el archivo.
    La ruta audio_path recibida debe ser la ruta final (ya renombrada externamente).
    Aquí validamos que el path almacenado es exactamente el que se pasó.
    """
    final_path = Path("/data/audio/final_segment.wav")
    db.add_segment(
        id="atomic_001",
        kind="music",
        title="Atomic test",
        duration_s=200.0,
        audio_path=final_path,
        producer="music_producer",
    )
    seg = db.get_segment("atomic_001")
    assert seg is not None
    assert seg["audio_path"] == str(final_path)


# ── test_tags_deserialized ────────────────────────────────────────────────────

def test_tags_deserialized_as_list(db: DB) -> None:
    """get_segment y list_segments devuelven tags como lista, no como JSON string."""
    db.add_segment(
        id="tagged_001",
        kind="factual",
        title="Tagged segment",
        duration_s=60.0,
        producer="p",
        tags=["news", "science"],
    )
    seg = db.get_segment("tagged_001")
    assert seg is not None
    assert isinstance(seg["tags"], list), f"expected list, got {type(seg['tags'])}"
    assert seg["tags"] == ["news", "science"]

    rows = db.list_segments(kind="factual")
    assert isinstance(rows[0]["tags"], list)


# ── test_producer_run_log ─────────────────────────────────────────────────────

def test_producer_run_log(db: DB) -> None:
    """log_producer_run registra la ejecución y queda en la tabla."""
    rowid = db.log_producer_run(
        producer="factual_producer",
        status="ok",
        detail="Fetched 3 articles",
    )
    assert rowid >= 1

    # Verificamos via list_segments que la BD sigue operativa,
    # y consultamos producer_runs directamente
    rows = db._conn.execute(
        "SELECT * FROM producer_runs WHERE id = ?", (rowid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["producer"] == "factual_producer"
    assert rows[0]["status"] == "ok"


# ── test_universe_state ───────────────────────────────────────────────────────

def test_universe_state(db: DB) -> None:
    """set y get de universe_state son consistentes."""
    # Valor que no existe
    assert db.get_universe_state("world_year") is None

    # Set y get básico
    db.set_universe_state("world_year", 2157)
    assert db.get_universe_state("world_year") == 2157

    # Actualización (UPSERT)
    db.set_universe_state("world_year", 2158)
    assert db.get_universe_state("world_year") == 2158

    # Valor complejo
    db.set_universe_state("characters", {"hero": "Ana", "villain": "Rodolfo"})
    assert db.get_universe_state("characters")["hero"] == "Ana"


# ── Consultas para scheduler / playout ────────────────────────────────────────

def test_pick_ready_prefers_never_played_then_least_recent(db: DB) -> None:
    """pick_ready elige primero lo nunca emitido y después lo emitido hace más tiempo."""
    for i, created in enumerate(["2024-01-01T00:00:00", "2024-01-02T00:00:00"]):
        db.add_segment(
            id=f"m{i}", kind="music", status="ready", created_at=created,
            title=f"t{i}", producer="music",
        )
    assert db.pick_ready("music")["id"] == "m0"  # type: ignore[index]
    db.log_play("m0", started_at="2024-02-01T10:00:00")
    assert db.pick_ready("music")["id"] == "m1"  # type: ignore[index]
    db.log_play("m1", started_at="2024-02-01T11:00:00")
    assert db.pick_ready("music")["id"] == "m0"  # type: ignore[index]
    assert db.pick_ready("fiction") is None


def test_pick_ready_exclude_tags(db: DB) -> None:
    db.add_segment(id="a", kind="music", status="ready", title="a", producer="m", tags=["artist:x"])
    db.add_segment(id="b", kind="music", status="ready", title="b", producer="m", tags=["artist:y"])
    db.log_play("b")
    assert db.pick_ready("music", exclude_tags=["artist:x"])["id"] == "b"  # type: ignore[index]


def test_pick_ready_max_duration(db: DB) -> None:
    db.add_segment(id="long", kind="music", status="ready", title="l", producer="m",
                   duration_s=600, created_at="2024-01-01T00:00:00")
    db.add_segment(id="short", kind="music", status="ready", title="s", producer="m",
                   duration_s=200, created_at="2024-01-02T00:00:00")
    assert db.pick_ready("music")["id"] == "long"  # type: ignore[index]
    assert db.pick_ready("music", max_duration_s=300)["id"] == "short"  # type: ignore[index]
    assert db.pick_ready("music", max_duration_s=100) is None


def test_count_ready_and_audio_path_lookup(db: DB) -> None:
    db.add_segment(id="a", kind="music", status="ready", title="a", producer="m",
                   audio_path=Path("/x/a.mp3"))
    db.add_segment(id="b", kind="jingle", status="pending", title="b", producer="m")
    assert db.count_ready_by_kind() == {"music": 1}
    assert db.get_segment_by_audio_path("/x/a.mp3")["id"] == "a"  # type: ignore[index]
    assert db.get_segment_by_audio_path("/x/none.mp3") is None


def test_list_plays_and_finish_play(db: DB) -> None:
    db.add_segment(id="a", kind="music", status="ready", title="a", producer="m", duration_s=5)
    pid = db.log_play("a", started_at="2024-01-01T10:00:00")
    db.finish_play(pid, ended_at="2024-01-01T10:00:05")
    plays = db.list_plays(since="2024-01-01T00:00:00")
    assert len(plays) == 1
    assert plays[0]["kind"] == "music" and plays[0]["ended_at"] == "2024-01-01T10:00:05"
    assert db.list_plays(since="2025-01-01T00:00:00") == []


def test_last_producer_run(db: DB) -> None:
    assert db.last_producer_run("x") is None
    db.log_producer_run("x", started_at="2024-01-01T00:00:00")
    db.log_producer_run("x", started_at="2024-01-02T00:00:00", status="error")
    assert db.last_producer_run("x")["status"] == "error"  # type: ignore[index]


def test_count_producer_runs(db: DB) -> None:
    assert db.count_producer_runs() == 0
    db.log_producer_run("x", status="ok")
    db.log_producer_run("x", status="error")
    db.log_producer_run("y", status="error")
    assert db.count_producer_runs() == 3
    assert db.count_producer_runs(status="error") == 2
