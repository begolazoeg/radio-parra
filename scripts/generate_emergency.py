#!/usr/bin/env python3
"""
Genera el bucle de emergencia de Radio Parra (ARCHITECTURE.md §8, peldaño 5).

Sintetiza, solo con la biblioteca estándar, un colchón suave de acordes (Cmaj7 →
Am7 → Fmaj7 → G6) con campanitas al inicio de cada acorde, fundido de entrada y de
salida para que el bucle empalme sin clics. Salida: WAV mono, 22,05 kHz, 16 bits.

Es determinista (sin azar): regenerarlo da el mismo audio. El resultado se versiona
en ``assets/emergency/emergency_loop.wav`` porque la emisora debe tenerlo *siempre*,
aunque no haya red ni herramientas (§1 inv. 3). Licencia: CC0 (ver assets/README.md).

Uso::

    python scripts/generate_emergency.py                 # escribe el asset versionado
    python scripts/generate_emergency.py -o /tmp/x.wav --seconds 10
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from pathlib import Path

RATE = 22050
DEFAULT_SECONDS = 24.0
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "assets" / "emergency" / "emergency_loop.wav"

# Acordes (frecuencias en Hz): voces graves y medias, registro cálido
CHORDS: list[list[float]] = [
    [130.81, 196.00, 246.94, 329.63],  # Cmaj7: C3 G3 B3 E4
    [110.00, 196.00, 261.63, 329.63],  # Am7:   A2 G3 C4 E4
    [87.31, 174.61, 220.00, 329.63],  # Fmaj7: F2 F3 A3 E4
    [98.00, 196.00, 246.94, 329.63],  # G6:    G2 G3 B3 E4
]
# Nota de la campanita al empezar cada acorde
CHIMES: list[float] = [783.99, 659.25, 880.00, 587.33]  # G5 E5 A5 D5

PEAK = 0.45  # pico final respecto a fondo de escala (≈ −7 dBFS): suave
FADE_IN_S = 2.0
FADE_OUT_S = 3.0


def _pad_envelope(t: float, start: float, length: float, overlap: float) -> float:
    """Ventana de un acorde: sube y baja en coseno alzado, solapando con el vecino."""
    local = t - start
    if local < -overlap / 2 or local > length + overlap / 2:
        return 0.0
    if local < overlap / 2:
        x = (local + overlap / 2) / overlap
        return 0.5 - 0.5 * math.cos(math.pi * x)
    if local > length - overlap / 2:
        x = (length + overlap / 2 - local) / overlap
        return 0.5 - 0.5 * math.cos(math.pi * x)
    return 1.0


def synthesize(seconds: float = DEFAULT_SECONDS, rate: int = RATE) -> array[int]:
    """Devuelve las muestras PCM de 16 bits (mono) del bucle."""
    n = int(seconds * rate)
    chord_len = seconds / len(CHORDS)
    overlap = min(1.5, chord_len / 2)
    two_pi = 2 * math.pi
    out = [0.0] * n
    for i in range(n):
        t = i / rate
        value = 0.0
        # Colchón: cada voz con un leve desafinado (coro) y un trémolo muy lento
        for c, chord in enumerate(CHORDS):
            env = _pad_envelope(t, c * chord_len, chord_len, overlap)
            if env == 0.0:
                continue
            tremolo = 0.85 + 0.15 * math.sin(two_pi * 0.2 * t + c)
            voices = 0.0
            for v, f in enumerate(chord):
                weight = 1.0 / (1 + v * 0.6)
                voices += weight * (
                    math.sin(two_pi * f * t) + 0.5 * math.sin(two_pi * f * 1.003 * t + v)
                )
            value += env * tremolo * voices * 0.12
        # Campanitas: parciales inarmónicos con caída exponencial
        c = min(int(t // chord_len), len(CHIMES) - 1)
        local = t - c * chord_len - 0.3
        if local >= 0:
            f = CHIMES[c]
            decay = math.exp(-local * 1.6)
            attack = min(1.0, local / 0.01)
            value += 0.18 * attack * decay * (
                math.sin(two_pi * f * local)
                + 0.35 * math.sin(two_pi * f * 2.76 * local) * math.exp(-local * 2.5)
                + 0.15 * math.sin(two_pi * f * 5.4 * local) * math.exp(-local * 4.0)
            )
        # Fundidos globales: el bucle empieza y acaba en silencio (empalme sin clic)
        if t < FADE_IN_S:
            value *= math.sin(0.5 * math.pi * t / FADE_IN_S) ** 2
        remaining = seconds - t
        if remaining < FADE_OUT_S:
            value *= math.sin(0.5 * math.pi * max(remaining, 0.0) / FADE_OUT_S) ** 2
        out[i] = value
    peak = max((abs(v) for v in out), default=0.0) or 1.0
    scale = PEAK * 32767 / peak
    return array("h", (int(round(v * scale)) for v in out))


def write_wav(path: Path, samples: array[int], rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.byteorder == "big":
        samples = array("h", samples)
        samples.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    args = parser.parse_args(argv)
    samples = synthesize(args.seconds)
    write_wav(args.out, samples)
    print(f"{args.out}: {len(samples) / RATE:.1f} s, {args.out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
