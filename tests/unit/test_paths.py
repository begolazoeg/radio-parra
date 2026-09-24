"""
Tests de las rutas de datos y la escritura atómica (§3.3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radio.core.paths import commit_audio, db_path, stock_dir, tmp_dir


def test_layout(tmp_path: Path) -> None:
    assert db_path(tmp_path) == tmp_path / "state.db"
    assert stock_dir(tmp_path, "music") == tmp_path / "stock" / "music"
    assert stock_dir(tmp_path, "time_signal") == tmp_path / "stock" / "time_signal"
    assert tmp_dir(tmp_path) == tmp_path / "tmp"
    assert not (tmp_path / "stock").exists()      # no crean nada


@pytest.mark.parametrize("kind", ["", "../etc", "a/b", "Music", "-x"])
def test_stock_dir_rejects_bad_kinds(tmp_path: Path, kind: str) -> None:
    with pytest.raises(ValueError):
        stock_dir(tmp_path, kind)


def test_commit_audio_moves_and_creates_parents(tmp_path: Path) -> None:
    src = tmp_dir(tmp_path) / "x.wav"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"audio")
    final = stock_dir(tmp_path, "music") / "x.wav"

    assert commit_audio(src, final) == final
    assert final.read_bytes() == b"audio"
    assert not src.exists()


def test_commit_audio_replaces_existing(tmp_path: Path) -> None:
    src = tmp_path / "new.wav"
    src.write_bytes(b"new")
    final = tmp_path / "stock" / "k" / "a.wav"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"old")
    commit_audio(src, final)
    assert final.read_bytes() == b"new"


def test_commit_audio_missing_source(tmp_path: Path) -> None:
    final = tmp_path / "stock" / "k" / "a.wav"
    with pytest.raises(FileNotFoundError):
        commit_audio(tmp_path / "nope.wav", final)
    assert not final.exists()
