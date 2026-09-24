"""
El bucle de emergencia está versionado y siempre presente (ARCHITECTURE.md §1 inv. 3, §8).
"""

from __future__ import annotations

import importlib.util
import wave
from array import array
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "assets" / "emergency" / "emergency_loop.wav"
SCRIPT = ROOT / "scripts" / "generate_emergency.py"


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_emergency", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_emergency_loop_is_committed_and_valid() -> None:
    assert ASSET.is_file(), "falta assets/emergency/emergency_loop.wav (scripts/generate_emergency.py)"
    assert ASSET.stat().st_size < 1_500_000
    with wave.open(str(ASSET), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 22050
        seconds = w.getnframes() / w.getframerate()
        samples = array("h", w.readframes(w.getnframes()))
    assert 20.0 <= seconds <= 30.0
    peak = max(abs(s) for s in samples)
    assert 0.2 * 32767 < peak < 0.9 * 32767  # suena, sin saturar
    # Empieza y acaba en silencio: el bucle empalma sin clic
    assert max(abs(s) for s in samples[:10]) < 50
    assert max(abs(s) for s in samples[-10:]) < 50


def test_generator_is_deterministic(tmp_path: Path) -> None:
    gen = load_generator()
    a = gen.synthesize(1.0)
    assert a == gen.synthesize(1.0)
    out = tmp_path / "loop.wav"
    gen.write_wav(out, a)
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() == 22050
