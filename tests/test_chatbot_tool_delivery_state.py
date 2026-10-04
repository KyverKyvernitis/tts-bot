"""Efeitos reais do turno sobrevivem ao fechamento, sem replay nem novas calls."""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.action_protocol import ChatReply, NativeToolCall
from cogs.chatbot.cog import ChatbotCog, TriggerInfo, _TURN_DELIVERY_RECEIPT
from cogs.chatbot.preferences import ConversationPreferences
from cogs.chatbot.providers import AllProvidersExhausted, ChatMessage
from cogs.chatbot.tool_registry import ToolRegistry, ToolSpec
from cogs.chatbot.voice_context import build_voice_snapshot
from tests.test_chatbot_audio_format import turn, activate_processing_turn


@pytest.fixture
def delivery(turn, monkeypatch):
    registry = ToolRegistry()
    registry.runtime = SimpleNamespace(response_format=None, action_context=None, action_draft=None,
        action_proposals=[], prepared_actions=[], proposals_invalid=False, proposals_error="")
    turn.cog._preferences = object()
    turn.events = []
    turn.registry = registry

    async def convert(arguments):
        turn.events.append("convert")
        kind = arguments.get("format", "audio")
        if kind == "audio":
            sent = await turn.message.reply(None, files=[discord.File(io.BytesIO(b"confirmed mp3"), filename="audio.mp3")])
        else:
            sent = await turn.message.reply("Texto confirmado.")
        turn.cog.note_public_delivery(sent.id)
        return {"ok": True, "status": "audio_sent" if kind == "audio" else "reply_sent",
                "data": {"message_id": str(sent.id)}}

    async def query(_arguments):
        turn.events.append("query")
        return {"ok": True, "status": "available", "data": {"value": 42}}

    async def image(_arguments):
        turn.events.append("image")
        sent = await turn.message.reply(None, files=[discord.File(io.BytesIO(b"confirmed png"), filename="image.png")])
        turn.cog.note_public_delivery(sent.id)
        return {"ok": True, "status": "image_sent", "data": {"message_id": str(sent.id)}}

    async def music(_arguments):
        turn.events.append("music")
        return {"ok": True, "status": "music_control_applied", "data": {"action": "pause", "secret": "PRIVATE_RAW_DATA"}}

    def register(name, handler, *, permission="read", properties=None):
        registry.register(ToolSpec(name, "Capacidade declarada.",
            {"type": "object", "properties": properties or {}, "additionalProperties": False},
            handler=handler, permission=permission))

    register("convert_reply_audio", convert, permission="automatic_effect",
             properties={"format": {"type": "string", "enum": ["audio", "text"]}})
    register("read_state", query)
    register("generate_image", image, permission="automatic_effect")
    register("control_music", music, permission="automatic_effect")
    monkeypatch.setattr("cogs.chatbot.tool_runtime.build_tool_registry", AsyncMock(return_value=registry))

    async def refresh(_registry):
        return build_voice_snapshot(turn.cog.bot, turn.message.guild, turn.message.author)
    monkeypatch.setattr("cogs.chatbot.tool_runtime.refresh_tool_context", refresh)
    turn.register = register
    return turn


def call(identifier, name, **arguments):
    return NativeToolCall(identifier, name, arguments)


def tool_evidence(messages):
    item = next(message for message in messages
                if message.role == "user" and message.content.startswith("[FERRAMENTAS;"))
    return json.loads(item.content.split("\n", 2)[1])


def reply_calls(*calls):
    return ChatReply("PRIVATE_INTERMEDIATE_PREVIEW", tool_calls=calls)


def unavailable():
    return AllProvidersExhausted("fake all unavailable", kind="rate_limit", status=429)


async def loop(turn):
    messages = [ChatMessage("user", "pedido")]
    result, registry, state = await turn.cog._run_native_tools(
        message=turn.message, system="Converse com as ferramentas reais.", messages=messages,
        config=turn.config, preferences=ConversationPreferences(), epoch=turn.epoch,
        visibility="channel:20", reply_target=None, action_context=None, temperature=.8, router_options={})
    return result, registry, state, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "text"])
