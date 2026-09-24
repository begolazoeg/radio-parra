"""
Tests unitarios para los modelos de dominio (§3.1).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest

from radio.core.models import STATUSES, Segment, Status, StockView, Voice

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def seg(seg_id: str, kind: str = "music", **kw: object) -> Segment:
    data: dict[str, object] = {
        "id": seg_id, "kind": kind, "factual": False, "path": Path(f"/x/{seg_id}.mp3"),
        "duration_s": 100.0, "created_at": T0, "producer": "test",
    }
    data.update(kw)
    return Segment(**data)  # type: ignore[arg-type]


def test_segment_fields_match_architecture() -> None:
    assert [f.name for f in fields(Segment)] == [
        "id", "kind", "factual", "path", "duration_s", "created_at", "producer",
        "status", "expires_at", "priority", "parent_id", "voice_id",
        "prompt_version", "summary", "meta",
    ]


def test_segment_defaults() -> None:
    s = seg("a")
    assert s.status == "ready"
    assert s.expires_at is None and s.priority == 0 and s.parent_id is None
    assert s.voice_id is None and s.prompt_version is None and s.summary is None
    assert s.meta == {}
    # meta no se comparte entre instancias
    assert seg("b").meta is not s.meta


def test_segment_is_frozen() -> None:
    s = seg("a")
    with pytest.raises(FrozenInstanceError):
        s.status = "retired"  # type: ignore[misc]


def test_status_values() -> None:
    assert set(get_args(Status)) == {"ready", "pending_review", "quarantined", "expired", "retired"}
    assert STATUSES == set(get_args(Status))


def test_title_and_tags_from_meta() -> None:
    s = seg("a", meta={"title": "Tema", "tags": ["artist:x", "source:tiny_desk"]})
    assert s.title == "Tema"
    assert s.tags == ("artist:x", "source:tiny_desk")
    bare = seg("b", kind="jingle")
    assert bare.title == "jingle" and bare.tags == ()


def test_is_live() -> None:
    assert seg("a").is_live(T0)
    assert seg("a", expires_at=T0 + timedelta(seconds=1)).is_live(T0)
    assert not seg("a", expires_at=T0).is_live(T0)
    assert not seg("a", status="retired").is_live(T0)


def test_stock_view_filters_and_groups() -> None:
    view = StockView.from_segments(
        [
            seg("m1"),
            seg("m2"),
            seg("t1", "time_signal", expires_at=T0 + timedelta(minutes=5)),
            seg("t0", "time_signal", expires_at=T0),                 # caducado
            seg("q", "weather", status="quarantined"),
        ],
        T0,
    )
    assert view.count("music") == 2
    assert [s.id for s in view.get("time_signal")] == ["t1"]
    assert view.count("weather") == 0 and view.get("weather") == ()
    assert view.kinds() == {"music", "time_signal"}
    with pytest.raises(TypeError):
        view.by_kind["x"] = ()  # type: ignore[index]


def test_empty_stock_view() -> None:
    view = StockView()
    assert view.kinds() == set() and view.count("music") == 0


def test_voice_model() -> None:
    v = Voice(
        id="locutor_principal", role="host", provider="cloud", provider_voice_id="abc",
        consent=True, consent_note="Voz sintética genérica",
    )
    assert v.language == "es" and v.universe is None and v.description == ""
