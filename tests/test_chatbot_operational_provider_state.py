"""Estado real no prompt e na ferramenta, sem consultar APIs ou revelar conteúdo."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from cogs.chatbot.action_protocol import ChatReply, NativeToolCall
from cogs.chatbot import constants as C
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.providers import AllProvidersExhausted, ChatMessage, ProviderRouter
from cogs.chatbot.tool_runtime import conversation_references, safe_provider_state
from tests.test_chatbot_action_flow import world
from tests.test_chatbot_native_atomicity import activate_registry, run_loop
from tests.test_chatbot_native_conversation import confirmed_state
from tests.test_chatbot_tool_runtime import registry_for, call
from tests.test_chatbot_action_providers import _Session, _Response, _groq


def diagnostics(*, available=False, wait=79200):
    return {
        "configured": {"groq": True, "gemini": False, "api_key": "PRIVATE_KEY"},
        "availability": [{"provider": "groq", "model": "test-model", "configured": True,
            "available": available, "cooldown_seconds": wait, "cause_kind": "rate_limit",
            "status": 429, "quota_scope": "account", "modes": ["text"], "tools_support": "unknown",
            "request_body": "PRIVATE_HTTP", "text": "PRIVATE_SPEECH"}],
        "earliest_retry_seconds": wait,
        "last_request": {"outcome": "failed", "kind": "rate_limit", "cause_kind": "rate_limit",
            "usage": {"input_tokens": 120, "output_tokens": 20, "total_tokens": 140, "api_key": "PRIVATE_USAGE"},
            "attempts": [{"arguments": "PRIVATE_ARGS"}], "message": "PRIVATE_MESSAGE"},
    }


def test_safe_snapshot_is_local_immutable_and_whitelisted():
    raw = diagnostics()
    original = copy.deepcopy(raw)
    router = SimpleNamespace(diagnostics=Mock(return_value=raw), chat=AsyncMock())
    state = safe_provider_state(router)
    assert raw == original
    assert state["configured"] == {"groq": True, "gemini": False}
    assert state["availability"][0]["model"] == "test-model"
    assert state["availability"][0]["cause_kind"] == "rate_limit"
    assert state["earliest_retry_seconds"] == 79200
    assert state["last_request"]["usage"] == {"input_tokens": 120, "output_tokens": 20, "total_tokens": 140}
    assert "PRIVATE_" not in json.dumps(state)
    router.chat.assert_not_awaited()
    state["availability"][0]["model"] = "changed"
    assert raw == original


@pytest.mark.parametrize("bad", [None, [], "PRIVATE_KEY", {"configured": {"groq": "PRIVATE_KEY"},
    "availability": [{"provider": "groq", "model": "invalid model PRIVATE_KEY"},
                     {"provider": "unknown", "model": "PRIVATE_KEY"}],
    "earliest_retry_seconds": float("nan")}])
def test_missing_or_malformed_snapshot_never_invents_configuration(bad):
    state = safe_provider_state(SimpleNamespace(diagnostics=lambda: bad))
    assert state["configured"] == {"groq": None, "gemini": None}
    assert state["availability"] == [] and state["earliest_retry_seconds"] is None
    assert "PRIVATE_" not in json.dumps(state)


def test_nonfinite_and_structurally_invalid_metadata_is_not_forwarded():
    raw = diagnostics()
    raw["availability"][0].update(cause_kind={}, quota_scope=[], tools_support={},
        modes=["text", {}, "text", "PRIVATE_MODE"], cooldown_seconds=10 ** 1000)
    raw["last_request"].update(kind={}, outcome=[], cause_kind={},
        usage={"input_tokens": float("inf"), "output_tokens": -10, "total_tokens": True})
    state = safe_provider_state(SimpleNamespace(diagnostics=lambda: raw))
    model = state["availability"][0]
    assert model["modes"] == ["text"]
    assert not set(model) & {"cause_kind", "quota_scope", "tools_support", "cooldown_seconds"}
    assert "last_request" not in state
    json.dumps(state, allow_nan=False)


def test_old_circuits_only_report_observed_model_eligibility():
    state = safe_provider_state(SimpleNamespace(diagnostics=lambda: {
        "configured": {"groq": True, "gemini": True},
        "circuits": {"gemini/test-model": {"available": False, "cooldown_seconds": 600,
            "last_kind": "model", "last_status": 404}},
    }))
    assert len(state["availability"]) == 1
    assert state["availability"][0]["provider"] == "gemini"
    assert state["availability"][0]["cause_kind"] == "model"
    assert state["earliest_retry_seconds"] is None


@pytest.mark.asyncio
async def test_operational_tool_reports_safe_provider_state_without_any_provider_request(world):
    world.cog._router.diagnostics = Mock(return_value=diagnostics())
    registry = await registry_for(world)
    result = await call(registry, "get_operational_state")
    assert result["ok"] is True
    state = result["data"]["providers"]
    assert state["configured"] == {"groq": True, "gemini": False}
    assert state["availability"][0]["cooldown_seconds"] == 79200
    assert result["data"]["voice_state"]["can_listen"] is False
    assert "PRIVATE_" not in json.dumps(result)
    world.cog._router.chat.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_each_round_refreshes_provider_state_preserving_native_history_and_references(world):
    activate_registry(world)
    local = diagnostics()
    world.cog._router.diagnostics = Mock(side_effect=lambda: copy.deepcopy(local))
    systems = []
    async def model(**options):
        systems.append(options["system"])
        if len(systems) == 1:
            local["availability"][0].update(available=True, cooldown_seconds=0)
            local["earliest_retry_seconds"] = None
            return ChatReply("private intermediate", tool_calls=(NativeToolCall("real-read", "get_operational_state", {}),))
        assert options["messages"][-1].tool_call_id == "real-read"
        assert json.loads(options["messages"][-1].content)["data"]["providers"]["availability"][0]["available"]
        return ChatReply("Esse provedor voltou a ficar elegível.")
    world.cog._router.chat.side_effect = model
    reply, registry, state = await run_loop(world)
    first, second = map(confirmed_state, systems)
    assert first["providers"]["availability"][0]["available"] is False
    assert second["providers"]["availability"][0]["available"] is True
    assert second["providers"]["earliest_retry_seconds"] is None
    assert first["references"]["members"]["autor"]["id"] == "1"
    assert first["references"]["members"]["m1"]["id"] == "3"
    assert registry.runtime.action_context.description not in systems[-1]
    assert "PRIVATE_" not in "".join(systems)
    assert reply.text == "Esse provedor voltou a ficar elegível." and not state["delivered"]
    world.message.reply.assert_not_awaited()
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_prompt_preserves_unique_action_rules_in_native_declarations(world):
    activate_registry(world)
    world.cog._router.chat.return_value = ChatReply("oi")
    _reply, registry, _state = await run_loop(world)
    options = world.cog._router.chat.await_args.kwargs
    context = registry.runtime.action_context
    assert len(context.description) > 1000
    assert context.description not in options["system"]
    proposal = next(spec for spec in options["tool_specs"] if spec.name == "propor_acao")
    assert "PRIVADO" in proposal.description and "aprovação separada da staff" in proposal.description
    assert set(proposal.parameters["properties"]["action"]["enum"]) == set(context.actions)
    state = confirmed_state(options["system"])
    assert state["references"]["members"]["autor"]["voice"]["channel_id"] == "20"
    assert "private intermediate" not in options["system"]


def test_reference_snapshot_hides_newly_private_channels_and_message_contents(world):
    resource = world.channel
    context = SimpleNamespace(targets={"autor": world.members[1]}, resources={"canal": resource})
    resource.permissions_for.return_value = SimpleNamespace(view_channel=False)
    refs = conversation_references(world.cog, world.message, context)
    assert refs["resources"] == {}
    hidden_voice = world.voice
    hidden_voice.permissions_for.return_value = SimpleNamespace(view_channel=False)
    refs = conversation_references(world.cog, world.message, context)
    assert refs["members"]["autor"]["voice"]["channel_id"] is None
    assert refs["members"]["autor"]["voice"]["channel_name"] is None
    message = Mock(spec=discord.Message)
    message.id, message.channel, message.content = 543, world.channel, "PRIVATE_MESSAGE_CONTENT"
    context.resources = {"mensagem": message}
    refs = conversation_references(world.cog, world.message, context)
    assert refs["resources"] == {"mensagem": {"id": "543", "kind": "message"}}
    assert "PRIVATE_MESSAGE_CONTENT" not in json.dumps(refs)


def test_snapshot_of_real_router_does_not_make_http_calls():
    session = SimpleNamespace(post=Mock(side_effect=AssertionError("No external calls")))
    router = ProviderRouter(session, groq_key="test-key")
    state = safe_provider_state(router)
    assert state["configured"] == {"groq": True, "gemini": False}
    session.post.assert_not_called()


def test_reported_attempt_usage_is_used_only_when_aggregate_is_missing():
    local = diagnostics()
    local["last_request"].pop("usage")
    local["last_request"]["attempts"] = [
        {"usage": {"input_tokens": 20, "total_tokens": 30}},
        {"usage": {"input_tokens": 50, "total_tokens": 60, "PRIVATE_KEY": 10}},
        {"status": 404, "body": "PRIVATE_HTTP"},
    ]
    state = safe_provider_state(SimpleNamespace(diagnostics=lambda: local))
    assert state["last_request"]["usage"] == {"input_tokens": 50, "total_tokens": 60}
    assert "PRIVATE_" not in json.dumps(state)
    local["last_request"]["usage"] = {"input_tokens": 70, "total_tokens": 90}
    assert safe_provider_state(SimpleNamespace(diagnostics=lambda: local))["last_request"]["usage"] == {
        "input_tokens": 70, "total_tokens": 90,
    }


def test_partial_reported_usage_remains_explicitly_partial_without_invented_fields():
    local = diagnostics()
    local["last_request"].update(usage_scope="reported_attempts", usage_complete=False,
                                usage_attempt_count=1)
    state = safe_provider_state(SimpleNamespace(diagnostics=lambda: local))["last_request"]
    assert state["usage_complete"] is False and state["usage_attempt_count"] == 1
    assert state["usage_scope"] == "reported_attempts"
    assert state["usage"] == {"input_tokens": 120, "output_tokens": 20, "total_tokens": 140}
    local["last_request"].update(usage_scope="PRIVATE_REQUEST_BODY", usage_complete="false",
                                usage_attempt_count=True)
    state = safe_provider_state(SimpleNamespace(diagnostics=lambda: local))["last_request"]
    assert not set(state) & {"usage_complete", "usage_scope", "usage_attempt_count"}


@pytest.mark.parametrize("mode", ["text", "vision"])
def test_safe_last_request_declares_modality_of_retry_and_usage(mode):
    local = diagnostics()
    local["last_request"]["mode"] = mode
    assert safe_provider_state(SimpleNamespace(diagnostics=lambda: local))["last_request"]["mode"] == mode
    local["last_request"]["mode"] = "PRIVATE_MODE"
    assert "mode" not in safe_provider_state(SimpleNamespace(diagnostics=lambda: local))["last_request"]


@pytest.mark.parametrize("kind", ["rate_limit", "cooldown"])
def test_quota_notice_uses_aggregate_viable_retry_not_last_bad_model(kind):
    exc = AllProvidersExhausted("PRIVATE_LAST_404", kind=kind, cause_kind="rate_limit",
        retry_after=11, earliest_retry_seconds=79200,
        causes=({"kind": "rate_limit", "status": 429}, {"kind": "model", "status": 404}))
    notice = ChatbotCog._chat_failure_text(exc, had_images=False)
    assert "limite de uso" in notice and "22 hora" in notice
    assert "11 segundo" not in notice and "404" not in notice and "PRIVATE_" not in notice


@pytest.mark.parametrize("delay", [None, float("nan"), float("inf"), -10])
def test_quota_notice_does_not_invent_reset_time(delay):
    exc = AllProvidersExhausted("PRIVATE_RATE", kind="rate_limit", retry_after=delay)
    notice = ChatbotCog._chat_failure_text(exc, had_images=True)
    assert "limite de uso" in notice and "Não recebi um prazo" in notice
    assert "segundo" not in notice and "PRIVATE_" not in notice


@pytest.mark.parametrize("kind", ["auth", "model", "unconfigured", "invalid_response", "tools_unsupported"])
def test_configuration_or_invalid_tools_are_not_presented_as_quota(kind):
    exc = AllProvidersExhausted("PRIVATE_ARGUMENT_VALUE", kind=kind, retry_after=600)
    notice = ChatbotCog._chat_failure_text(exc, had_images=False)
    assert "limite de uso" not in notice and "600" not in notice and "PRIVATE_" not in notice


@pytest.mark.parametrize("cause", ["auth", "model", "unconfigured"])
def test_configuration_only_cooldowns_never_promise_that_waiting_restores_access(cause):
    exc = AllProvidersExhausted("PRIVATE_CONFIG", kind="cooldown", cause_kind=cause, retry_after=300)
    notice = ChatbotCog._chat_failure_text(exc, had_images=False)
    assert "configuração" in notice
    assert "Aguarde" not in notice and "minuto" not in notice and "limite de uso" not in notice


@pytest.mark.parametrize("kind,phrase", [("invalid_response", "resposta válida"),
    ("tools_unsupported", "ferramentas"), ("deadline", "demorou demais"), ("network", "responder")])
def test_mixed_tool_failure_and_quota_discloses_both_without_global_wait(kind, phrase):
    exc = AllProvidersExhausted("PRIVATE_ARGUMENT", kind=kind, earliest_retry_seconds=79200,
        causes=({"kind": kind, "diagnostic_path": "$.text", "private": "PRIVATE_VALUE"},
                {"kind": "rate_limit", "retry_after": 79200}))
    notice = ChatbotCog._chat_failure_text(exc, had_images=False)
    assert phrase in notice and "alternativa de IA" in notice and "limite de uso" in notice
    assert "Aguarde" not in notice and "22 hora" not in notice and "PRIVATE_" not in notice


@pytest.mark.asyncio
async def test_real_router_mixed_404_and_long_quota_keep_viable_retry_in_state_and_notice(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-test",))
    monkeypatch.setattr(C, "GROQ_VISION_MODELS", ("groq-test",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-test",))
    monkeypatch.setattr(C, "GEMINI_VISION_MODELS", ("gemini-test",))
    quota = _Response({"error": {"message": "PRIVATE_PROJECT quota exceeded"}}, 429)
    quota.headers["Retry-After"] = "79200"
    session = _Session(_Response({"error": {"message": "PRIVATE_404 model not found"}}, 404), quota)
    router = ProviderRouter(session, groq_key="PRIVATE_GROQ_KEY", gemini_key="PRIVATE_GEMINI_KEY")
    for _ in range(2):
        with pytest.raises(AllProvidersExhausted) as failure:
            await router.chat(system="PRIVATE_SYSTEM", messages=[ChatMessage("user", "PRIVATE_TEXT")])
        assert failure.value.cause_kind == "rate_limit"
        notice = ChatbotCog._chat_failure_text(failure.value, had_images=False)
        assert "limite de uso" in notice and "22 hora" in notice
        state = safe_provider_state(router)
        assert 79100 < state["earliest_retry_seconds"] <= 79200
        assert "PRIVATE_" not in json.dumps(state) and "PRIVATE_" not in notice
    # A segunda consulta é bloqueada pelos circuitos, sem gastar pedidos.
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_real_reported_token_usage_reaches_operational_snapshot_without_content(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-test",))
    response = _groq("PRIVATE_REPLY", finish="stop")
    response["usage"] = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
    session = _Session(_Response(response))
    router = ProviderRouter(session, groq_key="PRIVATE_API_KEY")
    assert await router.chat(system="PRIVATE_SYSTEM", messages=[ChatMessage("user", "PRIVATE_REQUEST")]) == "PRIVATE_REPLY"
    state = safe_provider_state(router)
    assert state["last_request"]["usage"] == {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}
    assert state["last_request"]["outcome"] == "success"
    assert "PRIVATE_" not in json.dumps(state)


@pytest.mark.asyncio
async def test_two_host_rounds_share_one_contract_repair_budget_for_entire_turn(world, monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-test",))
    activate_registry(world)
    bad = _groq("PRIVATE_REJECTED", calls=[("get_operational_state", {"PRIVATE_ARGUMENT": "PRIVATE_VALUE"})])
    good = _groq(calls=[("get_operational_state", {})])
    good["choices"][0]["message"]["tool_calls"][0]["id"] = "confirmed-read"
    session = _Session(_Response(bad), _Response(good), _Response(bad),
                       _Response(_groq("UNEXPECTED_SECOND_REPAIR", finish="stop")))
    router = ProviderRouter(session, groq_key="PRIVATE_API_KEY")
    router.chat = AsyncMock(wraps=router.chat)
    world.cog._router = router
    messages = [ChatMessage("user", "PRIVATE_REQUEST")]
    with pytest.raises(AllProvidersExhausted) as failure:
        await run_loop(world, messages)
    assert failure.value.kind == "invalid_response"
    assert failure.value.diagnostic_code == "additional_properties"
    assert len(session.requests) == 3 and len(session.responses) == 1
    assert router.chat.await_count == 2
    first, second = router.chat.await_args_list
    assert first.kwargs["repair_state"] is second.kwargs["repair_state"]
    assert first.kwargs["repair_state"] == {"used": True}
    assert messages[-1].role == "tool" and messages[-1].tool_call_id == "confirmed-read"
    assert json.loads(messages[-1].content)["ok"] is True
    assert "PRIVATE_" not in json.dumps(router.diagnostics())
    world.message.reply.assert_not_awaited()
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_legacy_router_without_repair_keyword_remains_compatible(world):
    activate_registry(world)
    async def legacy(*, system, messages, temperature, tool_specs, text_provider_order, budget_seconds):
        return ChatReply("Resposta compatível.")
    world.cog._router = SimpleNamespace(chat=legacy)
    reply, _registry, _state = await run_loop(world)
    assert reply.text == "Resposta compatível."
