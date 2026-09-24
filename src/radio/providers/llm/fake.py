"""
Implementación fake del proveedor LLM para tests.
Devuelve fixtures configurables y registra todas las llamadas.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from radio.core.models import LLMResult


class FakeLLM:
    """
    LLM falso para tests.

    - ``fixture``: dict que se serializa como respuesta JSON, o str literal.
      Si es None, devuelve un JSON de ejemplo genérico.
    - ``script``: respuestas en orden, una por llamada (dict, str o una excepción,
      que se lanza); agotada la lista, se repite la última. Tiene prioridad sobre
      ``fixture``. Sirve para simular un LLM que a veces responde mal.
    - ``cost_eur``: coste que declara cada llamada (0 por defecto), para probar la
      contabilidad de ``producer_runs``.
    """

    def __init__(
        self,
        fixture: Any = None,
        *,
        script: Sequence[Any] | None = None,
        cost_eur: float = 0.0,
        model: str = "fake",
    ) -> None:
        self.fixture = fixture
        self.script = list(script) if script is not None else None
        self.cost_eur = cost_eur
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        if self.script:
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return self.fixture

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        json_schema: dict[str, Any] | None = None,
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

        answer = self._next()
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            text = json.dumps({"result": "fake"})
        elif isinstance(answer, str):
            text = answer
        else:
            text = json.dumps(answer, ensure_ascii=False)

        return LLMResult(
            text=text,
            input_tokens=len(system.split()) + len(user.split()),
            output_tokens=len(text.split()),
            model=self.model,
            cost_eur=self.cost_eur,
        )
