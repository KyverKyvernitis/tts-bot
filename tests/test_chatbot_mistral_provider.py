"""Quarta reserva Mistral: opt-in, raciocínio econômico e fallback seguro."""
from __future__ import annotations

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Response, _Session, _groq


@pytest.mark.asyncio
async def test_mistral_client_uses_official_chat_endpoint_and_disables_reasoning():
    session = _Session(_Response(_groq("ok", finish="stop")))
    client = P._MistralClient(session, "offline-key")
    result = await client.chat(
        system="prefixo estável", messages=[P.ChatMessage("user", "oi")],
        temperature=.8, model=C.MISTRAL_MODELS[0], timeout_seconds=5,
    )
    assert result == "ok"
    url, options = session.requests[0]
    payload = options["json"]
    assert url == "https://api.mistral.ai/v1/chat/completions"
    assert payload["model"] == C.MISTRAL_MODELS[0]
    assert payload["reasoning_effort"] == "none"
    assert payload["prompt_cache_key"].startswith("tts-bot-chatbot-")
    assert "prefixo estável" not in payload["prompt_cache_key"]
    assert payload["max_tokens"] == C.TINY_RESPONSE_TOKENS
    assert "include_reasoning" not in payload


@pytest.mark.asyncio
async def test_mistral_cache_key_does_not_change_with_dynamic_system_suffix():
    first = _Session(_Response(_groq("ok", finish="stop")))
    second = _Session(_Response(_groq("ok", finish="stop")))
    for session, system in ((first, "base\nestado=1"), (second, "base\nestado=2")):
        await P._MistralClient(session, "offline-key").chat(
            system=system, messages=[P.ChatMessage("user", "oi")], temperature=.8,
            model=C.MISTRAL_MODELS[0], timeout_seconds=5,
        )
    assert first.requests[0][1]["json"]["prompt_cache_key"] == second.requests[0][1]["json"]["prompt_cache_key"]


@pytest.mark.asyncio
async def test_mistral_preserves_native_tool_contract():
    spec = ToolSpec("consultar", "Consulta curta.", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    })
    data = _groq(calls=[("consultar", {"query": "x"})], finish="tool_calls")
    session = _Session(_Response(data))
    reply = await P._MistralClient(session, "offline-key").chat(
        system="s", messages=[P.ChatMessage("user", "consulte x")], temperature=.8,
        model=C.MISTRAL_MODELS[0], timeout_seconds=5, tool_specs=(spec,),
    )
    assert reply.provider == "mistral" and reply.tool_calls[0].name == "consultar"
    payload = session.requests[0][1]["json"]
    assert payload["tools"][0]["function"]["name"] == "consultar"
    assert payload["tool_choice"] == "auto"


def test_mistral_is_opt_in_and_visible_in_diagnostics():
    disabled = P.ProviderRouter(object(), mistral_key="secret", mistral_enabled=False)
    assert disabled.diagnostics()["configured"]["mistral"] is False
    assert disabled.diagnostics()["mistral_setup"] == {"enabled": False, "api_key_configured": True}

    enabled = P.ProviderRouter(object(), mistral_key="secret", mistral_enabled=True)
    data = enabled.diagnostics()
    assert data["configured"]["mistral"] is True
    assert data["models"]["mistral"] == tuple(C.MISTRAL_MODELS)
    assert "secret" not in repr(data)


@pytest.mark.asyncio
async def test_mistral_reserve_runs_before_cloudflare_neuron_reserve(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_MODELS", ("mistral-small-latest",))
    monkeypatch.setattr(C, "CLOUDFLARE_MODELS", ("@cf/qwen/qwen3-30b-a3b-fp8",))
    session = _Session(_Response(_groq("mistral ok", finish="stop")))
    router = P.ProviderRouter(
        session, mistral_key="m", mistral_enabled=True,
        cloudflare_key="c", cloudflare_account_id="a" * 32, cloudflare_enabled=True,
    )
    assert await router.chat(system="s", messages=[P.ChatMessage("user", "oi")]) == "mistral ok"
    assert len(session.requests) == 1
    assert session.requests[0][0] == "https://api.mistral.ai/v1/chat/completions"


@pytest.mark.asyncio
async def test_optional_work_never_uses_mistral_or_cloudflare_reserves():
    session = _Session(_Response(_groq("should not run", finish="stop")))
    router = P.ProviderRouter(
        session, mistral_key="m", mistral_enabled=True,
        cloudflare_key="c", cloudflare_account_id="a" * 32, cloudflare_enabled=True,
    )
    with pytest.raises(P.AllProvidersExhausted):
        await router.chat(system="s", messages=[P.ChatMessage("user", "oi")],
                          allow_protected_reserves=False)
    assert session.requests == []
