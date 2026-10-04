"""Modelo, runtime e serviço reais: nenhum lote parcial, replay ou prévia privada."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.action_protocol import ChatReply, NativeToolCall, TOOL_NAME
from cogs.chatbot.preferences import ConversationPreferences, PreferenceStore
from cogs.chatbot.providers import ChatMessage, ProviderRouter
from cogs.chatbot.tool_runtime import drain_action_proposals
from tests.test_chatbot_action_flow import world, _Collection, _member, _public_output
from tests.test_chatbot_action_providers import _Session, _Response, _groq, _gemini


def activate_registry(w):
    # Banco fake; políticas, preferências, catálogo, cog e ActionService reais.
    w.cog._preferences = PreferenceStore(_Collection([]), memory=w.cog._memory)
    return ConversationPreferences()


async def run_loop(w, messages=None):
    context = await w.cog._actions.describe(w.message, w.config)
    return await w.cog._run_native_tools(
        message=w.message, system="Converse naturalmente e use apenas ferramentas disponíveis.",
        messages=messages if messages is not None else [ChatMessage("user", w.message.content)],
        config=w.config, preferences=ConversationPreferences(), epoch=w.epoch,
        visibility="channel:30", reply_target=None, action_context=context,
        temperature=.8, router_options={},
    )


def proposal(identifier, target):
    return NativeToolCall(identifier, TOOL_NAME,
                          {"action": "ban_member", "target_ref": target, "reason": "spam confirmado"})


@pytest.mark.asyncio
async def test_valid_ban_then_denied_ban_does_not_persist_or_schedule_partial_chain(world):
    activate_registry(world)
    world.members[4] = _member(world.guild, 4, rank=30)
    world.message.mentions.append(world.members[4])
    world.message.content = "Bana <@3> e <@4>"
    world.cog._router.chat.return_value = ChatReply("Não publique este rascunho.", tool_calls=(
        proposal("ban-valid", "m1"), proposal("ban-hierarchy-denied", "m2"),
    ))
    assert await world.cog._generate_and_send(world.message, world.message.content)
    assert not world.collection.docs and not world.cog._supervisor.jobs
    world.members[3].ban.assert_not_awaited()
    world.members[4].ban.assert_not_awaited()
    world.channel.send.assert_not_awaited()
    world.cog._maybe_generate_tts.assert_not_awaited()
    assert "Não publique este rascunho" not in _public_output(world)


@pytest.mark.asyncio
async def test_denied_proposal_invalidates_runtime_drain_not_only_cog_rendering(world):
    activate_registry(world)
    world.members[4] = _member(world.guild, 4, rank=30)
    world.message.mentions.append(world.members[4])
    world.cog._router.chat.return_value = ChatReply("", tool_calls=(
        proposal("ban-valid", "m1"), proposal("ban-denied", "m2"),
    ))
    reply, registry, state = await run_loop(world)
    assert state["action_failed"] and not reply.text and not reply.proposals
    assert drain_action_proposals(registry) == ()
    assert not registry.runtime.prepared_actions
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_private_audio_then_denied_ban_never_synthesizes_or_reveals_transcript(world, monkeypatch):
    activate_registry(world)
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-native",))
    world.members[4] = _member(world.guild, 4, rank=30)
    world.message.mentions.append(world.members[4])
    secret = "PRIVATE_TRANSCRIPT_que_so_poderia_ser_falado"
    session = _Session(_Response(_groq(secret, calls=[
        (TOOL_NAME, {"action": "send_audio", "text": secret}),
        (TOOL_NAME, {"action": "ban_member", "target_ref": "m2", "reason": "spam confirmado"}),
    ])))
    world.cog._router = ProviderRouter(session, groq_key="private-key")
    assert await world.cog._generate_and_send(world.message, "responda por áudio e bana <@4>")
    assert len(session.requests) == 1
    assert not world.collection.docs and not world.cog._supervisor.jobs
    assert secret not in _public_output(world)
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    world.cog._maybe_generate_tts.assert_not_awaited()
    world.members[4].ban.assert_not_awaited()


@pytest.mark.asyncio
async def test_effect_deadline_cancels_handler_and_stops_replay_and_model_followup(world, monkeypatch):
    activate_registry(world)
    monkeypatch.setattr(C, "TOOL_LOOP_BUDGET_SECONDS", .025)
    cancelled = asyncio.Event()

    async def delayed_generation(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    world.cog._image_service = SimpleNamespace(generate=AsyncMock(side_effect=delayed_generation))
    call = NativeToolCall("first-image", "generate_image", {"prompt": "um gato"})
    replay = NativeToolCall("second-image", "generate_image", {"prompt": "um gato"})
    world.cog._router.chat.return_value = ChatReply("prévia que não deve sair", tool_calls=(call, replay))
    messages = [ChatMessage("user", "crie uma imagem de um gato")]
    started = time.monotonic()
    reply, registry, state = await run_loop(world, messages)
    assert time.monotonic() - started < .5
    assert cancelled.is_set() and state["uncertain"] and not state["delivered"]
    assert not reply.text and not drain_action_proposals(registry)
    results = {message.tool_call_id: json.loads(message.content)
               for message in messages if message.role == "tool"}
    assert results["first-image"]["status"] == "uncertain"
    assert results["second-image"]["status"] == "not_executed"
    assert results["second-image"]["ok"] is False
    world.cog._image_service.generate.assert_awaited_once()
    world.cog._router.chat.assert_awaited_once()
    world.message.reply.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_read_deadline_does_not_claim_effect_or_publish_partial_text(world, monkeypatch):
    activate_registry(world)
    monkeypatch.setattr(C, "TOOL_LOOP_BUDGET_SECONDS", .025)
    cancelled = asyncio.Event()
    original_get = world.cog._preferences.get_current
    calls = 0

    async def delayed_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:  # Leitura de preferência para construir o prompt.
            return await original_get(*args, **kwargs)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    world.cog._preferences.get_current = delayed_read
    world.cog._router.chat.return_value = ChatReply("rascunho", tool_calls=(
        NativeToolCall("read-preferences", "get_conversation_preferences", {}),
    ))
    messages = [ChatMessage("user", "qual formato você usa?")]
    reply, _registry, state = await run_loop(world, messages)
    assert cancelled.is_set() and not state["uncertain"] and not state["delivered"]
    assert "rascunho" not in reply.text
    assert json.loads(messages[-1].content)["status"] == "deadline"
    world.cog._router.chat.assert_awaited_once()
    world.message.reply.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_prompt_preference_read_also_respects_turn_budget_before_requesting_model(world, monkeypatch):
    activate_registry(world)
    monkeypatch.setattr(C, "TOOL_LOOP_BUDGET_SECONDS", .025)
    cancelled = asyncio.Event()

    async def blocked_preferences(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    world.cog._preferences.get_current = blocked_preferences
    await asyncio.wait_for(run_loop(world), timeout=.5)
    assert cancelled.is_set()
    world.cog._router.chat.assert_not_awaited()
    world.message.reply.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_actual_native_http_roundtrip_preserves_call_ids_and_returns_only_final_text(world, monkeypatch, provider):
    activate_registry(world)
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-native",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-native",))
    if provider == "groq":
        first = _groq("rascunho intermediário", calls=[("get_operational_state", {})])
        first["choices"][0]["message"]["tool_calls"][0]["id"] = "native-state-id"
        second = _groq("Estou conectado e não escuto a call.", finish="stop")
    else:
        first = {"candidates": [{"content": {"parts": [
            {"text": "rascunho intermediário"},
            {"functionCall": {"id": "native-state-id", "name": "get_operational_state", "args": {}},
             "thoughtSignature": "native-opaque-signature"},
        ]}, "finishReason": "STOP"}]}
        second = _gemini("Estou conectado e não escuto a call.")
    session = _Session(_Response(first), _Response(second))
    world.cog._router = ProviderRouter(session, **{f"{provider}_key": "private-key"})
    messages = [ChatMessage("user", "você está na call?")]
    reply, registry, state = await run_loop(world, messages)
    assert reply.text == "Estou conectado e não escuto a call." and not reply.tool_calls
    assert not state["delivered"] and not state["uncertain"] and not drain_action_proposals(registry)
    assert len(session.requests) == 2
    assistant, tool_result = messages[-2:]
    assert assistant.tool_calls[0].id == tool_result.tool_call_id == "native-state-id"
    assert tool_result.name == "get_operational_state"
    result = json.loads(tool_result.content)
    assert result["ok"] and result["data"]["voice"]["connected"] and result["data"]["voice"]["listening"] is False
    payload = session.requests[1][1]["json"]
    if provider == "groq":
        assert payload["messages"][-1]["tool_call_id"] == "native-state-id"
        assert payload["messages"][-2]["tool_calls"][0]["id"] == "native-state-id"
    else:
        native_part = payload["contents"][-2]["parts"][-1]
        assert native_part["thoughtSignature"] == "native-opaque-signature"
        assert payload["contents"][-1]["parts"][0]["functionResponse"]["id"] == "native-state-id"
    world.message.reply.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs
