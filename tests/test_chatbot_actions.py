"""Fluxo integrado de pedidos, autorização atual e execução única."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import UUID, uuid4

import pytest
import discord

from cogs.chatbot import actions as actions_module
from cogs.chatbot.action_execution import ActionExecutionUncertain, ExecutionResult
from cogs.chatbot.action_policy import ActionDenied, build_action_context
from cogs.chatbot.action_protocol import ActionProposal, ChatReply
from cogs.chatbot.action_store import ActionStore
from cogs.chatbot.actions import ActionService
from cogs.chatbot.memory import MemoryEpoch
from test_chatbot_action_policy import doc, expanded as policy_expanded, in_call, world as policy_world
from test_chatbot_action_store import _Clock, _Collection


class _Supervisor:
    def __init__(self):
        self.pending = []
        self.names = []

    def create(self, coroutine, *, name):
        self.pending.append(coroutine)
        self.names.append(name)

    async def drain(self):
        while self.pending:
            pending, self.pending = self.pending, []
            await asyncio.gather(*pending)

    def close(self):
        for coroutine in self.pending:
            coroutine.close()
        self.pending.clear()


def _interaction(world, actor=2, *, message_id=60, guild_id=10, channel_id=30):
    notices = []
    state = {"done": False}

    async def send(text, **kwargs):
        notices.append((text, kwargs))
        state["done"] = True

    async def defer(**kwargs):
        state["done"] = True

    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id), channel_id=channel_id,
        message=SimpleNamespace(id=message_id), user=world.members[actor],
        response=SimpleNamespace(is_done=lambda: state["done"],
                                 send_message=AsyncMock(side_effect=send),
                                 defer=AsyncMock(side_effect=defer)),
        followup=SimpleNamespace(send=AsyncMock(side_effect=send)), notices=notices,
    )


@pytest.fixture
def environment(monkeypatch):
    world = policy_world.__wrapped__()
    in_call(world, bot=True)
    world.config.enabled = True
    world.config.allows_channel = lambda channel_id, **kwargs: channel_id == world.chat.id
    world.bot.get_channel = world.guild.get_channel
    world.bot.add_view = Mock()
    world.card = SimpleNamespace(id=60, author=world.bot.user, edit=AsyncMock(), delete=AsyncMock())
    world.chat.fetch_message = AsyncMock(return_value=world.card)
    world.chat.send.return_value = world.card
    supervisor = _Supervisor()
    cog = SimpleNamespace(bot=world.bot, _config=world.store, _supervisor=supervisor,
                          _sanitize_model_reply=lambda text: text,
                          _remember_sent_message=AsyncMock(), _persist_turn=AsyncMock())
    world.cog = cog
    world.supervisor = supervisor
    world.coll, world.clock = _Collection(), _Clock()
    world.service = ActionService(cog, world.coll)
    world.service.store = ActionStore(world.coll, clock=world.clock)
    world.service.ready = True
    world.executor = AsyncMock(return_value=ExecutionResult("Ação concluída.", message_id=88))
    monkeypatch.setattr(actions_module, "execute_action", world.executor)
    monkeypatch.setattr(actions_module.C, "SAFE_MODE", False)
    yield world
    supervisor.close()


async def _request(world, action="ban_member", *, ask=True, target=None, bound=True, requester=1):
    data = doc(action, target=(3 if action == "ban_member" else 1) if target is None else target, voice=20, ask=ask)
    data["requester_id"] = requester
    data["base_reply"] = ""
    created = await world.service.store.create(data)
    if bound:
        assert await world.service.store.bind(created["request_id"], 60)
    return await world.service.store.get(created["request_id"])


@pytest.mark.asyncio
async def test_optional_audio_in_mixed_plan_hides_every_persisted_base_reply(environment):
    w = environment
    context = await build_action_context(w.bot, w.message, w.config)
    p = await w.service.plan(w.message, ChatReply("fala privada", (
        ActionProposal("send_audio", text="fala privada", ask_permission=True),
        ActionProposal("ban_member", "m1", reason="spam"))), context, w.config)
    assert len(p.requests) == 2 and p.base_reply == ""
    assert p.requests[0]["ask_permission"] is False and p.requests[1]["state"] == "blocked"
    assert not w.service.content(p) and w.service.view(p) is None
    assert all(not r["base_reply"] for r in w.coll.docs)

@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["unavailable_call", "invalid_speech"])
async def test_optional_audio_tries_second_valid_proposal_without_revealing_either_speech(environment, first_failure):
    w = environment
    if first_failure == "unavailable_call":
        w.members[1].voice = None
        first = ActionProposal("speak_voice", text="primeiro segredo")
    else:
        first = ActionProposal("send_audio", text=" ")
    p = await w.service.plan(w.message, ChatReply("segredo", (first, ActionProposal("ban_member", "m1", reason="spam"))),
                             await build_action_context(w.bot, w.message, w.config), w.config)
    assert not p.requests and not w.coll.docs and p.public_error
    assert "segredo" not in p.public_error and not w.supervisor.pending

@pytest.mark.asyncio
async def test_preparation_error_reports_host_reason_without_private_tool_arguments(environment):
    w = environment
    secret = "SEGREDO " * 300
    p = await w.service.plan(w.message, ChatReply(secret, (ActionProposal("send_audio", text=secret),)),
                             await build_action_context(w.bot, w.message, w.config), w.config)
    assert not p.requests and p.base_reply == "" and "curta" in p.public_error
    assert secret not in p.public_error

@pytest.mark.asyncio
async def test_hidden_speech_remains_hidden_when_card_refreshes_after_rejection(environment):
    w = environment
    p = await w.service.plan(w.message, ChatReply("segredo", (
        ActionProposal("ban_member", "m1", reason="spam"),
        ActionProposal("send_audio", text="segredo"))), await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    await w.service.handle_interaction(_interaction(w), p.requests[0]["request_id"], approve=False)
    assert [r["state"] for r in await w.service.store.list_for_plan(p.requests[0]["plan_id"])] == ["rejected", "cancelled"]
    assert "segredo" not in str(w.chat.send.call_args_list)
    w.card.delete.assert_awaited_once()
    w.executor.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [dict(message_id=999), dict(channel_id=999), dict(guild_id=999)])
async def test_button_copied_to_other_message_or_scope_cannot_authorize(environment, changes):
    w = environment
    r = await _request(w)
    i = _interaction(w, **changes)
    await w.service.handle_interaction(i, r["request_id"], approve=True)
    assert (await w.service.store.get(r["request_id"]))["state"] == "pending"
    assert not w.supervisor.pending and "não pertence" in i.notices[0][0]

@pytest.mark.asyncio
async def test_unbound_or_already_consumed_request_cannot_execute(environment):
    w = environment
    r = await _request(w, bound=False)
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    assert not w.supervisor.pending
    await w.service.store.bind(r["request_id"], 60)
    await w.service.store.reject(r["request_id"], guild_id=10, channel_id=30, message_id=60, actor_id=2)
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    assert not w.supervisor.pending

@pytest.mark.asyncio
async def test_optional_audio_accepts_common_requester_and_rejects_other_staff(environment):
    w = environment
    r = await _request(w, "send_audio", ask=False, bound=False)
    await w.service.store.arm_automatic(r["request_id"])
    await w.service._start_automatic(await w.service.store.get(r["request_id"]))
    assert w.executor.await_args.kwargs["actor_id"] == 1
    assert (await w.service.store.get(r["request_id"]))["state"] == "succeeded"
    assert not w.chat.send.await_count and not w.card.delete.await_count

@pytest.mark.asyncio
async def test_common_requester_can_approve_speech_in_current_call(environment):
    w = environment
    r = await _request(w, "speak_voice", ask=False, bound=False)
    await w.service.store.arm_automatic(r["request_id"])
    await w.service._start_automatic(await w.service.store.get(r["request_id"]))
    assert (await w.service.store.get(r["request_id"]))["state"] == "succeeded"
    assert w.executor.await_args.kwargs["actor_id"] == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("action,staff_actor", [("ban_member", 2)])
async def test_ban_still_requires_current_staff_authority(environment, action, staff_actor):
    w = environment
    in_call(w, 3)
    r = await _request(w, action, target=3)
    i = _interaction(w, actor=1)
    await w.service.handle_interaction(i, r["request_id"], approve=True)
    assert (await w.service.store.get(r["request_id"]))["state"] == "pending"
    await w.service.handle_interaction(_interaction(w, actor=staff_actor), r["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 1 and w.executor.await_args.kwargs["actor_id"] == staff_actor

@pytest.mark.asyncio
async def test_revoked_ban_permission_after_click_stops_execution(environment):
    w = environment
    r = await _request(w)
    i = _interaction(w)
    await w.service.handle_interaction(i, r["request_id"], approve=True)
    assert not i.notices
    i.response.defer.assert_awaited_with(thinking=False)
    w.members[2].guild_permissions.ban_members = False
    await w.supervisor.drain()
    w.executor.assert_not_awaited()
    assert (await w.service.store.get(r["request_id"]))["state"] == "failed"
    assert i.notices and i.notices[0][1]["ephemeral"]

@pytest.mark.asyncio
async def test_disabled_category_after_planning_prevents_approval(environment):
    w = environment
    r = await _request(w)
    w.config.moderation_actions_enabled = False
    i = _interaction(w)
    await w.service.handle_interaction(i, r["request_id"], approve=True)
    assert (await w.service.store.get(r["request_id"]))["state"] == "pending"
    assert not w.supervisor.pending and "desativada" in i.notices[0][0]

@pytest.mark.asyncio
async def test_expired_button_has_no_execution_and_refreshes_expired_state(environment):
    w = environment
    r = await _request(w)
    w.clock.advance(300)
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    assert (await w.service.store.get(r["request_id"]))["state"] == "expired"
    w.card.delete.assert_awaited_once()
    w.executor.assert_not_awaited()

@pytest.mark.asyncio
async def test_concurrent_approvals_schedule_exactly_one_side_effect(environment):
    w = environment
    r = await _request(w)
    await asyncio.gather(*(w.service.handle_interaction(_interaction(w), r["request_id"], approve=True) for _ in range(8)))
    assert len(w.supervisor.pending) == 1
    await w.supervisor.drain()
    w.executor.assert_awaited_once()
    w.card.delete.assert_awaited_once()

@pytest.mark.asyncio
async def test_automatic_audio_binds_and_executes_without_approval(environment):
    w = environment
    p = await w.service.plan(w.message, ChatReply("fala", (ActionProposal("send_audio", text="fala"),)),
                             await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    assert (await w.service.store.get(p.requests[0]["request_id"]))["state"] == "succeeded"
    assert w.service.view(p) is None and not w.service.content(p)
    w.chat.send.assert_not_awaited()
    w.card.delete.assert_not_awaited()

@pytest.mark.asyncio
async def test_speech_enters_memory_only_after_confirmed_execution_with_original_epoch(environment):
    w = environment
    epoch = MemoryEpoch(global_generation=3, guild_generation=4, user_generation=5)
    w.bot.get_cog("Chatbot")._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=epoch))
    p = await w.service.plan(w.message, ChatReply("fala", (ActionProposal("send_audio", text="fala", ask_permission=True),)),
                             await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, visibility_scope="private:30")
    await w.service.bind_and_start(p, None)
    w.cog._persist_turn.assert_not_awaited()
    await w.supervisor.drain()
    assert w.cog._persist_turn.await_args.kwargs["assistant_message"] == "fala"
    assert w.cog._persist_turn.await_args.kwargs["epoch"] == epoch
    assert "text" not in (await w.service.store.get(p.requests[0]["request_id"]))["payload"]

@pytest.mark.asyncio
async def test_rejected_optional_audio_never_enters_conversation_memory(environment):
    w = environment
    r = await _request(w, "send_audio", ask=True)
    restarted = ActionService(w.cog, w.coll)
    restarted.store = ActionStore(w.coll, clock=w.clock)
    await restarted.initialize()
    await w.supervisor.drain()
    assert (await restarted.store.get(r["request_id"]))["state"] == "cancelled"
    w.executor.assert_not_awaited()
    w.cog._persist_turn.assert_not_awaited()
    w.card.delete.assert_awaited_once()

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ban_member"])
async def test_false_model_permission_never_automates_ban(environment, action):
    w = environment
    in_call(w, 3)
    p = await w.service.plan(w.message, ChatReply("", (ActionProposal(action, "m1", reason="spam", ask_permission=False),)),
                             await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    current = await w.service.store.get(p.requests[0]["request_id"])
    assert current["ask_permission"] and current["state"] == "pending"
    current["ask_permission"] = False
    await w.service._start_automatic(current)
    w.executor.assert_not_awaited()
    assert "Posso" in w.chat.send.await_args.args[0]

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "executor_uncertain"])
async def test_uncertain_execution_has_no_replay_and_purges_speech(environment, monkeypatch, failure):
    w = environment
    r = await _request(w)
    if failure == "timeout":
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        w.executor.side_effect = blocked
        monkeypatch.setattr(actions_module.C, "ACTION_EXECUTION_TIMEOUT_SECONDS", 0.01)
    else:
        w.executor.side_effect = ActionExecutionUncertain("Envio pode ter ocorrido.")
    i = _interaction(w)
    await w.service.handle_interaction(i, r["request_id"], approve=True)
    await w.supervisor.drain()
    final = await w.service.store.get(r["request_id"])
    assert final["state"] == "uncertain" and "text" not in final["payload"]
    assert i.notices and i.notices[-1][1]["ephemeral"]
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    await w.service._start_automatic(final)
    await w.supervisor.drain()
    w.executor.assert_awaited_once()

@pytest.mark.asyncio
async def test_startup_restores_optional_views_without_replaying_automatic_audio(environment):
    w = environment
    staff = await _request(w)
    auto = await _request(w, "send_audio", ask=False, bound=False)
    await w.service.store.arm_automatic(auto["request_id"])
    restarted = ActionService(w.cog, w.coll)
    restarted.store = ActionStore(w.coll, clock=w.clock)
    await restarted.initialize()
    assert w.bot.add_view.call_count == 1
    assert w.bot.add_view.call_args.kwargs["message_id"] == 60
    await w.supervisor.drain()
    assert (await restarted.store.get(auto["request_id"]))["state"] == "succeeded"
    assert (await restarted.store.get(staff["request_id"]))["state"] == "pending"

@pytest.mark.asyncio
async def test_recent_operation_context_has_actual_state_without_private_speech(environment):
    w = environment
    r = await _request(w)
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    await w.supervisor.drain()
    context = await w.service.describe(w.message, w.config)
    assert "ban_member: succeeded" in context.description
    assert "conteúdo privado" not in context.description

@pytest.mark.asyncio
async def test_safe_mode_returns_frozen_context_without_actions(environment, monkeypatch):
    monkeypatch.setattr(actions_module.C, "SAFE_MODE", True)
    context = await environment.service.describe(environment.message, environment.config)
    assert not context.actions and "apenas em texto" in context.description

@pytest.mark.asyncio
async def test_two_requests_for_same_member_cannot_execute_at_once(environment):
    w = environment
    one, two = await _request(w), await _request(w)
    await asyncio.gather(w.service.handle_interaction(_interaction(w), one["request_id"], approve=True),
                         w.service.handle_interaction(_interaction(w), two["request_id"], approve=True))
    assert sorted([(await w.service.store.get(r["request_id"]))["state"] for r in (one, two)]) == ["executing", "pending"]
    assert len(w.supervisor.pending) == 1
    await w.supervisor.drain()
    assert not w.service._active_users

@pytest.mark.asyncio
async def test_two_member_global_limit_keeps_third_pending_then_accepts_retry(environment, monkeypatch):
    from test_chatbot_action_policy import member
    w = environment
    monkeypatch.setattr(actions_module.C, "MAX_CONCURRENT_ACTIONS", 2)
    w.members[5] = member(w.guild, 5, rank=0)
    for mid in (1, 2, 3):
        w.members[mid].guild_permissions.ban_members = True
        w.members[mid].top_role = 10
    requests = [await _request(w, requester=mid, target=5) for mid in (1, 2, 3)]
    for mid, r in zip((1, 2), requests[:2]):
        await w.service.handle_interaction(_interaction(w, actor=mid), r["request_id"], approve=True)
    await w.service.handle_interaction(_interaction(w, actor=3), requests[2]["request_id"], approve=True)
    assert w.service._active_users == {(10, 1), (10, 2)}
    await w.supervisor.drain()
    await w.service.handle_interaction(_interaction(w, actor=3), requests[2]["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 3 and not w.service._active_users

@pytest.mark.asyncio
async def test_busy_automatic_audio_is_rejected_and_private_payload_purged(environment):
    w = environment
    active = await _request(w)
    audio = await _request(w, "send_audio", ask=False, bound=False)
    await w.service.store.arm_automatic(audio["request_id"])
    await w.service.handle_interaction(_interaction(w), active["request_id"], approve=True)
    await w.service._start_automatic(await w.service.store.get(audio["request_id"]))
    assert (await w.service.store.get(audio["request_id"]))["state"] == "pending"
    await w.supervisor.drain()
    assert w.executor.await_count == 2 and not w.service._active_users
    assert "text" not in (await w.service.store.get(audio["request_id"]))["payload"]

@pytest.mark.asyncio
async def test_exception_releases_admission_slot_and_purges_hidden_payload(environment):
    w = environment
    r = await _request(w)
    w.executor.side_effect = RuntimeError("Disconnected")
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    await w.supervisor.drain()
    assert (await w.service.store.get(r["request_id"]))["state"] == "uncertain"
    assert not w.service._active_users

@pytest.mark.asyncio
async def test_cancelled_execution_releases_slot_and_persists_uncertainty(environment):
    w = environment
    r = await _request(w)
    started = asyncio.Event()
    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    w.executor.side_effect = blocked
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=True)
    task = asyncio.create_task(w.supervisor.pending.pop())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await w.service.store.get(r["request_id"]))["state"] == "uncertain"
    assert not w.service._active_users

@pytest.mark.asyncio
async def test_busy_loser_does_not_release_real_winner_reservation(environment, monkeypatch):
    w = environment
    one, two = await _request(w), await _request(w)
    entered, release = asyncio.Event(), asyncio.Event()
    original = w.service.store.claim
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(w.service.store, "claim", delayed)
    winner = asyncio.create_task(w.service.handle_interaction(_interaction(w), one["request_id"], approve=True))
    await entered.wait()
    await w.service.handle_interaction(_interaction(w), two["request_id"], approve=True)
    assert w.service._active_users == {(10, 1)}
    release.set()
    await winner
    await w.supervisor.drain()
    assert w.executor.await_count == 1 and not w.service._active_users

@pytest.mark.asyncio
async def test_lost_database_claim_releases_local_slot_for_another_request(environment, monkeypatch):
    w = environment
    one, two = await _request(w), await _request(w)
    original = w.service.store.claim
    monkeypatch.setattr(w.service.store, "claim", AsyncMock(return_value=None))
    await w.service.handle_interaction(_interaction(w), one["request_id"], approve=True)
    assert not w.service._active_users and not w.supervisor.pending
    monkeypatch.setattr(w.service.store, "claim", original)
    await w.service.handle_interaction(_interaction(w), two["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 1

@pytest.mark.asyncio
async def test_join_then_speech_runs_automatically_only_after_confirmed_connection(environment):
    w = environment
    w.guild.voice_client = None
    w.members[999].voice = None
    in_call(w, 1)
    context = await build_action_context(w.bot, w.message, w.config)
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("join_voice", "autor"), ActionProposal("speak_voice", text="oi call"))), context, w.config)
    assert len(p.requests) == 2 and p.requests[1]["state"] == "blocked"
    async def execute(bot, request, *, actor_id):
        if request["action"] == "join_voice":
            in_call(w, 1, bot=True)
        return ExecutionResult("ok")
    w.executor.side_effect = execute
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    assert [c.kwargs["actor_id"] for c in w.executor.await_args_list] == [1, 1]
    assert [r["state"] for r in await w.service.store.list_for_plan(p.requests[0]["plan_id"])] == ["succeeded", "succeeded"]
    assert all(not r["ask_permission"] for r in p.requests)
    w.chat.send.assert_not_awaited()
    w.card.delete.assert_not_awaited()
    assert not w.service._active_users

@pytest.mark.asyncio
async def test_two_bans_need_two_separate_approvals(environment):
    from test_chatbot_action_policy import member
    w = environment
    w.members[5] = member(w.guild, 5, rank=1)
    w.message.mentions.append(w.members[5])
    p = await w.service.plan(w.message, ChatReply("certo", (
        ActionProposal("ban_member", "m1", reason="spam"), ActionProposal("ban_member", "m2", reason="flood"))),
        await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    assert w.chat.send.await_count == 1
    await w.service.handle_interaction(_interaction(w), p.requests[0]["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 1 and w.chat.send.await_count == 2
    assert (await w.service.store.get(p.requests[1]["request_id"]))["state"] == "pending"
    await w.service.handle_interaction(_interaction(w), p.requests[1]["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 2

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "uncertain", "rejected"])
async def test_unsuccessful_predecessor_cancels_speech_and_purges_private_text(environment, outcome):
    w = environment
    context = await build_action_context(w.bot, w.message, w.config)
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("ban_member", "m1", reason="spam"), ActionProposal("send_audio", text="segredo"))), context, w.config)
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    if outcome == "rejected":
        await w.service.handle_interaction(_interaction(w), p.requests[0]["request_id"], approve=False)
    else:
        w.executor.side_effect = ActionDenied("negado") if outcome == "failed" else ActionExecutionUncertain("incerto")
        await w.service.handle_interaction(_interaction(w), p.requests[0]["request_id"], approve=True)
    await w.supervisor.drain()
    tail = await w.service.store.get(p.requests[1]["request_id"])
    assert tail["state"] == "cancelled" and "text" not in tail["payload"]
    assert w.executor.await_count <= 1

@pytest.mark.asyncio
async def test_delete_failure_removes_controls_and_cleanup_retries(environment):
    w = environment
    r = await _request(w)
    w.card.delete.side_effect = RuntimeError("network")
    await w.service.handle_interaction(_interaction(w), r["request_id"], approve=False)
    w.card.edit.assert_awaited_with(view=None)
    assert not (await w.service.store.get(r["request_id"]))["card_removed"]
    w.card.delete.side_effect = None
    await w.service.cleanup()
    assert (await w.service.store.get(r["request_id"]))["card_removed"]

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["definite_failure", "uncertain"])
async def test_audio_falls_back_to_text_only_after_confirmed_failure(environment, outcome):
    w = environment
    r = await _request(w, "send_audio", ask=False, bound=False)
    await w.service.store.arm_automatic(r["request_id"])
    w.executor.side_effect = ActionDenied("tts indisponível") if outcome == "definite_failure" else ActionExecutionUncertain("incerto")
    await w.service._start_automatic(await w.service.store.get(r["request_id"]))
    if outcome == "definite_failure":
        assert w.chat.send.await_args.args[0] == "conteúdo privado"
    else:
        w.chat.send.assert_not_awaited()

@pytest.mark.asyncio
async def test_two_distinct_audio_steps_reject_the_whole_plan(environment):
    w = environment
    p = await w.service.plan(w.message, ChatReply("prévia", (
        ActionProposal("send_audio", text="um"), ActionProposal("speak_voice", text="dois"))),
        await build_action_context(w.bot, w.message, w.config), w.config)
    assert not p.requests and not w.coll.docs and "único" in p.public_error


@pytest.mark.asyncio
async def test_native_plan_preserves_real_original_user_text_and_provider_metadata(environment):
    w = environment
    w.message.content = "áudio da pergunta"
    reply = ChatReply("", (ActionProposal("send_audio", text="resposta"),), provider="groq", model="modelo")
    p = await w.service.plan(w.message, reply, await build_action_context(w.bot, w.message, w.config),
                             w.config, original_user_text="pergunta transcrita completa")
    assert p.requests[0]["original_user_text"] == "pergunta transcrita completa"
    assert p.requests[0]["provider"] == "groq" and p.requests[0]["model"] == "modelo"
    assert "pergunta transcrita" not in w.service.content(p)


@pytest.mark.asyncio
async def test_own_pending_reads_never_disclose_private_speech_and_cancel_stops_chain(environment):
    w = environment
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("ban_member", "m1", reason="spam"),
        ActionProposal("send_audio", text="fala privada"))),
        await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    own = await w.service.list_pending(guild_id=10, channel_id=30, requester_id=1)
    assert len(own) == 2
    assert "fala privada" not in str(own) and "payload" not in str(own) and "execution_token" not in str(own)
    assert await w.service.list_pending(guild_id=10, channel_id=30, requester_id=2) == []
    assert not await w.service.cancel_pending(p.requests[0]["request_id"], guild_id=10, channel_id=30, requester_id=2)
    w.config.actions_enabled = False  # cancelling old work still available
    assert await w.service.cancel_pending(p.requests[0]["request_id"], guild_id=10, channel_id=30, requester_id=1)
    for request in p.requests:
        current = await w.service.store.get(request["request_id"])
        assert current["state"] == "cancelled" and "text" not in current["payload"]
    w.executor.assert_not_awaited()
    w.card.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_own_pending_access_checked_fresh_even_for_cancel(environment):
    w = environment
    r = await _request(w)
    w.chat.permissions_for.side_effect = lambda m: SimpleNamespace(view_channel=m.id != 1)
    for operation in (
        w.service.list_pending(guild_id=10, channel_id=30, requester_id=1),
        w.service.cancel_pending(r["request_id"], guild_id=10, channel_id=30, requester_id=1),
    ):
        with pytest.raises(ActionDenied):
            await operation
    assert (await w.service.store.get(r["request_id"]))["state"] == "pending"


@pytest.mark.asyncio
async def test_expanded_privileged_sequence_gets_separate_staff_approvals(environment):
    from test_chatbot_action_policy import expanded
    w = expanded.__wrapped__(environment)
    w.message.mentions.append(w.members[4])
    context = await build_action_context(w.bot, w.message, w.config)
    p = await w.service.plan(w.message, ChatReply("vou pedir", (
        ActionProposal("timeout_member", "m1", reason="spam", options={"duration_seconds": 30}),
        ActionProposal("kick_member", "m2", reason="abuso"))), context, w.config)
    assert len(p.requests) == 2
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    first, second = p.requests
    assert (await w.service.store.get(first["request_id"]))["state"] == "pending"
    assert (await w.service.store.get(second["request_id"]))["state"] == "blocked"
    await w.service.handle_interaction(_interaction(w), first["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 1
    assert (await w.service.store.get(second["request_id"]))["state"] == "pending"
    assert not (await w.service.store.get(second["request_id"])).get("approved_by")
    await w.service.handle_interaction(_interaction(w), second["request_id"], approve=True)
    await w.supervisor.drain()
    assert w.executor.await_count == 2 and not w.service._active_users
    assert (await w.service.store.get(second["request_id"]))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_invalid_expanded_predecessor_never_persists_or_executes_following_ban(environment):
    from test_chatbot_action_policy import expanded
    w = expanded.__wrapped__(environment)
    context = await build_action_context(w.bot, w.message, w.config)
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("edit_channel", reason="pedido", options={"channel_ref": "inventado", "channel_changes": {"name": "novo"}}),
        ActionProposal("ban_member", "m1", reason="spam"))), context, w.config)
    assert not p.requests and not w.coll.docs and p.public_error
    w.executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_fails", [False, True])
async def test_db_failure_after_card_send_removes_or_disables_known_orphan(environment, monkeypatch, delete_fails):
    w = environment
    if delete_fails:
        w.card.delete.side_effect = RuntimeError("Discord indisponível")
    monkeypatch.setattr(w.service.store, "bind", AsyncMock(side_effect=RuntimeError("banco indisponível")))
    plan = await w.service.plan(w.message, ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),)),
                                await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(plan)
    await w.supervisor.drain()
    current = await w.service.store.get(plan.requests[0]["request_id"])
    assert current["state"] == "uncertain"
    w.card.delete.assert_awaited_once()
    if delete_fails:
        w.card.edit.assert_awaited_once_with(view=None)
    assert not w.service._views
    w.executor.assert_not_awaited()


def _navigation_context(world, action):
    if action == "join_voice":
        world.guild.voice_client = None
        world.members[999].voice = None
        in_call(world, 1)
    else:
        from test_chatbot_action_policy import _expanded_voice
        _expanded_voice(world)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "move_voice", "leave_voice"])
async def test_common_member_navigation_runs_without_cards_despite_legacy_model_approval_flag(environment, action):
    w = environment
    _navigation_context(w, action)
    p = await w.service.plan(w.message, ChatReply("certo", (
        ActionProposal(action, "m1" if action == "move_voice" else "autor", ask_permission=True),)),
        await build_action_context(w.bot, w.message, w.config), w.config)
    assert len(p.requests) == 1 and not p.requests[0]["ask_permission"]
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    final = await w.service.store.get(p.requests[0]["request_id"])
    assert final["state"] == "succeeded" and final["message_id"] == 0
    assert w.executor.await_args.kwargs["actor_id"] == w.message.author.id
    w.chat.send.assert_not_awaited()
    w.card.delete.assert_not_awaited()
    w.bot.add_view.assert_not_called()


@pytest.mark.asyncio
async def test_move_then_speech_waits_for_the_confirmed_new_call(environment):
    from test_chatbot_action_policy import _expanded_voice
    w = environment
    destination = _expanded_voice(w)
    w.members[1].voice = SimpleNamespace(channel=destination)
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("move_voice", "autor"), ActionProposal("speak_voice", text="fala depois da mudança"))),
        await build_action_context(w.bot, w.message, w.config), w.config)
    assert len(p.requests) == 2 and p.requests[1]["state"] == "blocked"

    async def execute(bot, request, *, actor_id):
        if request["action"] == "move_voice":
            w.guild.voice_client = SimpleNamespace(channel=destination)
            w.members[999].voice = SimpleNamespace(channel=destination)
        return ExecutionResult("ok")

    w.executor.side_effect = execute
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    assert [call.args[1]["action"] for call in w.executor.await_args_list] == ["move_voice", "speak_voice"]
    assert [call.kwargs["actor_id"] for call in w.executor.await_args_list] == [1, 1]
    assert all(r["state"] == "succeeded" for r in await w.service.store.list_for_plan(p.requests[0]["plan_id"]))
    w.chat.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["denied", "uncertain", "unknown"])
async def test_navigation_failure_cancels_following_audio_and_reports_one_safe_message(environment, failure):
    w = environment
    _navigation_context(w, "join_voice")
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("join_voice", "autor"), ActionProposal("speak_voice", text="SEGREDO DA FALA"))),
        await build_action_context(w.bot, w.message, w.config), w.config)
    w.executor.side_effect = (ActionDenied("A call está ocupada.") if failure == "denied" else
                             ActionExecutionUncertain("SEGREDO DO ADAPTER") if failure == "uncertain" else
                             RuntimeError("SEGREDO DA EXCEÇÃO"))
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    final = await w.service.store.list_for_plan(p.requests[0]["plan_id"])
    assert [r["state"] for r in final] == ["failed" if failure == "denied" else "uncertain", "cancelled"]
    assert "text" not in final[1]["payload"]
    w.executor.assert_awaited_once()
    assert w.chat.send.await_count == 1
    text = w.chat.send.await_args.args[0]
    assert text == ("A call está ocupada." if failure == "denied" else
                    "Não consegui confirmar a entrada. Confira a call antes de tentar de novo.")
    assert "SEGREDO" not in text
    await w.service.cleanup()
    await w.supervisor.drain()
    assert w.executor.await_count == 1 and w.chat.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "move_voice", "leave_voice"])
async def test_startup_cancels_pending_legacy_navigation_without_connecting(environment, action):
    w = environment
    old = await _request(w, action, ask=True)
    restarted = ActionService(w.cog, w.coll)
    restarted.store = ActionStore(w.coll, clock=w.clock)
    await restarted.initialize()
    await w.supervisor.drain()
    assert (await restarted.store.get(old["request_id"]))["state"] == "cancelled"
    w.executor.assert_not_awaited()
    w.card.delete.assert_awaited_once()
    w.bot.add_view.assert_not_called()


@pytest.mark.asyncio
async def test_startup_recovers_armed_navigation_once_and_keeps_executing_or_uncertain_unreplayed(environment):
    w = environment
    _navigation_context(w, "join_voice")
    pending = await _request(w, "join_voice", ask=False, bound=False)
    executing = await _request(w, "join_voice", ask=False, bound=False)
    uncertain = await _request(w, "join_voice", ask=False, bound=False)
    for request in (pending, executing, uncertain):
        assert await w.service.store.arm_automatic(request["request_id"])
    for request in (executing, uncertain):
        claimed = await w.service.store.claim_automatic(request["request_id"], guild_id=10, channel_id=30, actor_id=1)
        if request is uncertain:
            assert await w.service.store.finish(request["request_id"], claimed["execution_token"],
                                                state="uncertain", public_result="Verifique a call.")
    restarted = ActionService(w.cog, w.coll)
    restarted.store = ActionStore(w.coll, clock=w.clock)
    await restarted.initialize()
    await w.supervisor.drain()
    w.executor.assert_awaited_once()
    assert (await restarted.store.get(pending["request_id"]))["state"] == "succeeded"
    assert (await restarted.store.get(executing["request_id"]))["state"] == "executing"
    assert (await restarted.store.get(uncertain["request_id"]))["state"] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "send_audio"])
async def test_automatic_effect_uses_shared_processing_context_and_releases_it(environment, action):
    w = environment
    events = []

    @asynccontextmanager
    async def processing(channel):
        events.append(("enter", channel.id))
        try:
            yield
        finally:
            events.append(("exit", channel.id))

    w.cog.processing = processing
    if action == "join_voice":
        _navigation_context(w, action)
    p = await w.service.plan(w.message, ChatReply("", (ActionProposal(action, "autor", text="oi" if action == "send_audio" else ""),)),
        await build_action_context(w.bot, w.message, w.config), w.config)

    async def execute(*args, **kwargs):
        assert events == [("enter", 30)]
        return ExecutionResult("ok")

    w.executor.side_effect = execute
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    assert events == [("enter", 30), ("exit", 30)]


@pytest.mark.asyncio
async def test_staff_card_waiting_never_starts_processing_indicator(environment):
    w = environment
    w.cog.processing = Mock(side_effect=AssertionError("Pending staff card must not start typing"))
    p = await w.service.plan(w.message, ChatReply("vou pedir", (ActionProposal("ban_member", "m1", reason="spam"),)),
        await build_action_context(w.bot, w.message, w.config), w.config)
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    assert (await w.service.store.get(p.requests[0]["request_id"]))["state"] == "pending"
    w.cog.processing.assert_not_called()


async def _draft_environment(world, *, action="ban_member", target_id=3, options=None):
    from cogs.chatbot.action_drafts import ActionDraftStore
    from test_chatbot_action_drafts import Collection
    epoch = MemoryEpoch(global_generation=1, guild_generation=2, user_generation=3)
    memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=epoch))
    world.bot.get_cog("Chatbot")._memory = memory
    world.draft_coll = Collection()
    world.cog._action_drafts = ActionDraftStore(world.draft_coll, memory=memory, clock=lambda: 1000.0)
    snapshot = await world.cog._action_drafts.save(10, 30, 1, epoch, action=action, target_id=target_id, options=options or {})
    return epoch, snapshot


@pytest.mark.asyncio
async def test_matching_draft_is_consumed_before_any_plan_insert_and_audits_same_uuid(environment, monkeypatch):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    consume = w.cog._action_drafts.consume

    async def consume_before_insert(*args, **kwargs):
        assert w.coll.docs == []
        assert await w.service.store.recover_ready() == []
        return await consume(*args, **kwargs)

    monkeypatch.setattr(w.cog._action_drafts, "consume", AsyncMock(side_effect=consume_before_insert))
    p = await w.service.plan(w.message, ChatReply("Vou solicitar o banimento; texto redundante.", (
        ActionProposal("ban_member", "m1", reason="motivo completado pelo usuário"),)),
        await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, action_draft=snapshot)
    assert len(p.requests) == 1 and p.base_reply == ""
    request = p.requests[0]
    args = w.cog._action_drafts.consume.await_args
    assert args.args == (10, 30, 1, epoch)
    assert args.kwargs["expected_draft_id"] == snapshot["draft_id"]
    assert args.kwargs["expected_revision"] == snapshot["revision"]
    assert request["plan_id"] == args.kwargs["plan_id"] == str(UUID(args.kwargs["plan_id"]))
    assert request["consumed_draft_id"] == snapshot["draft_id"]
    assert request["consumed_draft_revision"] == snapshot["revision"]
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) is None
    await w.service.bind_and_start(p)
    await w.supervisor.drain()
    assert w.chat.send.await_count == 1
    assert w.chat.send.await_args.args[0].startswith("Posso banir")
    w.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_draft_concurrently_produces_one_plan_one_card_and_one_approved_effect(environment):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    reply = ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),))
    context = await build_action_context(w.bot, w.message, w.config)
    plans = await asyncio.gather(*(w.service.plan(w.message, reply, context, w.config, epoch=epoch,
                                                action_draft=dict(snapshot)) for _ in range(2)))
    winners = [p for p in plans if p.requests]
    assert len(winners) == 1 and len(w.coll.docs) == 1
    assert next(p for p in plans if not p.requests).public_error
    await w.service.bind_and_start(winners[0])
    await w.supervisor.drain()
    assert w.chat.send.await_count == 1
    await w.service.handle_interaction(_interaction(w), winners[0].requests[0]["request_id"], approve=True)
    await w.supervisor.drain()
    w.executor.assert_awaited_once()
    later = await w.service.plan(w.message, reply, context, w.config, epoch=epoch, action_draft=snapshot)
    assert not later.requests and later.public_error
    assert len(w.coll.docs) == 1


@pytest.mark.asyncio
async def test_changed_draft_revision_refuses_plan_without_destroying_the_new_draft(environment):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    updated = await w.cog._action_drafts.save(10, 30, 1, epoch, reason="novo motivo",
        expected_draft_id=snapshot["draft_id"], expected_revision=snapshot["revision"])
    p = await w.service.plan(w.message, ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),)),
        await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, action_draft=snapshot)
    assert not p.requests and p.public_error and w.coll.docs == []
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) == updated
    w.executor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["target", "action"])
async def test_unrelated_proposal_leaves_current_draft_untouched(environment, difference):
    w = environment
    epoch, snapshot = await _draft_environment(w, action="kick_member" if difference == "action" else "ban_member",
                                               target_id=4 if difference == "target" else 3)
    p = await w.service.plan(w.message, ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),)),
        await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, action_draft=snapshot)
    assert len(p.requests) == 1 and "consumed_draft_id" not in p.requests[0]
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) == snapshot


@pytest.mark.asyncio
async def test_audio_draft_with_implicit_author_is_consumed_without_copying_private_draft_fields_to_audit(environment):
    w = environment
    epoch, snapshot = await _draft_environment(w, action="send_audio", target_id=None)
    p = await w.service.plan(w.message, ChatReply("PRIVATE PREVIEW", (
        ActionProposal("send_audio", text="fala privada concluída"),)),
        await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, action_draft=snapshot)
    assert len(p.requests) == 1 and p.base_reply == ""
    assert p.requests[0]["consumed_draft_id"] == snapshot["draft_id"]
    assert p.requests[0]["consumed_draft_revision"] == 1
    assert "action_draft" not in p.requests[0] and "missing_fields" not in p.requests[0]
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) is None


@pytest.mark.asyncio
async def test_consumption_unknown_outcome_creates_no_plan_and_never_restores_consumed_draft(environment, monkeypatch):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    consume = w.cog._action_drafts.consume

    async def uncertain_consume(*args, **kwargs):
        await consume(*args, **kwargs)
        raise RuntimeError("PRIVATE BACKEND DETAIL")

    monkeypatch.setattr(w.cog._action_drafts, "consume", AsyncMock(side_effect=uncertain_consume))
    p = await w.service.plan(w.message, ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),)),
        await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, action_draft=snapshot)
    assert not p.requests and p.public_error and w.coll.docs == []
    assert "PRIVATE" not in p.public_error
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) is None
    w.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_persistence_failure_after_consumption_never_restores_or_reuses_draft(environment, monkeypatch):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    create = w.service.store.create_plan
    failed_create = AsyncMock(side_effect=RuntimeError("PRIVATE write failed"))
    monkeypatch.setattr(w.service.store, "create_plan", failed_create)
    reply = ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),))
    context = await build_action_context(w.bot, w.message, w.config)
    failed = await w.service.plan(w.message, reply, context, w.config, epoch=epoch, action_draft=snapshot)
    assert not failed.requests and failed.public_error and "PRIVATE" not in failed.public_error
    failed_create.assert_awaited_once()
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) is None
    monkeypatch.setattr(w.service.store, "create_plan", create)
    repeat = await w.service.plan(w.message, reply, context, w.config, epoch=epoch, action_draft=snapshot)
    assert not repeat.requests and repeat.public_error and w.coll.docs == []


@pytest.mark.asyncio
async def test_uncertain_plan_write_is_recovered_once_and_consumed_snapshot_cannot_recreate_it(environment, monkeypatch):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    create = w.service.store.create_plan

    async def persisted_then_failed(*args, **kwargs):
        await create(*args, **kwargs)
        raise RuntimeError("PRIVATE write acknowledgement lost")

    failed_create = AsyncMock(side_effect=persisted_then_failed)
    monkeypatch.setattr(w.service.store, "create_plan", failed_create)
    reply = ChatReply("", (ActionProposal("ban_member", "m1", reason="spam"),))
    context = await build_action_context(w.bot, w.message, w.config)
    failed = await w.service.plan(w.message, reply, context, w.config, epoch=epoch, action_draft=snapshot)
    assert not failed.requests and failed.public_error and "PRIVATE" not in failed.public_error
    assert len(w.coll.docs) == 1 and not w.supervisor.pending
    repeated = await w.service.plan(w.message, reply, context, w.config, epoch=epoch, action_draft=snapshot)
    assert not repeated.requests and repeated.public_error
    failed_create.assert_awaited_once()
    await w.service.initialize()
    await w.supervisor.drain()
    assert w.chat.send.await_count == 1
    await w.service.handle_interaction(_interaction(w), w.coll.docs[0]["request_id"], approve=True)
    await w.supervisor.drain()
    w.executor.assert_awaited_once()
    await w.service.initialize()
    await w.supervisor.drain()
    assert w.chat.send.await_count == 1
    w.executor.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["assign_role", "edit_channel", "purge_messages"])
@pytest.mark.parametrize("matching_resource", [False, True])
async def test_draft_consumption_matches_host_resources_not_only_action_and_member(environment, action, matching_resource):
    w = policy_expanded.__wrapped__(environment)
    from datetime import datetime, timezone

    second_role = MagicMock(spec=discord.Role)
    second_role.id, second_role.guild, second_role.managed, second_role.position = 71, w.guild, False, 3
    second_role.is_default.return_value = False
    second_role.permissions = discord.Permissions.none()
    second_role.__lt__.side_effect = lambda rank: 3 < rank
    role_lookup, channel_lookup = w.guild.get_role, w.guild.get_channel
    w.guild.get_role = lambda identifier: second_role if identifier == 71 else role_lookup(identifier)
    second_channel = MagicMock(spec=discord.TextChannel)
    second_channel.id, second_channel.guild = 31, w.guild
    second_channel.name, second_channel.topic, second_channel.slowmode_delay = "outro", "antes", 0
    second_channel.permissions_for.side_effect = w.chat.permissions_for.side_effect
    w.guild.get_channel = lambda identifier: second_channel if identifier == 31 else channel_lookup(identifier)
    w.config.action_allowed_role_ids = (70, 71)
    w.config.action_allowed_channel_ids = (30, 31)
    second_message = MagicMock(spec=discord.Message)
    second_message.id, second_message.guild, second_message.channel = 101, w.guild, w.chat
    second_message.created_at = datetime.now(timezone.utc)
    w.resources.update(r2=second_role, c2=second_channel, msg2=second_message)
    ref_key, canonical, first, second = {
        "assign_role": ("role_ref", "70", "r1", "r2"),
        "edit_channel": ("channel_ref", "30", "c1", "c2"),
        "purge_messages": ("message_refs", ["100"], ["msg1"], ["msg2"]),
    }[action]
    epoch, snapshot = await _draft_environment(w, action=action, target_id=3 if action == "assign_role" else None,
                                               options={ref_key: canonical})
    restored = {**snapshot, "options": {ref_key: first}}
    proposed_options = {ref_key: first if matching_resource else second}
    if action == "edit_channel":
        proposed_options["channel_changes"] = {"topic": "proposta completada"}
    context = SimpleNamespace(actions=(action,), targets=w.targets, resources=w.resources)
    plan = await w.service.plan(w.message, ChatReply("", (ActionProposal(action, "m1", reason="motivo", options=proposed_options),)),
                                context, w.config, epoch=epoch, action_draft=restored)
    assert len(plan.requests) == 1 and not plan.public_error
    assert ("consumed_draft_id" in plan.requests[0]) is matching_resource
    current = await w.cog._action_drafts.get_current(10, 30, 1, epoch)
    assert current == (None if matching_resource else snapshot)
    w.executor.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_resource_id_without_host_alias_cannot_consume_role_draft(environment):
    w = policy_expanded.__wrapped__(environment)
    epoch, snapshot = await _draft_environment(w, action="assign_role", options={"role_ref": "70"})
    plan = await w.service.plan(w.message, ChatReply("", (ActionProposal("assign_role", "m1", reason="motivo", options={"role_ref": "r1"}),)),
        SimpleNamespace(actions=("assign_role",), targets=w.targets, resources=w.resources), w.config,
        epoch=epoch, action_draft=snapshot)
    assert len(plan.requests) == 1 and "consumed_draft_id" not in plan.requests[0]
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) == snapshot


@pytest.mark.asyncio
async def test_invalid_batch_preparation_never_consumes_valid_matching_draft(environment, monkeypatch):
    w = environment
    epoch, snapshot = await _draft_environment(w)
    consume = AsyncMock(wraps=w.cog._action_drafts.consume)
    monkeypatch.setattr(w.cog._action_drafts, "consume", consume)
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("ban_member", "m1", reason="spam"), ActionProposal("send_audio", text=" "))),
        await build_action_context(w.bot, w.message, w.config), w.config, epoch=epoch, action_draft=snapshot)
    assert not p.requests and p.public_error and w.coll.docs == []
    consume.assert_not_awaited()
    assert await w.cog._action_drafts.get_current(10, 30, 1, epoch) == snapshot


@pytest.mark.asyncio
async def test_operational_plans_suppress_redundant_messages_but_plain_conversation_remains(environment):
    w = environment
    plain = await w.service.plan(w.message, ChatReply("conversa normal"),
                                await build_action_context(w.bot, w.message, w.config), w.config)
    assert plain.base_reply == "conversa normal"
    _navigation_context(w, "join_voice")
    operational = await w.service.plan(w.message, ChatReply("Já entrei!", (ActionProposal("join_voice", "autor"),)),
                                      await build_action_context(w.bot, w.message, w.config), w.config)
    assert len(operational.requests) == 1 and operational.base_reply == ""
