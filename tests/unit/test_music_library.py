"""
Tests unitarios para la importación de la biblioteca musical local (music/library.py).
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from typer.testing import CliRunner

from radio.cli import app
from radio.core.store import DB
from radio.music.library import (
    AUDIO_EXTENSIONS,
    import_directory,
    read_meta,
    slugify,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_RATE = 8000


def _make_wav(path: Path, seconds: float = 0.5) -> Path:
    """Genera un WAV mono de 16 bits con silencio de la duración indicada."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(_RATE * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(b"\x00\x00" * frames)
    return path


@pytest.fixture
def db() -> DB:
    return DB(":memory:")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """
    Biblioteca de prueba:
      music/
        Rosalía - Tiny Desk (Home) Concert.wav
        loose_track-name.wav
        notes.txt                      (ignorado)
        broken.mp3                     (corrupto → failed)
        Tiny Desk/Anderson .Paak - Live.wav
        nested/deeper/Björk - Joga.wav
    """
    root = tmp_path / "music"
    _make_wav(root / "Rosalía - Tiny Desk (Home) Concert.wav", 1.0)
    _make_wav(root / "loose_track-name.wav", 0.25)
    _make_wav(root / "Tiny Desk" / "Anderson .Paak - Live.wav", 0.5)
    _make_wav(root / "nested" / "deeper" / "Björk - Joga.wav", 0.5)
    (root / "notes.txt").write_text("no soy audio")
    (root / "broken.mp3").write_bytes(b"esto no es un mp3 " * 64)
    return root


def _by_title(db: DB) -> dict[str, dict[str, object]]:
    return {s["title"]: s for s in db.list_segments(kind="music")}


# ── slugify ───────────────────────────────────────────────────────────────────

def test_slugify_strips_accents_and_punctuation() -> None:
    assert slugify("Rosalía") == "rosalia"
    assert slugify("Anderson .Paak") == "anderson-paak"
    assert slugify("  Björk & Co!  ") == "bjork-co"
    assert slugify("Café Tacvba") == "cafe-tacvba"


def test_audio_extensions_contains_common_formats() -> None:
    assert {".mp3", ".wav", ".flac"} <= AUDIO_EXTENSIONS


# ── read_meta ─────────────────────────────────────────────────────────────────

def test_read_meta_wav_duration_and_stem_parsing(tmp_path: Path) -> None:
    path = _make_wav(tmp_path / "Rosalía - Tiny Desk Concert.wav", 1.0)
    meta = read_meta(path)
    assert meta is not None
    assert meta.path == path.resolve()
    assert meta.path.is_absolute()
    assert meta.artist == "Rosalía"
    assert meta.title == "Tiny Desk Concert"
    assert meta.duration_s == pytest.approx(1.0, abs=0.01)


def test_read_meta_fallback_title_without_artist(tmp_path: Path) -> None:
    path = _make_wav(tmp_path / "some_cool-track__name.wav")
    meta = read_meta(path)
    assert meta is not None
    assert meta.artist is None
    assert meta.title == "some cool track name"


def test_read_meta_underscored_artist_title(tmp_path: Path) -> None:
    path = _make_wav(tmp_path / "Mon_Laferte_-_Tu_Falta_De_Querer.wav")
    meta = read_meta(path)
    assert meta is not None
    assert meta.artist == "Mon Laferte"
    assert meta.title == "Tu Falta De Querer"


def test_read_meta_corrupt_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"\x00garbage\xff" * 50)
    with caplog.at_level("WARNING"):
        assert read_meta(path) is None
    assert caplog.records


def test_read_meta_zero_duration_returns_none(tmp_path: Path) -> None:
    path = _make_wav(tmp_path / "empty.wav", 0.0)
    assert read_meta(path) is None


# ── import_directory ──────────────────────────────────────────────────────────

def test_import_directory_adds_tracks(db: DB, library: Path) -> None:
    report = import_directory(db, library)
    assert report.added == 4
    assert report.skipped_existing == 0
    assert report.failed == [(library / "broken.mp3").resolve()]

    segs = _by_title(db)
    assert set(segs) == {
        "Tiny Desk (Home) Concert",
        "loose track name",
        "Live",
        "Joga",
    }
    for seg in segs.values():
        assert seg["kind"] == "music"
        assert seg["status"] == "ready"
        assert seg["producer"] == "music_library"
        assert seg["source_url"] is None
        assert Path(str(seg["audio_path"])).is_absolute()
        assert float(seg["duration_s"]) > 0  # type: ignore[arg-type]


def test_import_directory_tags(db: DB, library: Path) -> None:
    import_directory(db, library)
    segs = _by_title(db)
    assert segs["Tiny Desk (Home) Concert"]["tags"] == ["artist:rosalia", "source:tiny_desk"]
    assert segs["Live"]["tags"] == ["artist:anderson-paak", "source:tiny_desk"]
    assert segs["Joga"]["tags"] == ["artist:bjork"]
    assert segs["loose track name"]["tags"] == []


def test_import_directory_tiny_desk_folder_variants(db: DB, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _make_wav(root / "tiny_desk" / "a.wav")
    _make_wav(root / "TINY-DESK concerts" / "b.wav")
    _make_wav(root / "other" / "c.wav")
    import_directory(db, root)
    tags = {s["title"]: s["tags"] for s in db.list_segments(kind="music")}
    assert tags == {"a": ["source:tiny_desk"], "b": ["source:tiny_desk"], "c": []}


def test_import_directory_is_idempotent(db: DB, library: Path) -> None:
    import_directory(db, library)
    report = import_directory(db, library)
    assert report.added == 0
    assert report.skipped_existing == 4
    assert len(report.failed) == 1
    assert len(db.list_segments(kind="music")) == 4


def test_import_directory_dedup_uses_audio_path(db: DB, library: Path) -> None:
    import_directory(db, library)
    path = (library / "nested" / "deeper" / "Björk - Joga.wav").resolve()
    seg = db.get_segment_by_audio_path(path)
    assert seg is not None
    assert seg["title"] == "Joga"


def test_import_directory_custom_producer(db: DB, library: Path) -> None:
    import_directory(db, library, producer="tiny_desk_import")
    assert {s["producer"] for s in db.list_segments(kind="music")} == {"tiny_desk_import"}


def test_import_directory_rejects_non_directory(db: DB, tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        import_directory(db, tmp_path / "missing")


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_import_music(library: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "radio.db"
    runner = CliRunner()

    result = runner.invoke(app, ["import-music", str(library), "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "Añadidas: 4" in result.output
    assert "Ya existentes: 0" in result.output
    assert "Fallidas: 1" in result.output
    assert db_path.exists()

    again = runner.invoke(app, ["import-music", str(library), "--db", str(db_path)])
    assert again.exit_code == 0, again.output
    assert "Añadidas: 0" in again.output
    assert "Ya existentes: 4" in again.output

    db = DB(db_path)
    try:
        assert len(db.list_segments(kind="music")) == 4
    finally:
        db.close()


def test_cli_import_music_missing_dir(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["import-music", str(tmp_path / "nope"), "--db", str(tmp_path / "r.db")]
    )
    assert result.exit_code != 0
