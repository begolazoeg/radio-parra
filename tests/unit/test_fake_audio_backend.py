"""
Tests del backend de audio falso con eventos (simulación determinista).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from radio.core.clock import FakeClock
from radio.providers.audio import (
    Ended,
    EventRecorder,
    FakeEventBackend,
    NullAudioBackend,
    PlayerEvent,
    QueueingAudioBackend,
    Started,
)

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
A, B, C = Path("/a.mp3"), Path("/b.mp3"), Path("/c.mp3")
DURATIONS = {A: 10.0, B: 20.0, C: 30.0}


def setup() -> tuple[FakeClock, FakeEventBackend, EventRecorder]:
    clock = FakeClock(T0)
    audio = FakeEventBackend(clock, duration_of=DURATIONS.__getitem__, advance=clock.advance)
    rec = EventRecorder()
    audio.add_listener(rec)
    return clock, audio, rec


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_is_queueing_backend() -> None:
    assert isinstance(FakeEventBackend(FakeClock(T0)), QueueingAudioBackend)


def test_enqueue_and_finish_advance_clock() -> None:
    clock, audio, rec = setup()
    audio.enqueue(A)
    audio.enqueue(B)
    assert audio.current() == A and audio.queued() == 1
    audio.finish()
    audio.finish()
    expected: list[PlayerEvent] = [
        Started(A, at(0)), Ended(A, at(10), "eof"),
        Started(B, at(10)), Ended(B, at(30), "eof"),
    ]
    assert rec.events == expected
    assert audio.current() is None and clock.now() == at(30)


def test_play_finishes_everything_up_to_the_file() -> None:
    _, audio, rec = setup()
    audio.enqueue(A)
    audio.play(B)
    audio.enqueue(C)
    assert [type(e).__name__ for e in rec.events] == ["Started", "Ended", "Started", "Ended", "Started"]
    assert audio.current() == C


def test_play_same_path_twice() -> None:
    _, audio, rec = setup()
    audio.enqueue(A)
    audio.play(A)
    assert rec.events[-1] == Ended(A, at(20), "eof")
    assert audio.current() is None


def test_skip_and_crash() -> None:
    _, audio, rec = setup()
    audio.enqueue(A)
    audio.enqueue(B)
    audio.enqueue(C)
    audio.skip()
    audio.crash()
    assert rec.events == [
        Started(A, at(0)), Ended(A, at(0), "skipped"),
        Started(B, at(0)), Ended(B, at(0), "error"),
        Started(C, at(0)),
    ]
    assert audio.restarts == 1
    audio.close()
    assert rec.events[-1] == Ended(C, at(0), "skipped")
    assert not audio.alive()


def test_listener_can_refill_queue() -> None:
    """Patrón de la emisora: al terminar algo, rellenar el lookahead desde el oyente."""
    _, audio, _ = setup()
    feed = [B, C]

    def refill(ev: PlayerEvent) -> None:
        if isinstance(ev, Ended) and feed:
            audio.enqueue(feed.pop(0))

    audio.add_listener(refill)
    audio.play(A)
    assert audio.current() == B
    audio.finish()
    assert audio.current() == C


def test_null_backend_still_records_calls() -> None:
    null = NullAudioBackend()
    null.play(A)
    null.enqueue(B)
    null.skip()
    assert [c["action"] for c in null.calls] == ["play", "enqueue", "skip"]


def test_clear_pending_keeps_current() -> None:
    _, audio, rec = setup()
    for p in (A, B, C):
        audio.enqueue(p)
    assert audio.clear_pending() == 2
    assert audio.current() == A and audio.queued() == 0
    audio.finish()
    assert rec.events == [Started(A, at(0)), Ended(A, at(10), "eof")]


def test_finish_advances_only_what_is_left() -> None:
    clock, audio, rec = setup()
    audio.enqueue(C)                     # 30 s
    assert audio.time_left() == 30.0
    clock.advance(12)                    # alguien mueve el reloj a mitad (temporizador)
    assert audio.time_left() == 18.0
    audio.finish()
    assert clock.now() == at(30)
    assert rec.events[-1] == Ended(C, at(30), "eof")
    assert audio.time_left() is None
