"""Cloudflare fallback exercises real serialization with offline HTTP only."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.action_protocol import ChatReply, NativeToolCall
from cogs.chatbot.media import PreparedImage
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Response, _Session, _groq


ACCOUNT = "a" * 32
KEY = "offline-token-must-not-appear"
MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"


def router(session, **options):
    return P.ProviderRouter(session, cloudflare_key=KEY, cloudflare_account_id=ACCOUNT,
                            cloudflare_enabled=True, **options)


async def chat(client, **options):
    return await client.chat(system="instruções fixas", messages=[P.ChatMessage("user", "oi")],
                             temperature=.8, model=MODEL, timeout_seconds=5, **options)


def read_spec():
    return ToolSpec("consulta", "Consulta de leitura.", {"type": "object", "properties": {},
                                                         "additionalProperties": False})


def native_data(arguments=None, *, call_id="real-call"):
    data = _groq(calls=[("consulta", {} if arguments is None else arguments)])
    data["choices"][0]["message"]["tool_calls"][0]["id"] = call_id
    return data


@pytest.mark.asyncio
async def test_existing_image_credentials_do_not_enable_chat_by_default():
    session = _Session()
    instance = P.ProviderRouter(session, cloudflare_key=KEY, cloudflare_account_id=ACCOUNT)
    assert instance.diagnostics()["configured"]["cloudflare"] is False
    assert instance.diagnostics()["cloudflare_setup"]["enabled"] is False
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[])
    assert failure.value.kind == "unconfigured" and session.requests == []


@pytest.mark.parametrize("account,key,enabled", [(None, KEY, True), ("bad-account", KEY, True),
                                               (ACCOUNT, None, True), (ACCOUNT, KEY, "true")])
def test_incomplete_or_nonexplicit_setup_does_not_initialize(account, key, enabled):
    instance = P.ProviderRouter(_Session(), cloudflare_account_id=account,
                                cloudflare_key=key, cloudflare_enabled=enabled)
    assert instance._cloudflare is None
    assert "billing_verified" in instance.diagnostics()["cloudflare_setup"]
    assert instance.diagnostics()["cloudflare_setup"]["billing_verified"] is False


@pytest.mark.asyncio
async def test_native_contract_and_history_are_preserved_on_cloudflare():
    session = _Session(_Response(native_data()))
    instance = router(session)
    prior = NativeToolCall("previous-call", "consulta", {})
    history = [P.ChatMessage("user", "consulta de novo"),
               P.ChatMessage("assistant", "", tool_calls=(prior,)),
               P.ChatMessage("tool", '{"ok":true}', tool_call_id=prior.id, name=prior.name)]
    result = await instance.chat(system="sistema fixo", messages=history, tool_specs=(read_spec(),))
    assert result.tool_calls[0].id == "real-call" and result.provider == "cloudflare"
    url, request = session.requests[0]
    payload = request["json"]
    assert url == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1/chat/completions"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert payload["model"] == MODEL and payload["stream"] is False
    assert payload["messages"][0]["content"] == "sistema fixo\n/no_think"
    assert payload["messages"][1:] == [message.to_openai_payload() for message in history]
    assert payload["tools"][0]["function"]["name"] == "consulta"
    assert payload["tool_choice"] == "auto" and "max_tokens" in payload
    assert not {"reasoning_effort", "enable_thinking", "extra_body", "max_completion_tokens"} & payload.keys()
    assert "no_think" not in history[0].content


@pytest.mark.asyncio
async def test_final_closing_keeps_declarations_and_disables_tools():
    session = _Session(_Response(_groq("pronto", finish="stop")))
    result = await chat(P._CloudflareClient(session, KEY, ACCOUNT), tool_specs=(read_spec(),), allow_tool_calls=False)
    assert result.text == "pronto"
    payload = session.requests[0][1]["json"]
    assert payload["tool_choice"] == "none" and payload["max_tokens"] == C.MAX_RESPONSE_TOKENS
    assert payload["tools"][0]["function"]["name"] == "consulta"


@pytest.mark.asyncio
@pytest.mark.parametrize("model,images", [("@cf/paid/arbitrary-model", []),
    (MODEL, [PreparedImage("image/png", b"image", "x.png")])])
async def test_paid_or_vision_paths_reject_before_http(model, images):
    session = _Session()
    client = P._CloudflareClient(session, KEY, ACCOUNT)
    with pytest.raises(P.ProviderError):
        await client.chat(system="s", messages=[P.ChatMessage("user", "oi", images=images)],
                          model=model, temperature=.8, timeout_seconds=5)
    assert not session.requests and client.budget_diagnostics()["reserved_or_used_neurons"] == 0


@pytest.mark.asyncio
async def test_cloudflare_is_last_even_if_text_order_requests_it_first(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-a", "groq-b"))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-a",))
    session = _Session(_Response({"error": {}}, 429), _Response({"error": {}}, 429),
                       _Response({"error": {}}, 429), _Response(_groq("fallback ok", finish="stop")))
    instance = router(session, groq_key=KEY, gemini_key=KEY)
    assert await instance.chat(system="s", messages=[], text_provider_order=("cloudflare", "groq", "gemini")) == "fallback ok"
    report = instance.get_request_report()
    assert [item["provider"] for item in report["attempts"]] == ["groq", "groq", "gemini", "cloudflare"]
    assert report["request_count"] == 4


@pytest.mark.asyncio
async def test_vision_router_never_degrades_to_text_only_cloudflare():
    session = _Session()
    instance = router(session)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[P.ChatMessage("user", "veja", images=[PreparedImage("image/png", b"x", "x.png")])])
    assert failure.value.kind == "unconfigured" and not session.requests
    model = next(item for item in instance.diagnostics()["availability"] if item["provider"] == "cloudflare")
    assert model["modes"] == ("text",)


@pytest.mark.asyncio
async def test_daily_neuron_failure_sets_account_circuit_until_utc_reset(monkeypatch, caplog):
    monkeypatch.setattr(P.time, "time", lambda: 86400 * 20000 + 80000)
    body = {"errors": [{"code": 123, "message": "daily neuron limit exceeded private detail"}]}
    session = _Session(_Response(body, 429))
    instance = router(session)
    with caplog.at_level(logging.INFO, logger="cogs.chatbot.providers"):
        with pytest.raises(P.AllProvidersExhausted) as failure:
            await instance.chat(system="PRIVATE prompt", messages=[])
    assert failure.value.quota_scope == "account" and 6399 < failure.value.retry_after <= 6400
    assert instance._account_states["cloudflare"].last_kind == "rate_limit"
    assert all(value not in caplog.text for value in (KEY, ACCOUNT, "private detail", "PRIVATE prompt"))


@pytest.mark.asyncio
async def test_generic_cloudflare_429_uses_explicit_retry_without_daily_assumption():
    response = _Response({"errors": [{"message": "too many requests"}]}, 429)
    response.headers["Retry-After"] = "12"
    session = _Session(response)
    instance = router(session)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[])
    assert failure.value.retry_after == pytest.approx(12, abs=.1)
    assert failure.value.quota_scope == "model"


@pytest.mark.asyncio
async def test_budget_rejects_before_http_and_resets_next_utc_day(monkeypatch):
    now = [86400 * 20000 + 80000]
    monkeypatch.setattr(P.time, "time", lambda: now[0])
    session = _Session(_Response(_groq("ok", finish="stop")))
    client = P._CloudflareClient(session, KEY, ACCOUNT)
    client._budget_spent = C.CLOUDFLARE_DAILY_NEURON_BUDGET
    with pytest.raises(P.RateLimitError) as failure:
        await chat(client)
    assert failure.value.stage == "routing" and session.requests == []
    now[0] += 86400
    assert await chat(client) == "ok"
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_measured_usage_reconciles_estimate_and_missing_usage_keeps_reservation():
    data = _groq("oi", finish="stop")
    data["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    session = _Session(_Response(data), _Response(_groq("outro", finish="stop")))
    client = P._CloudflareClient(session, KEY, ACCOUNT)
    assert await chat(client) == "oi"
    spent = client._budget_spent
    assert spent == pytest.approx(100 * .004625 + 20 * .030475)
    assert await chat(client) == "outro" and client._budget_spent > spent
    state = client.budget_diagnostics()
    assert state["scope"] == "process_daily" and state["estimated"] is True


@pytest.mark.asyncio
async def test_reported_neurons_override_local_token_estimate_when_available():
    data = _groq("oi", finish="stop")
    data["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "neurons": 0.75}
    client = P._CloudflareClient(_Session(_Response(data)), KEY, ACCOUNT)
    assert await chat(client) == "oi"
    assert client._budget_spent == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_concurrent_reservations_do_not_both_fit_the_last_budget(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    data = _groq("ok", finish="stop")
    class Body:
        async def iter_chunked(self, size):
            started.set()
            await release.wait()
            yield json.dumps(data).encode()
    response = _Response(data)
    response.content = Body()
    session = _Session(response)
    client = P._CloudflareClient(session, KEY, ACCOUNT)
    monkeypatch.setattr(C, "CLOUDFLARE_DAILY_NEURON_BUDGET", 20)
    first = asyncio.create_task(chat(client))
    await started.wait()
    with pytest.raises(P.RateLimitError):
        await chat(client)
    release.set()
    assert await first == "ok" and len(session.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [("<think>private thought</think>\nOi!", "Oi!"),
                                        ("texto <think> literal", "texto <think> literal")])
async def test_reasoning_block_is_removed_only_at_start(text, expected):
    session = _Session(_Response(_groq(text, finish="stop")))
    assert await chat(P._CloudflareClient(session, KEY, ACCOUNT)) == expected


@pytest.mark.asyncio
async def test_unfinished_reasoning_never_becomes_public_reply():
    session = _Session(_Response(_groq("<think>private unfinished", finish="length")))
    with pytest.raises(P.ProviderError) as failure:
        await chat(P._CloudflareClient(session, KEY, ACCOUNT))
    assert failure.value.kind == "empty" and "private unfinished" not in str(failure.value)


@pytest.mark.asyncio
async def test_invalid_cloudflare_call_gets_one_repair_and_preserves_real_id():
    session = _Session(_Response(native_data({"PRIVATE_EXTRA": "PRIVATE_VALUE"})), _Response(native_data()))
    result = await router(session).chat(system="s", messages=[], tool_specs=(read_spec(),))
    assert result.tool_calls[0].id == "real-call" and len(session.requests) == 2
    repaired = session.requests[1][1]["json"]["messages"][0]["content"]
    assert "Diagnóstico seguro:" in repaired and "PRIVATE_EXTRA" not in repaired and "PRIVATE_VALUE" not in repaired


@pytest.mark.asyncio
async def test_cloudflare_diagnostics_never_contain_credentials_or_billing_guarantee():
    session = _Session(_Response(_groq("oi", finish="stop")))
    instance = router(session)
    await instance.chat(system="PRIVATE system", messages=[])
    data = instance.diagnostics()
    assert data["configured"]["cloudflare"] and data["models"]["cloudflare"] == (MODEL,)
    assert data["cloudflare_setup"]["billing_verified"] is False
    assert all(value not in json.dumps(data) for value in (ACCOUNT, KEY, "PRIVATE system"))
