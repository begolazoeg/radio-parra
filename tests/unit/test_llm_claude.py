"""
Tests de ``ClaudeLLM`` sin red: cliente del SDK sustituido por un doble, y una
prueba con el cliente real del SDK sobre un transporte simulado (``MockTransport``)
para ver el cuerpo JSON que de verdad saldría hacia la API.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message

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
    ProviderError,
)
from radio.providers.llm.base import LLM
from radio.providers.llm.claude import (
    ClaudeLLM,
    detect_credentials,
    rejects_sampling,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"script": {"type": "string"}},
    "required": ["script"],
    "additionalProperties": False,
}


def make_message(
    text: str = '{"script": "Hola"}',
    *,
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    model: str = "claude-sonnet-5",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read: int | None = None,
    cache_write: int | None = None,
    thinking: bool = True,
) -> Message:
    """Respuesta de ``messages.create`` con los tipos reales del SDK."""
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    content.append({"type": "text", "text": text, "citations": None})
    return Message.model_validate({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    })


class FakeMessages:
    def __init__(self, response: Any = None, exc: BaseException | None = None) -> None:
        self.response = response if response is not None else make_message()
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


class FakeClient:
    def __init__(self, response: Any = None, exc: BaseException | None = None) -> None:
        self.messages = FakeMessages(response, exc)


def llm_with(response: Any = None, exc: BaseException | None = None, **kw: Any) -> tuple[
    ClaudeLLM, FakeMessages
]:
    client = FakeClient(response, exc)
    return ClaudeLLM(client=client, **kw), client.messages


# ── Forma de la petición ──────────────────────────────────────────────────────

def test_is_an_llm() -> None:
    llm, _ = llm_with()
    assert isinstance(llm, LLM)


def test_request_shape_sonnet5_with_schema() -> None:
    llm, messages = llm_with()
    llm.complete("Eres locutora.", "Presenta el concierto.", temperature=0.2,
                 json_schema=SCHEMA, max_tokens=500)
    (req,) = messages.calls
    assert req["model"] == "claude-sonnet-5"
    assert req["system"] == "Eres locutora."
    assert req["messages"] == [{"role": "user", "content": "Presenta el concierto."}]
    # Sonnet 5 rechaza temperature/top_p/top_k: no se envían de ninguna forma
    assert "temperature" not in req and "extra_body" not in req
    assert not {"top_p", "top_k"} & set(req)
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert "output_format" not in req                    # parámetro obsoleto
    # Pensamiento adaptativo con esfuerzo bajo y suelo de max_tokens
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"]["effort"] == "low"
    assert req["max_tokens"] == 2000
    # Sin prefill: el último mensaje es del usuario
    assert req["messages"][-1]["role"] == "user"


def test_request_without_schema_has_no_format() -> None:
    llm, messages = llm_with(make_message("Buenas tardes."))
    llm.complete("s", "u", temperature=0.9)
    req = messages.calls[0]
    assert "format" not in req.get("output_config", {})


def test_thinking_disabled_keeps_max_tokens() -> None:
    llm, messages = llm_with(thinking="disabled")
    llm.complete("s", "u", temperature=0.5, json_schema=SCHEMA, max_tokens=700)
    req = messages.calls[0]
    assert req["thinking"] == {"type": "disabled"}
    assert req["max_tokens"] == 700
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}


def test_max_tokens_above_floor_is_kept() -> None:
    llm, messages = llm_with()
    llm.complete("s", "u", temperature=0.5, max_tokens=4000)
    assert messages.calls[0]["max_tokens"] == 4000


@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "claude-opus-5", "claude-opus-5-5", "claude-fable-5",
    "claude-fable-5-1", "claude-opus-4-7", "claude-opus-4-8",
])
def test_models_without_sampling(model: str) -> None:
    assert rejects_sampling(model)


def test_other_models_get_temperature() -> None:
    assert not rejects_sampling("claude-haiku-4-5")
    llm, messages = llm_with(model="claude-haiku-4-5")
    llm.complete("s", "u", temperature=0.3)
    assert messages.calls[0]["extra_body"] == {"temperature": 0.3}


def test_temperature_ignored_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    llm, _ = llm_with()
    with caplog.at_level("DEBUG", logger="radio.providers.llm.claude"):
        llm.complete("s", "u", temperature=0.7)
    assert "temperature" in caplog.text


# ── Respuesta ─────────────────────────────────────────────────────────────────

def test_extracts_json_text_and_usage() -> None:
    llm, _ = llm_with(make_message('{"script": "Suena Tiny Desk"}', input_tokens=1000,
                                   output_tokens=200))
    res = llm.complete("s", "u", temperature=0.2, json_schema=SCHEMA)
    assert isinstance(res, LLMResult)
    assert json.loads(res.text) == {"script": "Suena Tiny Desk"}   # sin el bloque thinking
    assert (res.input_tokens, res.output_tokens, res.model) == (1000, 200, "claude-sonnet-5")
    # 1000 × 2 $/M + 200 × 10 $/M = 0,004 $ → × 0,92
    assert res.cost_eur == pytest.approx(0.004 * 0.92)


def test_cost_counts_cache_tokens() -> None:
    llm, _ = llm_with(make_message(input_tokens=100, output_tokens=0, cache_read=1_000_000,
                                   cache_write=1_000_000), usd_to_eur=1.0)
    res = llm.complete("s", "u", temperature=0.2)
    assert res.input_tokens == 2_000_100
    assert res.cost_eur == pytest.approx(100 * 2e-6 + 0.20 + 2.50)


def test_cost_price_override_and_unknown_model() -> None:
    llm = ClaudeLLM("claude-sonnet-5", client=FakeClient(), usd_to_eur=1.0,
                    prices={"claude-sonnet-5": {"input": 1.0}})
    assert llm.cost_eur("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0) == 1.0
    unknown = ClaudeLLM("modelo-raro", client=FakeClient())
    assert unknown.cost_eur("modelo-raro", input_tokens=10**6, output_tokens=10**6) == 0.0


def test_refusal_raises_with_category_and_cost() -> None:
    msg = make_message("", stop_reason="refusal",
                       stop_details={"type": "refusal", "category": "cyber",
                                     "explanation": "no"})
    llm, _ = llm_with(msg)
    with pytest.raises(LLMRefusal) as info:
        llm.complete("s", "u", temperature=0.2)
    assert info.value.category == "cyber"
    assert info.value.cost_eur > 0 and not info.value.retryable


def test_refusal_without_details() -> None:
    llm, _ = llm_with(make_message("", stop_reason="refusal", stop_details=None))
    with pytest.raises(LLMRefusal) as info:
        llm.complete("s", "u", temperature=0.2)
    assert info.value.category is None


def test_max_tokens_raises_truncated() -> None:
    llm, _ = llm_with(make_message('{"script": "Hol', stop_reason="max_tokens"))
    with pytest.raises(LLMTruncated) as info:
        llm.complete("s", "u", temperature=0.2, json_schema=SCHEMA)
    assert info.value.output_tokens == 200


def test_invalid_json_with_schema() -> None:
    llm, _ = llm_with(make_message("esto no es JSON"))
    with pytest.raises(LLMInvalidOutput):
        llm.complete("s", "u", temperature=0.2, json_schema=SCHEMA)


# ── Errores del SDK → jerarquía propia ───────────────────────────────────────

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls("fallo", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize(("exc", "expected", "retryable"), [
    (status_error(anthropic.BadRequestError, 400), LLMBadRequest, False),
    (status_error(anthropic.AuthenticationError, 401), LLMAuthError, False),
    (status_error(anthropic.PermissionDeniedError, 403), LLMAuthError, False),
    (status_error(anthropic.NotFoundError, 404), LLMNotFound, False),
    (status_error(anthropic.RateLimitError, 429), LLMRateLimited, True),
    (status_error(anthropic.InternalServerError, 500), LLMServerError, True),
    (status_error(anthropic.APIStatusError, 529), LLMServerError, True),
    (status_error(anthropic.UnprocessableEntityError, 422), LLMBadRequest, False),
    (anthropic.APIConnectionError(request=REQ), LLMConnectionError, True),
    (anthropic.APITimeoutError(request=REQ), LLMConnectionError, True),
    (anthropic.CredentialsError("sin perfil"), LLMAuthError, False),
])
def test_error_mapping(exc: BaseException, expected: type[LLMError], retryable: bool) -> None:
    llm, _ = llm_with(exc=exc)
    with pytest.raises(expected) as info:
        llm.complete("s", "u", temperature=0.2)
    assert isinstance(info.value, ProviderError)
    assert info.value.retryable is retryable
    assert info.value.__cause__ is exc


# ── Cliente real del SDK sobre transporte simulado (sin red) ─────────────────

def test_wire_body_with_real_sdk_client() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=make_message().model_dump(mode="json"))

    client = anthropic.Anthropic(
        api_key="sk-test", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    res = ClaudeLLM(client=client).complete("sys", "user", temperature=0.4,
                                            json_schema=SCHEMA, max_tokens=300)
    assert json.loads(res.text) == {"script": "Hola"}
    (body,) = seen
    assert body["model"] == "claude-sonnet-5"
    assert "temperature" not in body
    assert body["output_config"] == {"effort": "low",
                                     "format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["system"] == "sys" and body["max_tokens"] == 2000

    haiku = ClaudeLLM("claude-haiku-4-5", client=client, thinking="disabled")
    haiku.complete("sys", "user", temperature=0.4)
    assert seen[-1]["temperature"] == 0.4 and seen[-1]["thinking"] == {"type": "disabled"}


def test_wire_errors_with_real_sdk_client() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"type": "error", "error": {
            "type": "rate_limit_error", "message": "despacio"}})

    client = anthropic.Anthropic(
        api_key="sk-test", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    with pytest.raises(LLMRateLimited):
        ClaudeLLM(client=client).complete("s", "u", temperature=0.2)


# ── Credenciales ─────────────────────────────────────────────────────────────

def test_detect_credentials(tmp_path: Any) -> None:
    empty = {"ANTHROPIC_CONFIG_DIR": str(tmp_path / "nada")}
    assert detect_credentials(empty) is None
    assert detect_credentials({**empty, "ANTHROPIC_API_KEY": "sk"}) == "ANTHROPIC_API_KEY"
    assert detect_credentials({**empty, "ANTHROPIC_API_KEY": "  "}) is None
    profile = tmp_path / "anthropic"
    profile.mkdir()
    (profile / "active_config").write_text("default")
    assert detect_credentials({"ANTHROPIC_CONFIG_DIR": str(profile)}) == "perfil ant"


def test_invalid_thinking_mode() -> None:
    with pytest.raises(ValueError):
        ClaudeLLM(client=FakeClient(), thinking="enabled")  # type: ignore[arg-type]
