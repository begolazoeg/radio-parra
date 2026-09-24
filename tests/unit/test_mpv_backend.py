"""
Tests del backend mpv usando un binario falso (script de shell) en lugar de mpv.
"""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

from radio.providers.audio.base import AudioBackend
from radio.providers.audio.mpv import MpvAudioBackend


def _fake_mpv(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-mpv"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_is_audio_backend() -> None:
    assert isinstance(MpvAudioBackend(), AudioBackend)


def test_play_and_drain_invoke_binary_in_order(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    backend = MpvAudioBackend(mpv_bin=_fake_mpv(tmp_path, f'for a; do last="$a"; done; echo "$last" >> {log}'))
    backend.play(Path("/a.mp3"))
    backend.enqueue(Path("/b.mp3"))
    backend.enqueue(Path("/c.mp3"))
    backend.drain()
    assert log.read_text().split() == ["/a.mp3", "/b.mp3", "/c.mp3"]


def test_skip_interrupts_current_play(tmp_path: Path) -> None:
    backend = MpvAudioBackend(mpv_bin=_fake_mpv(tmp_path, "exec sleep 30"))
    t = threading.Thread(target=backend.play, args=(Path("/long.mp3"),))
    start = time.monotonic()
    t.start()
    time.sleep(0.3)
    backend.skip()
    t.join(timeout=5)
    assert not t.is_alive()
    assert time.monotonic() - start < 5
