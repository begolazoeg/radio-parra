"""
Caché de TTS (§4.1): no pagar (ni sintetizar) dos veces lo mismo.

``CachedTTS(inner, cache_dir)`` envuelve cualquier ``TTS``. La clave es
``sha256(texto + voice.id + proveedor + provider_voice_id)`` (más la
``cache_variant`` del proveedor, si la tiene: modelo y formato de la nube o
argumentos de Piper, para no servir un audio hecho con otra configuración).

- Acierto: se copia el audio cacheado a ``out_path`` (no se llama al proveedor) y
  se devuelve ``AudioInfo(cached=True)``: el productor no lo suma a ``tts_chars``.
- Fallo: se sintetiza en ``out_path`` y se guarda una copia en la caché, con
  escritura atómica (temporal + ``os.replace``) y un ``.json`` con la duración.
- ``hits`` / ``misses`` cuentan aciertos y fallos.

La caché es regenerable: se puede borrar ``cache_dir`` en cualquier momento.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from radio.core.models import AudioInfo, Voice
from radio.providers.tts.base import TTS


def provider_name(tts: object) -> str:
    """Nombre del proveedor (atributo ``name`` o, si no tiene, el de la clase)."""
    return str(getattr(tts, "name", "") or type(tts).__name__)


def cache_key(text: str, voice: Voice, provider: str, variant: str = "") -> str:
    """sha256 hexadecimal de texto + voz + proveedor + id de la voz en el proveedor."""
    payload = json.dumps(
        [text, voice.id, provider, voice.provider_voice_id, variant], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CachedTTS:
    """``TTS`` con caché en disco delante de ``inner``."""

    def __init__(self, inner: TTS, cache_dir: Path | str) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.name = provider_name(inner)
        self.hits = 0
        self.misses = 0

    def key(self, text: str, voice: Voice) -> str:
        variant = str(getattr(self.inner, "cache_variant", "") or "")
        return cache_key(text, voice, self.name, variant)

    def _paths(self, key: str) -> tuple[Path, Path]:
        base = self.cache_dir / key[:2] / key
        return base.with_suffix(".audio"), base.with_suffix(".json")

    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo:
        key = self.key(text, voice)
        audio, meta = self._paths(key)
        cached = self._read_meta(meta) if audio.is_file() else None
        if cached is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(audio, out_path)
            self.hits += 1
            return AudioInfo(path=out_path, duration_s=cached, cached=True)

        self.misses += 1
        info = self.inner.synthesize(text, voice, out_path)
        self._store(info, audio, meta)
        return info

    @staticmethod
    def _read_meta(meta: Path) -> float | None:
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            duration = float(data["duration_s"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return duration if duration > 0 else None

    @staticmethod
    def _store(info: AudioInfo, audio: Path, meta: Path) -> None:
        """Copia atómica del audio y su duración a la caché (la caché es opcional)."""
        audio.parent.mkdir(parents=True, exist_ok=True)
        tmp_audio = audio.with_suffix(".audio.tmp")
        tmp_meta = meta.with_suffix(".json.tmp")
        try:
            shutil.copyfile(info.path, tmp_audio)
            tmp_meta.write_text(json.dumps({"duration_s": info.duration_s}), encoding="utf-8")
            os.replace(tmp_audio, audio)
            os.replace(tmp_meta, meta)      # el .json al final: marca la entrada completa
        except OSError:
            tmp_audio.unlink(missing_ok=True)
            tmp_meta.unlink(missing_ok=True)
