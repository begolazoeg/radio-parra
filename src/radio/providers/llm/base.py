"""
Protocolo base para proveedores LLM.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from radio.core.models import LLMResult


@runtime_checkable
class LLM(Protocol):
    """Interfaz mínima que todo proveedor LLM debe implementar."""

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        json_schema: Optional[dict[str, Any]] = None,
        max_tokens: int = 1000,
    ) -> LLMResult:
        ...
