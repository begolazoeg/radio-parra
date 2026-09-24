"""
Tests del backend mpv por JSON-IPC contra un mpv falso (tests/fixtures/fake_mpv.py).

El mpv falso "reproduce" WAVs durmiendo su duración (0.1–0.3 s aquí) y se estrella
con archivos cuyo nombre contiene "crash".
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radio.core.clock import FakeClock
from radio.providers.audio import (
    AudioBackend,
    Ended,
    EventRecorder,
    MpvError,
    MpvIpcBackend,
    PlayerEvent,
    QueueingAudioBackend,
    Started,
)

FAKE_MPV = Path(__file__).resolve().parents[1] / "fixtures" / "fake_mpv.py"
TIMEOUT = 5.0


def make_wav(path: Path, seconds: float, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def wait_until(cond: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condición no alcanzada a tiempo"
        time.sleep(0.01)


def next_event(rec: EventRecorder) -> PlayerEvent:
    ev = rec.get(timeout=TIMEOUT)
    assert ev is not None, "no llegó ningún evento"
    return ev


def summary(events: list[PlayerEvent]) -> list[tuple[str, str, str]]:
    return [
        ("start", e.path.name, "") if isinstance(e, Started) else ("end", e.path.name, e.reason)
        for e in events
    ]


@pytest.fixture
def backend() -> Iterator[MpvIpcBackend]:
    b = MpvIpcBackend(
        [sys.executable, str(FAKE_MPV)],
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
        backoff_initial=0.05,
        backoff_max=0.2,
    )
    yield b
    b.close()


@pytest.fixture
def rec(backend: MpvIpcBackend) -> EventRecorder:
    r = EventRecorder()
    backend.add_listener(r)
    return r


def test_protocols() -> None:
    b = MpvIpcBackend([sys.executable, str(FAKE_MPV)])
    assert isinstance(b, AudioBackend)
    assert isinstance(b, QueueingAudioBackend)
    b.close()  # cerrar sin haber arrancado no falla


def test_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_MPV_ARGV_LOG", str(argv_log))
    with MpvIpcBackend(
        [sys.executable, str(FAKE_MPV)], extra_args=["--volume=50"]
    ) as b:
        assert b.alive()
        sock = b.socket_path
        assert sock is not None and sock.exists()
    args = json.loads(argv_log.read_text().splitlines()[0])
    assert args[:4] == ["--idle=yes", "--no-video", "--no-terminal", "--audio-display=no"]
    assert f"--input-ipc-server={sock}" in args
    assert args[-2:] == ["--gapless-audio=weak", "--volume=50"]


def test_enqueue_plays_in_order_with_events(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    files = [make_wav(tmp_path / f"{n}.wav", 0.1) for n in ("a", "b", "c")]
    for f in files:
        backend.enqueue(f)
    first = next_event(rec)
    assert first == Started(files[0], datetime(2026, 1, 1, tzinfo=UTC))
    assert backend.current() == files[0]
    assert backend.queued() == 2
    wait_until(lambda: len(rec.events) == 6)
    assert summary(rec.events) == [
        ("start", "a.wav", ""), ("end", "a.wav", "eof"),
        ("start", "b.wav", ""), ("end", "b.wav", "eof"),
        ("start", "c.wav", ""), ("end", "c.wav", "eof"),
    ]
    assert backend.current() is None and backend.queued() == 0
    wait_until(backend.idle)
    # La playlist de mpv se poda: solo queda la última entrada terminada
    assert [Path(e["filename"]).name for e in backend.mpv_playlist()] == ["c.wav"]


def test_event_timestamps_use_injected_clock(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
    rec = EventRecorder()
    with MpvIpcBackend([sys.executable, str(FAKE_MPV)], clock=clock) as b:
        b.add_listener(rec)
        f = make_wav(tmp_path / "a.wav", 0.1)
        b.enqueue(f)
        assert next_event(rec) == Started(f, datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
        clock.advance(30)
        assert next_event(rec) == Ended(f, datetime(2026, 5, 1, 12, 0, 30, tzinfo=UTC), "eof")


def test_play_blocks_until_file_ends(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    a = make_wav(tmp_path / "a.wav", 0.15)
    b = make_wav(tmp_path / "b.wav", 0.15)
    backend.enqueue(a)
    start = time.monotonic()
    backend.play(b)  # suena a y luego b
    elapsed = time.monotonic() - start
    assert elapsed >= 0.25
    assert summary(rec.events)[-1] == ("end", "b.wav", "eof")


def test_skip_emits_skipped_and_advances(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    long = make_wav(tmp_path / "long.wav", 30)
    short = make_wav(tmp_path / "short.wav", 0.1)
    backend.enqueue(long)
    backend.enqueue(short)
    assert next_event(rec) == Started(long, datetime(2026, 1, 1, tzinfo=UTC))
    backend.skip()
    ended = next_event(rec)
    assert isinstance(ended, Ended) and ended.path == long and ended.reason == "skipped"
    assert next_event(rec) == Started(short, datetime(2026, 1, 1, tzinfo=UTC))
    wait_until(lambda: len(rec.events) == 4)
    assert summary(rec.events)[-1] == ("end", "short.wav", "eof")


def test_clear_pending_keeps_current_and_drops_the_rest(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    a = make_wav(tmp_path / "a.wav", 0.3)
    b = make_wav(tmp_path / "b.wav", 0.1)
    c = make_wav(tmp_path / "c.wav", 0.1)
    for f in (a, b, c):
        backend.enqueue(f)
    assert next_event(rec) == Started(a, datetime(2026, 1, 1, tzinfo=UTC))
    assert backend.clear_pending() == 2
    assert backend.queued() == 0 and backend.current() == a
    assert summary([next_event(rec)]) == [("end", "a.wav", "eof")]
    wait_until(backend.idle)
    # Tras vaciar, la cola sigue funcionando (y la correspondencia FIFO se mantiene)
    d = make_wav(tmp_path / "d.wav", 0.1)
    backend.enqueue(d)
    wait_until(lambda: len(rec.events) == 4)
    assert summary(rec.events) == [
        ("start", "a.wav", ""), ("end", "a.wav", "eof"),
        ("start", "d.wav", ""), ("end", "d.wav", "eof"),
    ]
    assert backend.clear_pending() == 0


def test_clear_pending_then_skip_gives_way_to_interrupt(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    """Lo que hace la emisora al saltar la señal horaria: vaciar, encolar, cortar."""
    long = make_wav(tmp_path / "long.wav", 30)
    queued = make_wav(tmp_path / "queued.wav", 0.1)
    signal = make_wav(tmp_path / "signal.wav", 0.1)
    backend.enqueue(long)
    backend.enqueue(queued)
    assert next_event(rec) == Started(long, datetime(2026, 1, 1, tzinfo=UTC))
    backend.clear_pending()
    backend.enqueue(signal)
    backend.skip()
    wait_until(lambda: len(rec.events) == 4)
    assert summary(rec.events) == [
        ("start", "long.wav", ""), ("end", "long.wav", "skipped"),
        ("start", "signal.wav", ""), ("end", "signal.wav", "eof"),
    ]


def test_skip_last_file_unblocks_play(backend: MpvIpcBackend, tmp_path: Path) -> None:
    long = make_wav(tmp_path / "long.wav", 30)
    t = threading.Thread(target=backend.play, args=(long,))
    t.start()
    wait_until(lambda: backend.current() == long)
    backend.skip()
    t.join(timeout=TIMEOUT)
    assert not t.is_alive()
    assert backend.current() is None


def test_skip_without_current_is_noop(backend: MpvIpcBackend) -> None:
    backend.start()
    backend.skip()
    assert backend.alive()


def test_missing_file_ends_with_error(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    ok = make_wav(tmp_path / "ok.wav", 0.1)
    backend.enqueue(tmp_path / "missing.wav")
    backend.play(ok)
    assert summary(rec.events) == [
        ("start", "missing.wav", ""), ("end", "missing.wav", "error"),
        ("start", "ok.wav", ""), ("end", "ok.wav", "eof"),
    ]


def test_watchdog_relaunches_and_keeps_pending(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    a = make_wav(tmp_path / "a.wav", 0.1)
    crash = make_wav(tmp_path / "crash.wav", 0.1)
    b = make_wav(tmp_path / "b.wav", 0.1)
    c = make_wav(tmp_path / "c.wav", 0.1)
    backend.enqueue(a)
    backend.enqueue(crash)
    backend.enqueue(b)
    first_pid = backend.pid
    backend.play(c)  # sobrevive a la caída y termina tras relanzar
    assert backend.restarts == 1
    assert backend.pid != first_pid
    assert backend.alive()
    assert summary(rec.events) == [
        ("start", "a.wav", ""), ("end", "a.wav", "eof"),
        ("start", "crash.wav", ""), ("end", "crash.wav", "error"),
        ("start", "b.wav", ""), ("end", "b.wav", "eof"),
        ("start", "c.wav", ""), ("end", "c.wav", "eof"),
    ]


def test_watchdog_relaunches_after_kill(
    backend: MpvIpcBackend, rec: EventRecorder, tmp_path: Path
) -> None:
    long = make_wav(tmp_path / "long.wav", 30)
    nxt = make_wav(tmp_path / "next.wav", 0.1)
    backend.enqueue(long)
    backend.enqueue(nxt)
    assert isinstance(next_event(rec), Started)
    pid = backend.pid
    assert pid is not None
    os.kill(pid, 9)
    ended = next_event(rec)
    assert isinstance(ended, Ended) and ended.path == long and ended.reason == "error"
    assert next_event(rec) == Started(nxt, datetime(2026, 1, 1, tzinfo=UTC))
    assert backend.restarts == 1
    assert backend.alive()


def test_close_cleans_up(tmp_path: Path) -> None:
    rec = EventRecorder()
    b = MpvIpcBackend([sys.executable, str(FAKE_MPV)])
    b.add_listener(rec)
    long = make_wav(tmp_path / "long.wav", 30)
    b.enqueue(long)
    b.enqueue(make_wav(tmp_path / "pending.wav", 0.1))
    assert isinstance(next_event(rec), Started)
    proc = b._proc
    sock = b.socket_path
    assert proc is not None and sock is not None
    blocked = threading.Thread(target=b.play, args=(make_wav(tmp_path / "x.wav", 0.1),))
    blocked.start()
    b.close()
    blocked.join(timeout=TIMEOUT)
    assert not blocked.is_alive()
    assert proc.poll() is not None  # recogido: sin zombi
    assert not sock.exists() and not sock.parent.exists()
    assert not b.alive()
    assert summary(rec.events) == [("start", "long.wav", ""), ("end", "long.wav", "skipped")]
    b.close()  # idempotente
    with pytest.raises(MpvError):
        b.enqueue(long)


def test_missing_binary_raises(tmp_path: Path) -> None:
    b = MpvIpcBackend(str(tmp_path / "no-such-mpv"))
    with pytest.raises(MpvError):
        b.play(tmp_path / "a.wav")
    b.close()
