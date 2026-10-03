"""Fluxo integrado de pedidos, autorização atual e execução única."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.chatbot import actions as actions_module
from cogs.chatbot.action_execution import ActionExecutionUncertain, ExecutionResult
from cogs.chatbot.action_policy import build_action_context
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


def _interaction(world, actor=1, *, message_id=60, guild_id=10, channel_id=30):
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
    world.card = SimpleNamespace(id=60, author=world.bot.user, edit=AsyncMock())
    world.chat.fetch_message = AsyncMock(return_value=world.card)
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


async def _request(world, action="send_audio", *, ask=True, target=1, bound=True, requester=1):
    data = doc(action, target=target, voice=20, ask=ask)
    data["requester_id"] = requester
    data["base_reply"] = ""
    created = await world.service.store.create(data)
    if bound:
        assert await world.service.store.bind(created["request_id"], 60)
    return await world.service.store.get(created["request_id"])


@pytest.mark.asyncio
async def test_optional_audio_in_mixed_plan_hides_every_persisted_base_reply(environment):
    world = environment
    context = await build_action_context(world.bot, world.message, world.config)
    private = "Esta fala precisa permanecer completamente privada."
    # Até o motivo da segunda ação pode virar uma prévia indevida da fala.
    reply = ChatReply(private, (
        ActionProposal("ban_member", "m1", reason=private),
        ActionProposal("send_audio", text=private, ask_permission=True),
    ))
    plan = await world.service.plan(world.message, reply, context, world.config)
    assert len(plan.requests) == 1
    assert plan.requests[0]["action"] == "send_audio"
    assert plan.base_reply == ""
    assert all(request["base_reply"] == "" for request in world.coll.docs)
    assert all(request["action"] != "ban_member" for request in world.coll.docs)
    content = world.service.content(plan)
    assert private not in content and "Banimento" not in content and "áudio" in content.lower()
    assert len(world.service.view(plan).children) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["unavailable_call", "invalid_speech"])
async def test_optional_audio_tries_second_valid_proposal_without_revealing_either_speech(environment, first_failure):
    world = environment
    first_secret = "Primeira fala que não deve ser mostrada."
    second_secret = "Segunda fala reservada até a aprovação."
    if first_failure == "unavailable_call":
        world.members[1].voice = None
        first = ActionProposal("speak_voice", "m1", text=first_secret, ask_permission=True)
    else:
        # Falha de preparação, mesmo com o tipo de ação disponível.
        first = ActionProposal("send_audio", text=" " * 5, ask_permission=True)
    context = await build_action_context(world.bot, world.message, world.config)
    reply = ChatReply(first_secret + second_secret, (
        first, ActionProposal("send_audio", "usuario", text=second_secret, ask_permission=True),
    ))
    plan = await world.service.plan(world.message, reply, context, world.config)
    assert len(plan.requests) == len(world.coll.docs) == 1
    request = plan.requests[0]
    assert request["action"] == "send_audio"
    assert request["payload"]["text"] == second_secret
    assert request["payload"]["target_id"] == world.message.author.id
    assert request["ask_permission"] and request["base_reply"] == plan.base_reply == ""
    assert plan.public_error == ""
    assert first_secret not in world.service.content(plan)
    assert second_secret not in world.service.content(plan)
    assert len(world.service.view(plan).children) == 2
    assert world.executor.await_count == 0 and not world.supervisor.pending


@pytest.mark.asyncio
async def test_preparation_error_reports_host_reason_without_private_tool_arguments(environment):
    world = environment
    context = await build_action_context(world.bot, world.message, world.config)
    secret = "SEGREDO QUE NÃO PODE VIRAR DIAGNÓSTICO " * 30
    plan = await world.service.plan(
        world.message, ChatReply(secret, (ActionProposal("send_audio", text=secret, ask_permission=True),)),
        context, world.config,
    )
    assert not plan.requests and not world.coll.docs
    assert plan.base_reply == ""
    assert plan.public_error == "Não consegui preparar essa fala. Peça uma resposta mais curta."
    assert secret not in plan.public_error
    assert world.executor.await_count == 0 and not world.supervisor.pending


@pytest.mark.asyncio
async def test_hidden_speech_remains_hidden_when_card_refreshes_after_rejection(environment):
    world = environment
    context = await build_action_context(world.bot, world.message, world.config)
    private = "Segredo que não deve aparecer no pedido."
    plan = await world.service.plan(
        world.message, ChatReply(private, (ActionProposal("send_audio", text=private, ask_permission=True),)),
        context, world.config,
    )
    await world.service.bind_and_start(plan, world.card)
    interaction = _interaction(world)
    await world.service.handle_interaction(interaction, plan.requests[0]["request_id"], approve=False)
    assert (await world.service.store.get(plan.requests[0]["request_id"]))["state"] == "rejected"
    assert private not in str(world.card.edit.call_args_list)
    assert private not in str(interaction.notices)
    assert world.executor.await_count == 0 and not world.supervisor.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [dict(message_id=999), dict(channel_id=999), dict(guild_id=999)])
async def test_button_copied_to_other_message_or_scope_cannot_authorize(environment, changes):
    world = environment
    request = await _request(world)
    interaction = _interaction(world, **changes)
    await world.service.handle_interaction(interaction, request["request_id"], approve=True)
    assert (await world.service.store.get(request["request_id"]))["state"] == "pending"
    assert not world.supervisor.pending and world.executor.await_count == 0
    assert "não pertence" in interaction.notices[0][0]
    assert interaction.notices[0][1]["ephemeral"]


@pytest.mark.asyncio
async def test_unbound_or_already_consumed_request_cannot_execute(environment):
    world = environment
    request = await _request(world, bound=False)
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    assert not world.supervisor.pending
    assert await world.service.store.bind(request["request_id"], 60)
    assert await world.service.store.reject(request["request_id"], guild_id=10, channel_id=30,
                                           message_id=60, actor_id=1)
    interaction = _interaction(world)
    await world.service.handle_interaction(interaction, request["request_id"], approve=True)
    assert not world.supervisor.pending and world.executor.await_count == 0
    assert "já foi encerrado" in interaction.notices[0][0]


@pytest.mark.asyncio
async def test_optional_audio_accepts_common_requester_and_rejects_other_staff(environment):
    world = environment
    request = await _request(world)
    unauthorized = _interaction(world, actor=2)
    await world.service.handle_interaction(unauthorized, request["request_id"], approve=True)
    assert (await world.service.store.get(request["request_id"]))["state"] == "pending"
    assert "Somente o membro" in unauthorized.notices[0][0]
    requester = _interaction(world)
    await world.service.handle_interaction(requester, request["request_id"], approve=True)
    await world.supervisor.drain()
    assert world.executor.await_count == 1
    assert world.executor.call_args.kwargs["actor_id"] == 1
    assert (await world.service.store.get(request["request_id"]))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_common_requester_can_approve_speech_in_current_call(environment):
    world = environment
    request = await _request(world, "speak_voice")
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    await world.supervisor.drain()
    assert world.executor.await_count == 1
    assert (await world.service.store.get(request["request_id"]))["state"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("action,staff_actor", [("join_voice", 4), ("ban_member", 2)])
async def test_join_and_ban_require_current_staff_authority(environment, action, staff_actor):
    world = environment
    in_call(world, 3)
    request = await _request(world, action, target=3)
    ordinary = _interaction(world)
    await world.service.handle_interaction(ordinary, request["request_id"], approve=True)
    assert (await world.service.store.get(request["request_id"]))["state"] == "pending"
    assert not world.supervisor.pending
    await world.service.handle_interaction(_interaction(world, actor=staff_actor), request["request_id"], approve=True)
    await world.supervisor.drain()
    assert world.executor.await_count == 1
    assert world.executor.call_args.kwargs["actor_id"] == staff_actor


@pytest.mark.asyncio
async def test_revoked_ban_permission_after_click_stops_execution(environment):
    world = environment
    request = await _request(world, "ban_member", target=3)
    await world.service.handle_interaction(_interaction(world, actor=2), request["request_id"], approve=True)
    assert (await world.service.store.get(request["request_id"]))["state"] == "executing"
    world.members[2].guild_permissions.ban_members = False
    await world.supervisor.drain()
    assert world.executor.await_count == 0
    assert (await world.service.store.get(request["request_id"]))["state"] == "failed"


@pytest.mark.asyncio
async def test_disabled_category_after_planning_prevents_approval(environment):
    world = environment
    request = await _request(world)
    world.config.audio_actions_enabled = False
    interaction = _interaction(world)
    await world.service.handle_interaction(interaction, request["request_id"], approve=True)
    assert (await world.service.store.get(request["request_id"]))["state"] == "pending"
    assert not world.supervisor.pending
    assert "desativada" in interaction.notices[0][0]


@pytest.mark.asyncio
async def test_expired_button_has_no_execution_and_refreshes_expired_state(environment):
    world = environment
    request = await _request(world)
    world.clock.advance(300)
    interaction = _interaction(world)
    await world.service.handle_interaction(interaction, request["request_id"], approve=True)
    assert (await world.service.store.get(request["request_id"]))["state"] == "expired"
    assert not world.supervisor.pending and world.executor.await_count == 0
    assert "expirou" in interaction.notices[0][0]


@pytest.mark.asyncio
async def test_concurrent_approvals_schedule_exactly_one_side_effect(environment):
    world = environment
    request = await _request(world)
    await asyncio.gather(*(
        world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
        for _ in range(8)
    ))
    assert len(world.supervisor.pending) == 1
    await world.supervisor.drain()
    assert world.executor.await_count == 1
    world.cog._remember_sent_message.assert_awaited_once_with(guild_id=10, channel_id=30, message_id=88)


@pytest.mark.asyncio
async def test_automatic_audio_binds_and_executes_without_approval(environment):
    world = environment
    context = await build_action_context(world.bot, world.message, world.config)
    plan = await world.service.plan(
        world.message, ChatReply("", (ActionProposal("send_audio", text="Fala automática."),)),
        context, world.config,
    )
    assert world.service.view(plan) is None
    assert world.executor.await_count == 0
    await world.service.bind_and_start(plan, world.card)
    await world.supervisor.drain()
    assert world.executor.await_count == 1
    assert (await world.service.store.get(plan.requests[0]["request_id"]))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_speech_enters_memory_only_after_confirmed_execution_with_original_epoch(environment):
    world = environment
    context = await build_action_context(world.bot, world.message, world.config)
    epoch = MemoryEpoch(global_generation=3, guild_generation=4, user_generation=5)
    spoken = "Resposta em áudio preservada depois da aprovação."
    plan = await world.service.plan(
        world.message, ChatReply(spoken, (ActionProposal("send_audio", text=spoken, ask_permission=True),)),
        context, world.config, epoch=epoch, visibility_scope="private:30",
    )
    await world.service.bind_and_start(plan, world.card)
    assert world.cog._persist_turn.await_count == 0
    await world.service.handle_interaction(_interaction(world), plan.requests[0]["request_id"], approve=True)
    assert world.cog._persist_turn.await_count == 0
    await world.supervisor.drain()
    world.cog._persist_turn.assert_awaited_once()
    kwargs = world.cog._persist_turn.call_args.kwargs
    assert kwargs["epoch"] == epoch
    assert kwargs["visibility_scope"] == "private:30"
    assert kwargs["assistant_message"] == spoken
    assert "text" not in (await world.service.store.get(plan.requests[0]["request_id"]))["payload"]


@pytest.mark.asyncio
async def test_rejected_optional_audio_never_enters_conversation_memory(environment):
    world = environment
    context = await build_action_context(world.bot, world.message, world.config)
    plan = await world.service.plan(
        world.message, ChatReply("", (ActionProposal("send_audio", text="Fala que foi recusada.", ask_permission=True),)),
        context, world.config, epoch=MemoryEpoch(), visibility_scope="channel:30",
    )
    await world.service.bind_and_start(plan, world.card)
    await world.service.handle_interaction(_interaction(world), plan.requests[0]["request_id"], approve=False)
    assert world.cog._persist_turn.await_count == 0
    assert world.executor.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_false_model_permission_never_automates_join_or_ban(environment, action):
    world = environment
    in_call(world, 3)
    if action == "join_voice":
        # A entrada só é oferecida quando não existe conexão de voz em uso.
        world.guild.voice_client = None
        world.members[999].voice = None
    context = await build_action_context(world.bot, world.message, world.config)
    plan = await world.service.plan(
        world.message, ChatReply("", (ActionProposal(action, "m1", reason="Violação repetida", ask_permission=False),)),
        context, world.config,
    )
    assert plan.requests[0]["ask_permission"] is True
    await world.service.bind_and_start(plan, world.card)
    assert not world.supervisor.pending and world.executor.await_count == 0
    # Defesa adicional: até um documento alterado com false não executa sozinho.
    altered = dict(await world.service.store.get(plan.requests[0]["request_id"]))
    altered["ask_permission"] = False
    await world.service._start_automatic(altered)
    assert world.executor.await_count == 0
    assert (await world.service.store.get(altered["request_id"]))["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "executor_uncertain"])
async def test_uncertain_execution_has_no_replay_and_purges_speech(environment, monkeypatch, failure):
    world = environment
    request = await _request(world)
    if failure == "timeout":
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        world.executor.side_effect = blocked
        monkeypatch.setattr(actions_module.C, "ACTION_EXECUTION_TIMEOUT_SECONDS", 0.01)
    else:
        world.executor.side_effect = ActionExecutionUncertain("Envio pode ter ocorrido.")
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    await world.supervisor.drain()
    final = await world.service.store.get(request["request_id"])
    assert final["state"] == "uncertain"
    assert "text" not in final["payload"]
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    await world.service._start_automatic(final)
    await world.supervisor.drain()
    assert world.executor.await_count == 1
    assert world.cog._remember_sent_message.await_count == 0


@pytest.mark.asyncio
async def test_startup_restores_optional_views_without_replaying_automatic_audio(environment):
    world = environment
    optional = await _request(world)
    automatic = await _request(world, ask=False)
    restarted = ActionService(world.cog, world.coll)
    restarted.store = ActionStore(world.coll, clock=world.clock)
    await restarted.initialize()
    assert restarted.ready
    assert world.bot.add_view.call_count == 1
    call = world.bot.add_view.call_args
    assert call.kwargs["message_id"] == 60
    assert len(call.args[0].children) == 2
    assert not world.supervisor.pending and world.executor.await_count == 0
    assert (await restarted.store.get(automatic["request_id"]))["state"] == "pending"
    await restarted.handle_interaction(_interaction(world), optional["request_id"], approve=True)
    await world.supervisor.drain()
    assert world.executor.await_count == 1


@pytest.mark.asyncio
async def test_recent_operation_context_has_actual_state_without_private_speech(environment):
    world = environment
    request = await _request(world)
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    await world.supervisor.drain()
    context = await world.service.describe(world.message, world.config)
    assert "send_audio: succeeded" in context.description
    assert "conteúdo privado" not in context.description
    assert "conteúdo privado" not in str(await world.service.store.recent_results(10, 30, 1))


@pytest.mark.asyncio
async def test_safe_mode_returns_frozen_context_without_actions(environment, monkeypatch):
    world = environment
    monkeypatch.setattr(actions_module.C, "SAFE_MODE", True)
    context = await world.service.describe(world.message, world.config)
    assert context.actions == () and "apenas em texto" in context.description


@pytest.mark.asyncio
async def test_two_requests_for_same_member_cannot_execute_at_once(environment):
    world = environment
    first, second = await _request(world), await _request(world)
    await asyncio.gather(
        world.service.handle_interaction(_interaction(world), first["request_id"], approve=True),
        world.service.handle_interaction(_interaction(world), second["request_id"], approve=True),
    )
    states = [(await world.service.store.get(request["request_id"]))["state"]
              for request in (first, second)]
    assert sorted(states) == ["executing", "pending"]
    assert world.service._active_users == {(10, 1)}
    assert len(world.supervisor.pending) == 1
    await world.supervisor.drain()
    assert world.executor.await_count == 1 and world.service._active_users == set()


@pytest.mark.asyncio
async def test_two_member_global_limit_keeps_third_pending_then_accepts_retry(environment, monkeypatch):
    world = environment
    monkeypatch.setattr(actions_module.C, "MAX_CONCURRENT_ACTIONS", 2)
    requests = [await _request(world, requester=mid, target=mid) for mid in (1, 2, 3)]
    for mid, request in zip((1, 2), requests[:2]):
        await world.service.handle_interaction(_interaction(world, actor=mid), request["request_id"], approve=True)
    busy = _interaction(world, actor=3)
    await world.service.handle_interaction(busy, requests[2]["request_id"], approve=True)
    assert world.service._active_users == {(10, 1), (10, 2)}
    assert len(world.supervisor.pending) == 2
    assert (await world.service.store.get(requests[2]["request_id"]))["state"] == "pending"
    assert "ocupado" in busy.notices[0][0]
    await world.supervisor.drain()
    assert world.executor.await_count == 2 and not world.service._active_users
    await world.service.handle_interaction(_interaction(world, actor=3), requests[2]["request_id"], approve=True)
    await world.supervisor.drain()
    assert world.executor.await_count == 3 and not world.service._active_users
    assert (await world.service.store.get(requests[2]["request_id"]))["state"] == "succeeded"


@pytest.mark.asyncio
async def test_busy_automatic_audio_is_rejected_and_private_payload_purged(environment):
    world = environment
    active = await _request(world)
    automatic = await _request(world, ask=False)
    await world.service.handle_interaction(_interaction(world), active["request_id"], approve=True)
    await world.service._start_automatic(automatic)
    refused = await world.service.store.get(automatic["request_id"])
    assert refused["state"] == "rejected" and "text" not in refused["payload"]
    assert world.service._active_users == {(10, 1)}
    await world.supervisor.drain()
    assert world.executor.await_count == 1 and not world.service._active_users


@pytest.mark.asyncio
async def test_exception_releases_admission_slot_and_purges_hidden_payload(environment):
    world = environment
    request = await _request(world)
    world.executor.side_effect = RuntimeError("Disconnected")
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    await world.supervisor.drain()
    final = await world.service.store.get(request["request_id"])
    assert final["state"] == "uncertain" and "text" not in final["payload"]
    assert not world.service._active_users
    next_request = await _request(world)
    world.executor.side_effect = None
    await world.service.handle_interaction(_interaction(world), next_request["request_id"], approve=True)
    await world.supervisor.drain()
    assert world.executor.await_count == 2 and not world.service._active_users


@pytest.mark.asyncio
async def test_cancelled_execution_releases_slot_and_persists_uncertainty(environment):
    world = environment
    request = await _request(world)
    started = asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    world.executor.side_effect = blocked
    await world.service.handle_interaction(_interaction(world), request["request_id"], approve=True)
    coroutine = world.supervisor.pending.pop()
    running = asyncio.create_task(coroutine)
    await asyncio.wait_for(started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    final = await world.service.store.get(request["request_id"])
    assert final["state"] == "uncertain" and "text" not in final["payload"]
    assert not world.service._active_users
    assert world.cog._persist_turn.await_count == 0


@pytest.mark.asyncio
async def test_busy_loser_does_not_release_real_winner_reservation(environment, monkeypatch):
    world = environment
    first, second = await _request(world), await _request(world)
    entered_claim, release_claim = asyncio.Event(), asyncio.Event()
    original_claim = world.service.store.claim

    async def delayed_claim(*args, **kwargs):
        entered_claim.set()
        await release_claim.wait()
        return await original_claim(*args, **kwargs)

    monkeypatch.setattr(world.service.store, "claim", delayed_claim)
    winner = asyncio.create_task(world.service.handle_interaction(
        _interaction(world), first["request_id"], approve=True,
    ))
    await asyncio.wait_for(entered_claim.wait(), timeout=1)
    loser = _interaction(world)
    await world.service.handle_interaction(loser, second["request_id"], approve=True)
    assert world.service._active_users == {(10, 1)}
    assert (await world.service.store.get(second["request_id"]))["state"] == "pending"
    release_claim.set()
    await winner
    assert world.service._active_users == {(10, 1)}
    await world.supervisor.drain()
    assert world.executor.await_count == 1 and not world.service._active_users


@pytest.mark.asyncio
async def test_lost_database_claim_releases_local_slot_for_another_request(environment, monkeypatch):
    world = environment
    first, second = await _request(world), await _request(world)
    original_claim = world.service.store.claim
    monkeypatch.setattr(world.service.store, "claim", AsyncMock(return_value=None))
    await world.service.handle_interaction(_interaction(world), first["request_id"], approve=True)
    assert not world.service._active_users and not world.supervisor.pending
    assert world.executor.await_count == 0
    monkeypatch.setattr(world.service.store, "claim", original_claim)
    await world.service.handle_interaction(_interaction(world), second["request_id"], approve=True)
    assert world.service._active_users == {(10, 1)}
    await world.supervisor.drain()
    assert world.executor.await_count == 1 and not world.service._active_users
