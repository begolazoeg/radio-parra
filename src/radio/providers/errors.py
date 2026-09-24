"""
Errores comunes de los proveedores (LLM y TTS).

Jerarquía (todas son ``RuntimeError`` para que el runner las registre en
``producer_runs.error`` sin tratamiento especial, §8):

- ``ProviderError``: base. ``retryable`` indica si reintentar más tarde tiene
  sentido (límite de tasa, caída del servicio, red) o no (petición inválida,
  credenciales, rechazo del modelo). El SDK/cliente ya reintenta por su cuenta
  unas pocas veces; ``retryable`` es para la *siguiente pasada del timer*.
- ``ProviderNotAvailable``: el proveedor no se puede usar en esta máquina (no
  está implementado, falta el binario, el modelo de voz o la clave). Lo lanza
  ``build_llm``/``build_tts`` al construir, o el proveedor al usarse.
- ``LLMError`` y sus subclases: fallos de una llamada al LLM.
- ``TTSError``: fallos de una síntesis.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Fallo de un proveedor externo. ``retryable``: tiene sentido reintentar más tarde."""

    retryable: bool = False


class ProviderNotAvailable(ProviderError):
    """El proveedor configurado no existe, no está implementado o le falta algo local."""


# ── LLM ───────────────────────────────────────────────────────────────────────

class LLMError(ProviderError):
    """
    Fallo de una llamada al LLM. Si la llamada llegó a facturarse (rechazo del
    modelo, respuesta cortada), ``cost_eur`` y los tokens dicen cuánto costó, para
    que el productor lo sume igualmente a ``producer_runs``.
    """

    def __init__(
        self,
        message: str,
        *,
        cost_eur: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        super().__init__(message)
        self.cost_eur = cost_eur
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class LLMRetryableError(LLMError):
    """Fallo transitorio (tasa, servidor, red): se reintenta en la siguiente pasada."""

    retryable = True


class LLMRateLimited(LLMRetryableError):
    """HTTP 429: límite de tasa del proveedor."""


class LLMServerError(LLMRetryableError):
    """HTTP 5xx (o 408/409): error del lado del proveedor."""


class LLMConnectionError(LLMRetryableError):
    """Sin conexión o tiempo de espera agotado."""


class LLMBadRequest(LLMError):
    """HTTP 400/413/422: la petición es inválida (parámetros, esquema, tamaño)."""


class LLMAuthError(LLMError):
    """HTTP 401/403 o credenciales no encontradas."""


class LLMNotFound(LLMError):
    """HTTP 404: modelo o endpoint inexistente (¿id de modelo mal escrito?)."""


class LLMRefusal(LLMError):
    """El modelo se negó a responder (``stop_reason == "refusal"``)."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        cost_eur: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        super().__init__(
            message, cost_eur=cost_eur, input_tokens=input_tokens, output_tokens=output_tokens
        )
        self.category = category


class LLMTruncated(LLMError):
    """La respuesta se cortó por ``max_tokens``: guion incompleto, no se usa."""


class LLMInvalidOutput(LLMError):
    """Se pidió JSON con esquema y el texto devuelto no es JSON válido."""


# ── TTS ───────────────────────────────────────────────────────────────────────

class TTSError(ProviderError):
    """Fallo de una síntesis de voz."""


class TTSRetryableError(TTSError):
    """Fallo transitorio del TTS en la nube (tasa, servidor, red)."""

    retryable = True
