"""Fluxo integrado de pedidos, autorização atual e execução única."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.chatbot import actions as actions_module
from cogs.chatbot.action_execution import ActionExecutionUncertain, ExecutionResult
from cogs.chatbot.action_policy import ActionDenied, build_action_context
from cogs.chatbot.action_protocol import ActionProposal, ChatReply
from cogs.chatbot.action_store import ActionStore
from cogs.chatbot.actions import ActionService
from cogs.chatbot.memory import MemoryEpoch
from test_chatbot_action_policy import doc, in_call, world as policy_world
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
@pytest.mark.parametrize("action,staff_actor", [("join_voice", 4), ("ban_member", 2)])
async def test_join_and_ban_require_current_staff_authority(environment, action, staff_actor):
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
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_false_model_permission_never_automates_join_or_ban(environment, action):
    w = environment
    in_call(w, 3)
    if action == "join_voice":
        w.guild.voice_client = None
        w.members[999].voice = None
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
async def test_join_then_speech_waits_for_staff_and_executes_as_requester(environment):
    w = environment
    w.guild.voice_client = None
    w.members[999].voice = None
    in_call(w, 1)
    context = await build_action_context(w.bot, w.message, w.config)
    p = await w.service.plan(w.message, ChatReply("", (
        ActionProposal("join_voice", "autor"), ActionProposal("speak_voice", text="oi call"))), context, w.config)
    assert len(p.requests) == 2 and p.requests[1]["state"] == "blocked"
    await w.service.bind_and_start(p, None)
    await w.supervisor.drain()
    w.executor.assert_not_awaited()
    async def execute(bot, request, *, actor_id):
        if request["action"] == "join_voice":
            in_call(w, 1, bot=True)
        return ExecutionResult("ok")
    w.executor.side_effect = execute
    await w.service.handle_interaction(_interaction(w, actor=4), p.requests[0]["request_id"], approve=True)
    await w.supervisor.drain()
    assert [c.kwargs["actor_id"] for c in w.executor.await_args_list] == [4, 1]
    assert [r["state"] for r in await w.service.store.list_for_plan(p.requests[0]["plan_id"])] == ["succeeded", "succeeded"]
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
