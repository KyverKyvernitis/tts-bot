"""V8: Ministral adaptativo e payload compatível com modelos sem reasoning."""
from __future__ import annotations

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Response, _Session, _groq


MODELS = ("ministral-3b-latest", "ministral-8b-latest", "ministral-14b-latest")


def test_default_mistral_chain_uses_free_ministral_tiers():
    assert C.MISTRAL_MODELS == MODELS


def test_adaptive_order_preserves_custom_models_and_only_reorders_known_tiers():
    models = ("custom-a", *MODELS, "mistral-small-latest", "custom-b")
    assert P._adaptive_mistral_models(models, profile="economy", wants_tools=False) == (
        "custom-a", "ministral-3b-latest", "ministral-8b-latest",
        "ministral-14b-latest", "mistral-small-latest", "custom-b",
    )
    assert P._adaptive_mistral_models(models, profile="full", wants_tools=False) == (
        "custom-a", "ministral-8b-latest", "ministral-14b-latest",
        "ministral-3b-latest", "mistral-small-latest", "custom-b",
    )
    assert P._adaptive_mistral_models(models, profile="full", wants_tools=True) == (
        "custom-a", "ministral-14b-latest", "ministral-8b-latest",
        "ministral-3b-latest", "mistral-small-latest", "custom-b",
    )
    assert P._adaptive_mistral_models(models, profile="closing_economy", wants_tools=True) == (
        "custom-a", "ministral-3b-latest", "ministral-8b-latest",
        "ministral-14b-latest", "mistral-small-latest", "custom-b",
    )


@pytest.mark.asyncio
async def test_mistral_simple_text_prefers_3b(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_MODELS", MODELS)
    session = _Session(_Response(_groq("ok", finish="stop")))
    router = P.ProviderRouter(session, mistral_key="m", mistral_enabled=True)
    assert await router.chat(system="s", messages=[P.ChatMessage("user", "oi")]) == "ok"
    assert session.requests[0][1]["json"]["model"] == "ministral-3b-latest"
    assert router.get_request_report()["routing_profile"] == "economy"


@pytest.mark.asyncio
async def test_mistral_normal_text_prefers_8b(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_MODELS", MODELS)
    session = _Session(_Response(_groq("ok", finish="stop")))
    router = P.ProviderRouter(session, mistral_key="m", mistral_enabled=True)
    message = "explique " + ("contexto " * 100)
    assert len(message) > 700
    assert await router.chat(system="s", messages=[P.ChatMessage("user", message)]) == "ok"
    assert session.requests[0][1]["json"]["model"] == "ministral-8b-latest"
    assert router.get_request_report()["routing_profile"] == "full"


@pytest.mark.asyncio
async def test_mistral_tool_turn_prefers_14b(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_MODELS", MODELS)
    spec = ToolSpec("consultar", "Consulta curta.", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    })
    session = _Session(_Response(_groq(calls=[("consultar", {"query": "x"})], finish="tool_calls")))
    router = P.ProviderRouter(session, mistral_key="m", mistral_enabled=True)
    reply = await router.chat(
        system="s", messages=[P.ChatMessage("user", "consulte x")], tool_specs=(spec,),
    )
    assert reply.tool_calls[0].name == "consultar"
    payload = session.requests[0][1]["json"]
    assert payload["model"] == "ministral-14b-latest"
    assert "reasoning_effort" not in payload
    assert router.get_request_report()["routing_profile"] == "full"


@pytest.mark.asyncio
async def test_mistral_closing_prefers_3b(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_MODELS", MODELS)
    session = _Session(_Response(_groq("final", finish="stop")))
    router = P.ProviderRouter(session, mistral_key="m", mistral_enabled=True)
    assert await router.chat(
        system="s", messages=[P.ChatMessage("user", "resuma")], allow_tool_calls=False,
    ) == "final"
    assert session.requests[0][1]["json"]["model"] == "ministral-3b-latest"
    assert router.get_request_report()["routing_profile"] == "closing_economy"


@pytest.mark.asyncio
async def test_api_parameter_error_is_not_repaired_on_same_model(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_MODELS", ("ministral-3b-latest",))
    error = {
        "object": "error",
        "message": "reasoning_effort is not enabled for this model",
        "type": "invalid_request_invalid_args",
        "code": "3051",
        "raw_status_code": 400,
    }
    session = _Session(_Response(error, status=400))
    router = P.ProviderRouter(session, mistral_key="m", mistral_enabled=True)
    with pytest.raises(P.AllProvidersExhausted) as caught:
        await router.chat(system="s", messages=[P.ChatMessage("user", "oi")])
    assert len(session.requests) == 1
    assert caught.value.kind == "request"
    assert caught.value.diagnostic_code == "api_parameter"
