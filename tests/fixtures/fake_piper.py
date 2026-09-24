"""
Doble del binario ``piper`` para tests (sin modelos ni audio real).

Uso igual que Piper: ``echo texto | fake_piper --model X.onnx --output_file out.wav``.
Escribe un WAV de silencio de ``len(texto) / 15`` segundos. Con
``FAKE_PIPER_FAIL=1`` sale con código 1 y un mensaje en stderr. Guarda los
argumentos recibidos en ``<out>.args.json`` para que el test los compruebe.
"""

from __future__ import annotations

import json
import os
import sys
import wave


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_PIPER_FAIL") == "1":
        sys.stderr.write("fake piper: fallo simulado\n")
        return 1
    args = dict(zip(argv[::2], argv[1::2], strict=False))
    model, out = args.get("--model"), args.get("--output_file")
    if not model or not out:
        sys.stderr.write("faltan --model/--output_file\n")
        return 2
    text = sys.stdin.read()
    rate = 22050
    frames = int(rate * max(0.1, len(text.strip()) / 15))
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)
    with open(out + ".args.json", "w", encoding="utf-8") as f:
        json.dump({"argv": argv, "text": text}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