async def test_conversion_finishes_without_requesting_model_again(delivery, kind):
    delivery.cog._router.chat.side_effect = [reply_calls(call("convert", "convert_reply_audio", format=kind)), unavailable()]
    assert await delivery.cog._generate_and_send(delivery.message, "converta a anterior")
    delivery.cog._router.chat.assert_awaited_once()
    delivery.message.reply.assert_awaited_once()
    assert delivery.message.reply.await_args.args[0] == (None if kind == "audio" else "Texto confirmado.")
    assert delivery.events == ["convert"]
    delivery.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert not delivery.cog._processing_indicator._channels


@pytest.mark.asyncio
async def test_conversion_finishes_after_all_legitimate_calls_in_the_same_batch(delivery):
    delivery.cog._router.chat.return_value = reply_calls(call("convert", "convert_reply_audio"), call("music", "control_music"))
    result, _registry, state, messages = await loop(delivery)
    assert delivery.events == ["convert", "music"]
    assert state["delivered"] and state["response_complete"] and not state["partial"]
    assert state["effects_confirmed"] == [{"name": "control_music", "status": "music_control_applied", "public_result": "Música pausada."}]
    assert result.text == "" and not result.tool_calls
    assert [item["tool"] for item in tool_evidence(messages)] == ["convert_reply_audio", "control_music"]
    assert not any(message.role == "tool" or message.tool_calls for message in messages)
    delivery.cog._router.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_failure_after_confirmed_image_is_silent_and_does_not_repeat(delivery):
    delivery.cog._router.chat.side_effect = [reply_calls(call("image", "generate_image")), unavailable()]
    assert await delivery.cog._generate_and_send(delivery.message, "desenhe")
    assert delivery.events == ["image"]
    assert delivery.cog._router.chat.await_count == 2
    delivery.message.reply.assert_awaited_once()
    assert delivery.message.reply.await_args.args == (None,)
    delivery.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_effect_without_public_delivery_gets_typed_operational_result(delivery):
    delivery.cog._router.chat.side_effect = [reply_calls(call("music", "control_music")), unavailable()]
    assert await delivery.cog._generate_and_send(delivery.message, "pause")
    assert delivery.message.reply.await_args.args == ("Música pausada.",)
    assert delivery.events == ["music"]
    delivery.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert "PRIVATE_RAW_DATA" not in delivery.message.reply.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("permission,status", [("read", "executed"), ("automatic_effect", "music_control_accepted"),
                                              ("automatic_effect", "enqueued"), ("automatic_effect", "pending")])
