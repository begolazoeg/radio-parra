"""
Implementación fake del proveedor LLM para tests.
Devuelve fixtures configurables y registra todas las llamadas.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from radio.core.models import LLMResult


class FakeLLM:
    """
    LLM falso para tests.
    - fixture: dict que se serializa como respuesta JSON, o str literal.
    - Si fixture es None, devuelve un JSON de ejemplo genérico.
    """

    def __init__(self, fixture: Any = None) -> None:
        self.fixture = fixture
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        json_schema: Optional[dict[str, Any]] = None,
        max_tokens: int = 1000,
    ) -> LLMResult:
        # Registra la llamada para aserciones en tests
        self.calls.append(
            {
                "system": system,
                "user": user,
                "temperature": temperature,
                "json_schema": json_schema,
                "max_tokens": max_tokens,
            }
        )

        if self.fixture is None:
            text = json.dumps({"result": "fake"})
        elif isinstance(self.fixture, str):
            text = self.fixture
        else:
            text = json.dumps(self.fixture)

        return LLMResult(
            text=text,
            input_tokens=len(system.split()) + len(user.split()),
            output_tokens=len(text.split()),
        )
