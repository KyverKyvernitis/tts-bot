"""Um anexo confirmado não volta a ser incerto por falha de metadados."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from cogs.chatbot import actions as actions_module
from cogs.chatbot.action_execution import ExecutionResult
from cogs.chatbot.action_policy import ActionDenied, build_action_context
from cogs.chatbot.action_protocol import ActionProposal, ChatReply
from cogs.chatbot.memory import MemoryEpoch
from test_chatbot_actions import environment, _request
from test_chatbot_action_policy import doc


async def _claimed(world, action="send_audio"):
    request = await _request(world, action, ask=False, bound=False)
    assert await world.service.store.arm_automatic(request["request_id"])
    claimed = await world.service.store.claim_automatic(request["request_id"], guild_id=10,
                                                       channel_id=30, actor_id=1)
    assert claimed is not None
    claimed.update(memory_epoch={"global_generation": 0, "guild_generation": 0, "user_generation": 0},
                   visibility_scope="channel:30", original_user_text="Minha pergunta de verdade")
    world.cog._memory = type("Memory", (), {
        "capture_epoch": AsyncMock(return_value=MemoryEpoch(0, 0, 0))})()
    world.bot.get_cog("Chatbot")._memory = world.cog._memory
    return claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_confirmed_attachment_survives_index_failure_and_still_records_history(environment, action):
    w = environment
    request = await _claimed(w, action)
    w.cog._remember_sent_message.side_effect = RuntimeError("índice fora do ar")

    state, public_result = await w.service._execute_reserved(request, 1)

    assert state == "succeeded" and public_result == "Ação concluída."
    assert (await w.service.store.get(request["request_id"]))["state"] == "succeeded"
    w.executor.assert_awaited_once()
    w.cog._persist_turn.assert_awaited_once()
    assert w.cog._persist_turn.await_args.kwargs["user_message"] == "Minha pergunta de verdade"
    assert not w.chat.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", [None, "text", "auto", "audio"])
async def test_audio_override_comes_only_from_host_and_is_preserved_in_audio_sequence_step(environment, response_format):
    w = environment
    reply = ChatReply('response_format="audio"', (
        ActionProposal("ban_member", "m1", reason="spam"),
        ActionProposal("send_audio", text="fala válida"),
    ))
    context = await build_action_context(w.bot, w.message, w.config)

    plan = await w.service.plan(w.message, reply, context, w.config, response_format=response_format)

    assert len(plan.requests) == 2
    stored = await w.service.store.list_for_plan(plan.requests[0]["plan_id"])
    assert "response_format" not in stored[0]
    assert stored[1].get("response_format") == ("audio" if response_format == "audio" else None)
    w.executor.assert_not_awaited()
    assert not w.chat.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", ["_remember_sent_message", "_persist_turn"])
async def test_slow_metadata_is_bounded_and_does_not_reverse_delivery(environment, monkeypatch, metadata):
    w = environment
    request = await _claimed(w)
    blocked = asyncio.Event()

    async def pending(**kwargs):
        await blocked.wait()

    setattr(w.cog, metadata, AsyncMock(side_effect=pending))
    monkeypatch.setattr(actions_module, "_DELIVERY_METADATA_TIMEOUT_SECONDS", 0.01)

    state, _ = await w.service._execute_reserved(request, 1)

    assert state == "succeeded"
    assert (await w.service.store.get(request["request_id"]))["state"] == "succeeded"
    w.executor.assert_awaited_once()
    w.cog._remember_sent_message.assert_awaited_once()
    w.cog._persist_turn.assert_awaited_once()
    assert not w.chat.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", ["_remember_sent_message", "_persist_turn"])
async def test_cancel_after_attachment_preserves_receipt_and_propagates(environment, metadata):
    w = environment
    request = await _claimed(w)
    entered, blocked = asyncio.Event(), asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await blocked.wait()

    setattr(w.cog, metadata, AsyncMock(side_effect=pending))
    task = asyncio.create_task(w.service._execute_reserved(request, 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = await w.service.store.get(request["request_id"])
    assert saved["state"] == "succeeded" and saved["public_result"] == "Ação concluída."
    w.executor.assert_awaited_once()
    assert not w.chat.send.await_count


@pytest.mark.asyncio
async def test_cancelled_executor_can_return_confirmed_attachment_receipt(environment):
    w = environment
    request = await _claimed(w)
    sent, blocked = asyncio.Event(), asyncio.Event()

    async def delivered_then_metadata(*args, **kwargs):
        sent.set()
        try:
            await blocked.wait()
        except asyncio.CancelledError:
            return ExecutionResult("Áudio enviado.", message_id=89)

    w.executor.side_effect = delivered_then_metadata
    task = asyncio.create_task(w.service._execute_reserved(request, 1))
    await asyncio.wait_for(sent.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = await w.service.store.get(request["request_id"])
    assert saved["state"] == "succeeded" and saved["public_result"] == "Áudio enviado."
    w.executor.assert_awaited_once()
    assert not w.chat.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_receipt", [False, True])
async def test_cancel_without_confirmed_attachment_remains_uncertain(environment, queue_receipt):
    w = environment
    request = await _claimed(w)
    entered, blocked = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        try:
            await blocked.wait()
        except asyncio.CancelledError:
            if queue_receipt:
                return ExecutionResult("Fala enfileirada.")
            raise

    w.executor.side_effect = pending
    task = asyncio.create_task(w.service._execute_reserved(request, 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await w.service.store.get(request["request_id"]))["state"] == "uncertain"
    w.executor.assert_awaited_once()
    assert not w.chat.send.await_count


@pytest.mark.asyncio
async def test_cancel_during_terminal_cas_does_not_cancel_or_repeat_committed_write(environment):
    w = environment
    request = await _claimed(w)
    entered, release = asyncio.Event(), asyncio.Event()
    original_finish = w.service.store.finish
    cancelled = []

    async def pending_finish(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return await original_finish(*args, **kwargs)

    w.service.store.finish = AsyncMock(side_effect=pending_finish)
    task = asyncio.create_task(w.service._execute_reserved(request, 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not cancelled
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await w.service.store.get(request["request_id"]))["state"] == "succeeded"
    w.service.store.finish.assert_awaited_once()
    w.executor.assert_awaited_once()
    assert not cancelled and not w.chat.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("action,status,state,next_state", [
    ("speak_voice", "enqueued", "succeeded", "created"),
    ("speak_voice", "skipped", "failed", "cancelled"),
    ("speak_voice", "uncertain", "uncertain", "cancelled"),
    ("send_audio", "skipped", "succeeded", "created"),
    ("send_audio", "uncertain", "succeeded", "created"),
])
async def test_required_call_partial_delivery_stops_successor_without_sending_again(
        environment, action, status, state, next_state):
    w = environment
    plan = await w.service.store.create_plan([
        doc(action, target=1, voice=20, ask=False),
        doc("ban_member", target=3, ask=True),
    ])
    first = plan[0]
    assert await w.service.store.arm_automatic(first["request_id"])
    claimed = await w.service.store.claim_automatic(first["request_id"], guild_id=10, channel_id=30, actor_id=1)
    w.executor.return_value = ExecutionResult("Áudio enviado.", 88, True, status)
    w.cog._remember_sent_message.side_effect = RuntimeError("índice indisponível")

    outcome, public_result = await w.service._execute_reserved(claimed, 1)

    stored = await w.service.store.list_for_plan(first["plan_id"])
    assert outcome == state and [item["state"] for item in stored] == [state, next_state]
    if action == "speak_voice":
        assert "áudio está no chat" in public_result.lower()
        assert "reproduzido" not in public_result
    w.executor.assert_awaited_once()
    assert not w.chat.send.await_count and not w.supervisor.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("status,state", [("skipped", "failed"), ("uncertain", "uncertain")])
async def test_cancel_after_partial_call_preserves_partial_outcome(environment, status, state):
    w = environment
    request = await _claimed(w, "speak_voice")
    w.executor.return_value = ExecutionResult("Áudio enviado.", 88, True, status)
    entered, blocked = asyncio.Event(), asyncio.Event()

    async def metadata(**kwargs):
        entered.set()
        await blocked.wait()

    w.cog._remember_sent_message.side_effect = metadata
    task = asyncio.create_task(w.service._execute_reserved(request, 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await w.service.store.get(request["request_id"]))["state"] == state
    w.executor.assert_awaited_once()
    assert not w.chat.send.await_count


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_definite_audio_failure_falls_back_once_and_isolates_text_metadata(environment, action):
    w = environment
    request = await _claimed(w, action)
    w.executor.side_effect = ActionDenied("Não consegui gerar o áudio.")
    w.cog._remember_sent_message.side_effect = RuntimeError("índice indisponível")
    w.cog._reply_store = type("Replies", (), {
        "record_sent": AsyncMock(side_effect=RuntimeError("memória indisponível"))})()

    state, public_result = await w.service._execute_reserved(request, 1)

    assert state == "failed" and public_result == "Não consegui gerar o áudio."
    assert (await w.service.store.get(request["request_id"]))["state"] == "failed"
    w.chat.send.assert_awaited_once()
    assert w.chat.send.await_args.args == (request["payload"]["text"],)
    w.cog._reply_store.record_sent.assert_awaited_once()
    w.cog._persist_turn.assert_awaited_once()
    w.executor.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_after_text_fallback_preserves_failed_audio_without_resending(environment):
    w = environment
    request = await _claimed(w, "speak_voice")
    w.executor.side_effect = ActionDenied("Não consegui gerar o áudio.")
    entered, blocked = asyncio.Event(), asyncio.Event()

    async def metadata(**kwargs):
        entered.set()
        await blocked.wait()

    w.cog._remember_sent_message.side_effect = metadata
    task = asyncio.create_task(w.service._execute_reserved(request, 1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await w.service.store.get(request["request_id"]))["state"] == "failed"
    w.chat.send.assert_awaited_once()
    w.executor.assert_awaited_once()