async def test_query_or_pending_effect_is_never_announced_as_completed(delivery, permission, status):
    async def pending(_arguments):
        return {"ok": True, "status": status, "data": {"action": "pause"}}
    delivery.register("other_operation", pending, permission=permission)
    delivery.cog._router.chat.side_effect = [reply_calls(call("other", "other_operation")), unavailable()]
    with pytest.raises(AllProvidersExhausted):
        await loop(delivery)
    delivery.message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_batch_reports_failure_and_keeps_other_valid_effects(delivery):
    async def denied(_arguments):
        delivery.events.append("denied-query")
        return {"ok": False, "status": "failed", "reason": "Não consigo consultar esse membro."}
    delivery.register("denied_query", denied)
    delivery.cog._router.chat.return_value = reply_calls(call("convert", "convert_reply_audio"),
        call("denied", "denied_query"), call("music", "control_music"))
    assert await delivery.cog._generate_and_send(delivery.message, "três partes")
    assert delivery.events == ["convert", "denied-query", "music"]
    assert [item.args[0] for item in delivery.message.reply.await_args_list] == [None, "Não consigo consultar esse membro."]
    delivery.cog._router.chat.assert_awaited_once()
    delivery.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_delivery_stops_following_effects_and_never_claims_completion(delivery):
    async def uncertain(_arguments):
        return {"ok": False, "status": "uncertain", "reason": "Não consegui confirmar o envio."}
    delivery.register("uncertain_delivery", uncertain, permission="automatic_effect")
    delivery.cog._router.chat.return_value = reply_calls(call("uncertain", "uncertain_delivery"), call("music", "control_music"))
    result, _registry, state, _messages = await loop(delivery)
    assert state["uncertain"] and not state["delivered"] and not state["effects_confirmed"]
    assert not delivery.events and result.text == ""
    delivery.message.reply.assert_not_awaited()
    delivery.cog._router.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_effect_deadline_cancels_work_without_replay_or_following_effect(delivery, monkeypatch):
    monkeypatch.setattr(C, "TOOL_LOOP_BUDGET_SECONDS", .025)
    cancelled = asyncio.Event()
    async def blocked(_arguments):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    delivery.register("blocked_effect", blocked, permission="automatic_effect")
    delivery.cog._router.chat.return_value = reply_calls(call("blocked", "blocked_effect"), call("music", "control_music"))
    result, _registry, state, _messages = await asyncio.wait_for(loop(delivery), timeout=.5)
    assert cancelled.is_set() and state["uncertain"]
    assert not delivery.events and not state["effects_confirmed"] and not result.text
    delivery.cog._router.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_four_successful_query_rounds_have_one_final_round_with_compact_evidence(delivery):
    delivery.cog._router.chat.side_effect = [reply_calls(call(str(i), "read_state")) for i in range(4)] + [ChatReply("O resultado é 42.")]
    result, _registry, state, messages = await loop(delivery)
    assert result.text == "O resultado é 42." and not state.get("limit_reached")
    assert delivery.events == ["query"] * 4
    assert delivery.cog._router.chat.await_count == 5
    last = delivery.cog._router.chat.await_args.kwargs
    assert last["allow_tool_calls"] is False and last["tool_specs"] == ()
    evidence = tool_evidence(messages)
    assert [item["tool"] for item in evidence] == ["read_state"] * 4
    assert not any(message.role == "tool" or message.tool_calls for message in messages)


@pytest.mark.asyncio
async def test_eight_calls_allow_only_the_final_response_and_no_ninth_effect(delivery):
    delivery.cog._router.chat.side_effect = [reply_calls(*(call(str(i), "read_state") for i in range(8))), ChatReply("Terminei as consultas.")]
    result, _registry, state, _messages = await loop(delivery)
    assert result.text == "Terminei as consultas." and not state.get("limit_reached")
    assert delivery.events == ["query"] * 8
    assert delivery.cog._router.chat.await_count == 2
    assert delivery.cog._router.chat.await_args.kwargs["allow_tool_calls"] is False


@pytest.mark.asyncio
async def test_final_round_does_not_execute_calls_from_a_backend_ignoring_the_disable(delivery):
    delivery.cog._router.chat.side_effect = [reply_calls(call(str(i), "read_state")) for i in range(4)] + [reply_calls(call("forbidden-final", "control_music"))]
    result, _registry, state, _messages = await loop(delivery)
    assert state["limit_reached"] and not result.text and not result.tool_calls
    assert delivery.events == ["query"] * 4 and not state["effects_confirmed"]
    delivery.message.reply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cost,expected_requests", [(12, 5), (15, 4)])
async def test_final_round_shares_the_original_55_second_deadline(delivery, monkeypatch, cost, expected_requests):
    import cogs.chatbot.cog as cog_module
    clock = [100.0]
    monkeypatch.setattr(cog_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    requests = []
    async def model(**kwargs):
        requests.append(kwargs["budget_seconds"])
        if len(requests) <= 4:
            clock[0] += cost
            return reply_calls(call(str(len(requests)), "read_state"))
        return ChatReply("Resumo final.")
    delivery.cog._router.chat.side_effect = model
    result, _registry, state, _messages = await loop(delivery)
    assert len(requests) == expected_requests
    assert requests[:4] == [47, 47-cost, 47-cost*2, 47-cost*3]
    if expected_requests == 5:
        assert requests[-1] == 7 and result.text == "Resumo final."
    else:
        assert state["deadline"] and not result.text


@pytest.mark.asyncio
async def test_normal_model_attempt_reserves_time_for_final_when_it_uses_its_whole_budget(delivery, monkeypatch):
    import cogs.chatbot.cog as cog_module
    from cogs.chatbot.providers import ProviderError
    clock = [100.0]
    monkeypatch.setattr(cog_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    budgets = []
    async def model(**kwargs):
        budgets.append(kwargs["budget_seconds"])
        if len(budgets) == 1:
            clock[0] += 3
            return reply_calls(call("known-query", "read_state"))
        if len(budgets) == 2:
            clock[0] += kwargs["budget_seconds"]
            raise ProviderError("operation deadline", kind="deadline")
        assert kwargs["allow_tool_calls"] is False
        return ChatReply("O dado confirmado é 42.")
    delivery.cog._router.chat.side_effect = model
    result, _registry, state, messages = await loop(delivery)
    assert budgets == [47, 44, 8]
    assert result.text == "O dado confirmado é 42." and not state.get("deadline")
    assert delivery.events == ["query"]
    assert [item["tool"] for item in tool_evidence(messages)] == ["read_state"]


@pytest.mark.asyncio
async def test_slow_read_cannot_consume_the_reserved_final_response_time(delivery, monkeypatch):
    monkeypatch.setattr(C, "TOOL_LOOP_BUDGET_SECONDS", .12)
    cancelled = asyncio.Event()
    async def slow_read(_arguments):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    delivery.register("slow_read", slow_read)
    delivery.cog._router.chat.side_effect = [reply_calls(call("known", "read_state"), call("slow", "slow_read")),
                                            ChatReply("Consegui o primeiro dado; a segunda consulta demorou.")]
    result, _registry, state, messages = await asyncio.wait_for(loop(delivery), timeout=.5)
    assert cancelled.is_set() and not state.get("deadline") and not state["uncertain"]
    assert result.text == "Consegui o primeiro dado; a segunda consulta demorou."
    assert delivery.cog._router.chat.await_count == 2
    final = delivery.cog._router.chat.await_args.kwargs
    assert final["allow_tool_calls"] is False and 0 < final["budget_seconds"] <= .018
    assert tool_evidence(messages)[-1]["result"]["status"] == "deadline"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["_remember_sent_message", "_record_delivered_reply", "_mirror_sent_audio", "_persist_turn"])
async def test_turn_timeout_after_confirmed_send_does_not_publish_a_second_error(delivery, monkeypatch, stage):
    activate_processing_turn(delivery)
    delivery.cog._resolve_trigger.return_value = TriggerInfo("oi", "mention")
    delivery.cog._router.chat.return_value = ChatReply("Resposta confirmada.")
    monkeypatch.setattr(C, "CHAT_TURN_TIMEOUT_SECONDS", .025)
    cancelled = asyncio.Event()
    async def blocked(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    setattr(delivery.cog, stage, AsyncMock(side_effect=blocked))
    await asyncio.wait_for(delivery.cog._process_chat(delivery.message), timeout=.5)
    assert cancelled.is_set()
    delivery.message.reply.assert_awaited_once()
    assert delivery.message.reply.await_args.args == (None,)
    assert not delivery.cog._processing_indicator._channels and not delivery.cog._admission._inflight_users
    assert _TURN_DELIVERY_RECEIPT.get() is None


@pytest.mark.asyncio
async def test_external_cancellation_after_send_still_propagates_and_cleans_processing(delivery):
    activate_processing_turn(delivery)
    delivery.cog._resolve_trigger.return_value = TriggerInfo("oi", "mention")
    delivery.cog._router.chat.return_value = ChatReply("Resposta confirmada.")
    entered = asyncio.Event()
    async def blocked(**_kwargs):
        entered.set()
        await asyncio.Event().wait()
    delivery.cog._remember_sent_message = AsyncMock(side_effect=blocked)
    task = asyncio.create_task(delivery.cog._process_chat(delivery.message))
    await asyncio.wait_for(entered.wait(), timeout=.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    delivery.message.reply.assert_awaited_once()
    assert not delivery.cog._processing_indicator._channels and not delivery.cog._admission._inflight_users
    assert _TURN_DELIVERY_RECEIPT.get() is None


def test_delivery_receipt_requires_a_positive_confirmed_message_id(delivery):
    receipt = {"delivered": False}
    token = _TURN_DELIVERY_RECEIPT.set(receipt)
    try:
        for value in (None, 0, -1, True, 1.5, "1.5", "not-an-id"):
            delivery.cog.note_public_delivery(value)
            assert not receipt["delivered"]
        delivery.cog.note_public_delivery(50)
        assert receipt["delivered"]
    finally:
        _TURN_DELIVERY_RECEIPT.reset(token)


@pytest.mark.parametrize("cause,delay,phrase", [("rate_limit", 12, "12 segundo"), ("rate_limit", 61, "2 minuto"),
    ("network", 20, "conexão"), ("auth", 30, "configuração"), ("", None, "pausa temporária")])
def test_cooldown_explains_cause_and_wait_without_exposing_models(cause, delay, phrase):
    exc = AllProvidersExhausted("PRIVATE_MODEL_ERROR", kind="cooldown", retry_after=delay)
    exc.cause_kind = cause
    text = ChatbotCog._chat_failure_text(exc, had_images=False)
    assert phrase in text and "PRIVATE_MODEL_ERROR" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["move", "disconnect", "reconnect"])
async def test_legacy_audio_keeps_chat_file_and_skips_call_if_captured_session_changes(delivery, change):
    delivery.cog._router.chat.return_value = ChatReply("A mesma fala no arquivo.")
    async def synthesize(**_kwargs):
        if change == "move":
            delivery.message.guild.voice_client.channel.id = 778
        elif change == "disconnect":
            delivery.message.guild.voice_client = None
        delivery.tts.chatbot_voice_session_ref.return_value = "changed-session" if change != "disconnect" else None
        return b"unchanged exact mp3"
    delivery.tts.synthesize_chatbot_attachment.side_effect = synthesize
    assert await delivery.cog._generate_and_send(delivery.message, "fale")
    delivery.message.reply.assert_awaited_once()
    assert delivery.message.reply.await_args.args == (None,)
    assert delivery.message.reply.await_args.kwargs["files"][0].fp.read() == b"unchanged exact mp3"
    delivery.tts.chatbot_mirror_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_audio_never_targets_a_call_created_after_synthesis_started(delivery):
    delivery.cog._router.chat.return_value = ChatReply("Arquivo no chat.")
    delivery.message.guild.voice_client = None
    delivery.tts.chatbot_voice_session_ref.return_value = None
    async def synthesize(**_kwargs):
        delivery.message.guild.voice_client = SimpleNamespace(channel=SimpleNamespace(id=777))
        delivery.tts.chatbot_voice_session_ref.return_value = "new-session"
        return b"same audio bytes"
    delivery.tts.synthesize_chatbot_attachment.side_effect = synthesize
    assert await delivery.cog._generate_and_send(delivery.message, "fale")
    delivery.message.reply.assert_awaited_once()
    delivery.tts.chatbot_mirror_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_audio_mirror_receives_captured_channel_and_token_without_changing_audio(delivery):
    delivery.cog._router.chat.return_value = ChatReply("Fala original.")
    assert await delivery.cog._generate_and_send(delivery.message, "fale")
    arguments = delivery.tts.chatbot_mirror_audio.await_args.kwargs
    assert arguments["expected_voice_channel_id"] == 777
    assert arguments["expected_session_ref"] == "voice-session-10"
    assert arguments["audio"] == b"the exact mp3 bytes"
    assert delivery.tts.chatbot_voice_session_ref.call_args.kwargs == {"require_idle": False}


@pytest.mark.asyncio
async def test_external_cancellation_is_preserved_if_tool_metadata_returns_delivery_receipt(delivery):
    activate_processing_turn(delivery)
    delivery.cog._resolve_trigger.return_value = TriggerInfo("converta", "mention")
    entered = asyncio.Event()
    async def metadata_catches_cancel(_arguments):
        sent = await delivery.message.reply(None, files=[discord.File(io.BytesIO(b"sent once"), filename="audio.mp3")])
        delivery.cog.note_public_delivery(sent.id)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"ok": True, "status": "audio_sent", "data": {"message_id": str(sent.id)}}
    delivery.register("delivery_with_metadata", metadata_catches_cancel, permission="automatic_effect")
    delivery.cog._router.chat.return_value = reply_calls(call("only-delivery", "delivery_with_metadata"))
    task = asyncio.create_task(delivery.cog._process_chat(delivery.message))
    await asyncio.wait_for(entered.wait(), timeout=.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    delivery.message.reply.assert_awaited_once()
    delivery.cog._router.chat.assert_awaited_once()
    assert not delivery.cog._processing_indicator._channels and not delivery.cog._admission._inflight_users


@pytest.mark.asyncio
async def test_guard_change_after_confirmed_delivery_does_not_publish_redundant_error(delivery, monkeypatch):
    from cogs.chatbot.action_policy import ActionDenied
    count = 0
    async def refresh(_registry):
        nonlocal count
        count += 1
        if count > 1:
            raise ActionDenied("A memória desta conversa foi reiniciada.")
        return build_voice_snapshot(delivery.cog.bot, delivery.message.guild, delivery.message.author)
    monkeypatch.setattr("cogs.chatbot.tool_runtime.refresh_tool_context", refresh)
    delivery.cog._router.chat.return_value = reply_calls(call("image", "generate_image"))
    assert await delivery.cog._generate_and_send(delivery.message, "desenhe")
    delivery.message.reply.assert_awaited_once()
    delivery.cog._router.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporary_audio_selection_is_the_only_host_override_sent_to_plan(delivery):
    from cogs.chatbot.action_protocol import ActionProposal
    plan = SimpleNamespace(requests=[{"action": "speak_voice"}], public_error="")
    context = SimpleNamespace(actions=("speak_voice",), targets={"autor": delivery.message.author},
                              description="Fala disponível via sistema.")
    service = delivery.cog._actions = SimpleNamespace(ready=True, describe=AsyncMock(return_value=context),
        plan=AsyncMock(return_value=plan), content=lambda _plan: "", bind_and_start=AsyncMock())
    delivery.registry.runtime.action_context = context
    async def select(_arguments):
        delivery.registry.runtime.response_format = "audio"
        return {"ok": True, "status": "selected", "data": {"mode": "audio", "persistent": False}}
    async def propose(_arguments):
        delivery.registry.runtime.action_proposals.append(ActionProposal("speak_voice", text="PRIVATE_SPOKEN_TEXT"))
        return {"ok": True, "status": "proposed", "data": {"executed": False}}
    delivery.register("select_response_format", select, permission="automatic_effect")
    delivery.register("propor_acao", propose, permission="automatic_effect")
    delivery.cog.get_conversation_preferences.return_value = ConversationPreferences(mode="text")
    delivery.cog._router.chat.side_effect = [reply_calls(call("select", "select_response_format")),
                                            reply_calls(call("propose", "propor_acao"))]
    assert await delivery.cog._generate_and_send(delivery.message, "essa resposta em áudio")
    assert service.plan.await_args.kwargs["response_format"] == "audio"
    service.bind_and_start.assert_awaited_once_with(plan, None)
    delivery.message.reply.assert_not_awaited()
    delivery.tts.synthesize_chatbot_attachment.assert_not_awaited()
