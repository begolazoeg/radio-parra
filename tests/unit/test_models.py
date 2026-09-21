"""
Tests unitarios para los modelos de dominio.
"""

from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

from radio.core.models import Segment, Voice, SegmentStatus


# ── test_segment_creation ─────────────────────────────────────────────────────

def test_segment_creation() -> None:
    """Crea un Segment con todos los campos y verifica sus valores."""
    seg = Segment(
        id="01HZZZZZZZZZZZZZZZZZZZZZZZZ",
        kind="factual",
        status="pending",
        created_at=datetime(2024, 1, 1, 12, 0, 0),
        title="Test factual segment",
        duration_s=120.0,
        audio_path=Path("/tmp/test.wav"),
        producer="factual_producer",
        source_url="https://example.com/article",
        script="Hoy en las noticias...",
        voice_id="host_news",
        tags=("news", "science"),
    )
    assert seg.id == "01HZZZZZZZZZZZZZZZZZZZZZZZZ"
    assert seg.kind == "factual"
    assert seg.status == "pending"
    assert seg.duration_s == 120.0
    assert seg.tags == ("news", "science")


def test_segment_is_frozen() -> None:
    """Los Segment son inmutables (frozen=True)."""
    seg = Segment(
        id="01HZZZZZZZZZZZZZZZZZZZZZZZZ",
        kind="music",
        status="ready",
        created_at=datetime(2024, 1, 1),
        title="Song",
        duration_s=180.0,
        audio_path=None,
        producer="music_producer",
    )
    with pytest.raises((FrozenInstanceError, AttributeError)):
        seg.status = "done"  # type: ignore[misc]


# ── test_segment_status_values ────────────────────────────────────────────────

def test_segment_status_values() -> None:
    """Los 5 status literales son válidos como SegmentStatus."""
    valid_statuses: list[SegmentStatus] = ["pending", "ready", "playing", "done", "error"]
    for status in valid_statuses:
        seg = Segment(
            id=f"id_{status}",
            kind="jingle",
            status=status,
            created_at=datetime(2024, 1, 1),
            title="Jingle",
            duration_s=5.0,
            audio_path=None,
            producer="jingle_producer",
        )
        assert seg.status == status


# ── test_voice_requires_consent ───────────────────────────────────────────────

def test_voice_requires_consent() -> None:
    """Voice con consent=True es válida; consent=False debe ser rechazado."""
    # Voz válida
    voice = Voice(
        id="host_main",
        name="Locutora Principal",
        provider="elevenlabs",
        consent=True,
    )
    assert voice.consent is True


def test_voice_without_consent_is_flagged() -> None:
    """
    Voice con consent=False se puede crear como dataclass (frozen),
    pero VoicesConfig debe rechazarla en validación.
    """
    from pydantic import ValidationError
    from radio.core.config import VoiceEntry, VoicesConfig

    with pytest.raises(ValidationError):
        VoiceEntry(
            id="bad_voice",
            name="Sin consentimiento",
            provider="elevenlabs",
            consent=False,
        )
