"""
Proveedor LLM con la API de Claude (§4.1), usando el SDK oficial ``anthropic``.

Modelo por defecto: ``claude-sonnet-5`` (decisión de la dueña, 2026-09-24),
configurable en ``station.yaml → providers.llm.model``.

Credenciales: ``anthropic.Anthropic()`` las resuelve solo, en este orden:
``ANTHROPIC_API_KEY`` → ``ANTHROPIC_AUTH_TOKEN`` → perfil de ``ant auth login``
(``~/.config/anthropic/``) → variables de *workload identity*. Nunca se guardan en
el repo (``.env`` fuera de git, systemd lo carga con ``EnvironmentFile``).

Mapeo de parámetros del protocolo ``LLM`` (invariante 6: la firma no cambia):

- ``temperature``: Claude Sonnet 5 (y los modelos de ``NO_SAMPLING_MODELS``)
  **rechaza** ``temperature``/``top_p``/``top_k`` con un 400. Para esos modelos el
  argumento se acepta y se ignora (se registra en ``debug``); la diferencia
  factual/ficción la marcan el prompt y la plantilla, no la temperatura. Para el
  resto de modelos se envía tal cual (en ``extra_body``: el SDK 1.x ya no la
  expone como argumento de ``messages.create``).
- ``json_schema``: salida estructurada con
  ``output_config={"format": {"type": "json_schema", "schema": ...}}``. El esquema
  debe cerrar sus objetos (``"additionalProperties": false`` y ``required``); el
  texto devuelto es JSON válido y aquí se comprueba con ``json.loads``. No se usa el
  parámetro obsoleto ``output_format`` ni *prefill* del asistente (400 en esta
  familia de modelos).
- ``max_tokens``: tope de la respuesta. Con pensamiento adaptativo los tokens de
  pensamiento cuentan dentro de ``max_tokens``, así que se sube a
  ``min_max_tokens_adaptive`` (2000 por defecto) si se pide menos.

Pensamiento (``providers.llm.extra.thinking``):

- ``"adaptive"`` (por defecto) con ``effort: "low"``: el modelo piensa poco o nada
  en guiones cortos, pero sigue pensando cuando le hace falta. Se elige frente a
  ``"disabled"`` porque, sin pensamiento, estos modelos a veces vuelcan etiquetas de
  razonamiento en el texto visible, y aquí el texto se *locuta*: un guion con
  ``<thinking>`` sería un fallo audible. El coste extra es pequeño (effort bajo).
- ``"disabled"``: ``thinking={"type": "disabled"}`` (Sonnet 5 lo acepta). Más
  barato y predecible; útil si siempre se pide JSON con esquema.

Respuesta: se concatenan los bloques ``text`` (los de pensamiento se ignoran). Un
``stop_reason`` ``"refusal"`` lanza ``LLMRefusal`` (con ``stop_details.category``
si viene) y ``"max_tokens"`` lanza ``LLMTruncated``; ambos llevan el coste de la
llamada, que se factura igualmente.

Errores del SDK → jerarquía propia (``radio.providers.errors``), de lo más
específico a lo más general; el SDK ya reintenta 408/409/429/5xx y errores de red
``max_retries`` veces con espera exponencial antes de que lleguen aquí.

Coste: tabla ``PRICES_USD_PER_MTOK`` (cacheada 2026-06; **verificar** en la página
de precios de Anthropic antes de fiarse del presupuesto), sobrescribible con
``extra.prices``. El presupuesto va en euros: ``extra.usd_to_eur`` (aproximado,
0.92 por defecto; actualizarlo a mano de vez en cuando).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from radio.core.models import LLMResult
from radio.providers.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMConnectionError,
    LLMError,
    LLMInvalidOutput,
    LLMNotFound,
    LLMRateLimited,
    LLMRefusal,
    LLMServerError,
    LLMTruncated,
    ProviderNotAvailable,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"

# Modelos que devuelven 400 si se envía temperature/top_p/top_k. Se compara por
# prefijo para cubrir variantes del mismo id.
NO_SAMPLING_MODELS: frozenset[str] = frozenset({
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-opus-4-7",
    "claude-opus-4-8",
})

# Precios en USD por millón de tokens (cacheados 2026-06; VERIFICAR antes de usar
# el presupuesto como límite duro). ``cache_read``/``cache_write`` son las tarifas
# de lectura y escritura de la caché de prompts (≈0,1× y ≈1,25× la entrada).
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}

# Cambio aproximado USD → EUR (configurable en extra.usd_to_eur)
DEFAULT_USD_TO_EUR = 0.92

# Con pensamiento adaptativo, suelo de max_tokens (el pensamiento cuenta dentro)
DEFAULT_MIN_MAX_TOKENS_ADAPTIVE = 2000

ThinkingMode = Literal["adaptive", "disabled"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# Variables de entorno que el SDK usa como credenciales (sin red)
_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
_WIF_ENV = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)


def _matches(model: str, ids: frozenset[str] | Mapping[str, Any]) -> str | None:
    """Id conocido que coincide con ``model`` (exacto o el prefijo más largo)."""
    if model in ids:
        return model
    candidates = [m for m in ids if model.startswith(m + "-")]
    return max(candidates, key=len) if candidates else None


def rejects_sampling(model: str) -> bool:
    """¿El modelo rechaza temperature/top_p/top_k?"""
    return _matches(model, NO_SAMPLING_MODELS) is not None


def detect_credentials(env: Mapping[str, str] | None = None) -> str | None:
    """
    Fuente de credenciales de Claude que se ve desde aquí, sin red: nombre de la
    variable de entorno, ``"perfil ant"`` si hay un directorio de configuración de
    ``ant auth login``, o None. Es una pista (para ``radio doctor`` y el registro):
    no garantiza que la clave sea válida.
    """
    env = os.environ if env is None else env
    for name in _CREDENTIAL_ENV:
        if env.get(name, "").strip():
            return name
    if all(env.get(name, "").strip() for name in _WIF_ENV) and (
        env.get("ANTHROPIC_IDENTITY_TOKEN") or env.get("ANTHROPIC_IDENTITY_TOKEN_FILE")
    ):
        return "workload identity"
    config_dir = env.get("ANTHROPIC_CONFIG_DIR") or str(Path.home() / ".config" / "anthropic")
    path = Path(config_dir)
    if path.is_dir() and any(path.iterdir()):
        return "perfil ant"
    return None


class ClaudeLLM:
    """
    ``LLM`` sobre ``client.messages.create`` (ver el docstring del módulo).

    ``client`` permite inyectar un cliente (tests con *mocks*, sin red); si es
    None se crea ``anthropic.Anthropic(timeout=..., max_retries=...)`` en la primera
    llamada.
    """

    name = "claude"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: Any = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        thinking: ThinkingMode = "adaptive",
        effort: Effort | None = "low",
        min_max_tokens_adaptive: int = DEFAULT_MIN_MAX_TOKENS_ADAPTIVE,
        usd_to_eur: float = DEFAULT_USD_TO_EUR,
        prices: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        if thinking not in ("adaptive", "disabled"):
            raise ValueError(f"thinking debe ser 'adaptive' o 'disabled', no {thinking!r}")
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.thinking = thinking
        self.effort = effort
        self.min_max_tokens_adaptive = min_max_tokens_adaptive
        self.usd_to_eur = usd_to_eur
        self.prices: dict[str, dict[str, float]] = {
            k: dict(v) for k, v in PRICES_USD_PER_MTOK.items()
        }
        for key, table in (prices or {}).items():
            self.prices[key] = {**self.prices.get(key, {}), **table}
        self._client = client

    # ── Cliente ───────────────────────────────────────────────────────────────

    @property
    def client(self) -> Any:
        """Cliente del SDK (se crea al primer uso; importar ``anthropic`` es caro)."""
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - dependencia declarada
                raise ProviderNotAvailable("falta el paquete 'anthropic' (uv sync)") from exc
            self._client = anthropic.Anthropic(
                timeout=self.timeout_s, max_retries=self.max_retries
            )
        return self._client

    # ── Petición ──────────────────────────────────────────────────────────────

    def build_request(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Argumentos de ``messages.create`` (separado para poder testearlo)."""
        request: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        output_config: dict[str, Any] = {}
        if self.thinking == "disabled":
            request["thinking"] = {"type": "disabled"}
        else:
            request["thinking"] = {"type": "adaptive"}
            max_tokens = max(max_tokens, self.min_max_tokens_adaptive)
            if self.effort:
                output_config["effort"] = self.effort
        request["max_tokens"] = max_tokens
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            request["output_config"] = output_config
        if rejects_sampling(self.model):
            logger.debug(
                "%s no admite temperature: se ignora temperature=%s", self.model, temperature
            )
        else:
            request["extra_body"] = {"temperature": temperature}
        return request

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1000,
    ) -> LLMResult:
        request = self.build_request(
            system, user, temperature=temperature, json_schema=json_schema,
            max_tokens=max_tokens,
        )
        response = self._call(request)

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        model = str(getattr(response, "model", "") or self.model)
        cost = self.cost_eur(
            model, input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        )
        # Tokens de entrada totales (los cacheados también se procesan y se cobran)
        total_in = input_tokens + cache_read + cache_write

        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            raise LLMRefusal(
                f"{model} se negó a responder (categoría: {category or 'sin categoría'})",
                category=category, cost_eur=cost, input_tokens=total_in,
                output_tokens=output_tokens,
            )
        if stop == "max_tokens":
            raise LLMTruncated(
                f"respuesta cortada por max_tokens={request['max_tokens']} ({model})",
                cost_eur=cost, input_tokens=total_in, output_tokens=output_tokens,
            )

        text = "".join(
            block.text for block in getattr(response, "content", None) or ()
            if getattr(block, "type", None) == "text"
        )
        if json_schema is not None:
            try:
                json.loads(text)
            except ValueError as exc:
                raise LLMInvalidOutput(
                    f"se pidió JSON y la respuesta no lo es: {exc}",
                    cost_eur=cost, input_tokens=total_in, output_tokens=output_tokens,
                ) from exc

        return LLMResult(
            text=text,
            input_tokens=total_in,
            output_tokens=output_tokens,
            model=model,
            cost_eur=cost,
        )

    def _call(self, request: dict[str, Any]) -> Any:
        """``messages.create`` traduciendo las excepciones del SDK a ``LLMError``."""
        client = self.client
        import anthropic  # noqa: PLC0415

        try:
            return client.messages.create(**request)
        # De lo más específico a lo más general (APITimeoutError ⊂ APIConnectionError)
        except anthropic.BadRequestError as exc:
            raise LLMBadRequest(f"petición inválida (400): {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(f"credenciales rechazadas (401): {exc.message}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMAuthError(f"sin permiso (403): {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMNotFound(f"modelo o endpoint no encontrado (404): {self.model}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMRateLimited(f"límite de tasa (429): {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            if status >= 500 or status in (408, 409):
                raise LLMServerError(f"error del servidor ({status}): {exc.message}") from exc
            raise LLMBadRequest(f"error de la API ({status}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMConnectionError(f"sin conexión con la API de Claude: {exc}") from exc
        except anthropic.CredentialsError as exc:
            raise LLMAuthError(f"no hay credenciales de Claude: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(f"error del SDK de Claude: {exc}") from exc

    # ── Coste ─────────────────────────────────────────────────────────────────

    def cost_eur(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """
        Coste estimado en euros. ``input_tokens`` son los no cacheados (así los
        cuenta la API). Modelo sin precio conocido → 0 con aviso (la regla de gasto
        lo infravaloraría: añade su precio en ``extra.prices``).
        """
        key = _matches(model, self.prices) or _matches(self.model, self.prices)
        if key is None:
            logger.warning("sin precio para el modelo %s: coste 0 (añádelo a extra.prices)", model)
            return 0.0
        p = self.prices[key]
        usd = (
            input_tokens * p.get("input", 0.0)
            + output_tokens * p.get("output", 0.0)
            + cache_read_tokens * p.get("cache_read", p.get("input", 0.0))
            + cache_write_tokens * p.get("cache_write", p.get("input", 0.0))
        ) / 1_000_000
        return usd * self.usd_to_eur
