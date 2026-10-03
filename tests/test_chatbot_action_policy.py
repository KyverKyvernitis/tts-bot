"""Autorizações reais de ações e execução sem exposição da fala privada."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.chatbot.action_execution import ActionExecutionUncertain, execute_action
from cogs.chatbot.action_policy import ActionDenied, build_action_context, prepare_action, validate_action
from cogs.chatbot.action_protocol import ActionProposal


def member(guild, mid, *, rank=1, roles=(), ban=False, manage=False, admin=False, bot=False):
    obj = MagicMock(spec=discord.Member)
    obj.id, obj.guild, obj.top_role = mid, guild, rank
    obj.name = obj.display_name = f"membro {mid}"
    obj.roles, obj.bot, obj.voice = [SimpleNamespace(id=rid) for rid in roles], bot, None
    obj.guild_permissions = SimpleNamespace(ban_members=ban, manage_guild=manage, administrator=admin)
    obj.ban = AsyncMock()
    return obj


@pytest.fixture
def world():
    guild = SimpleNamespace(id=10, owner_id=99, voice_client=None)
    members = {1: member(guild, 1), 2: member(guild, 2, rank=10, ban=True),
               3: member(guild, 3, rank=2), 4: member(guild, 4, rank=5, manage=True),
               99: member(guild, 99, rank=100, ban=True, admin=True),
               999: member(guild, 999, rank=20, ban=True, bot=True)}
    guild.me = members[999]
    async def fetch(mid):
        return members.get(mid)
    guild.fetch_member = AsyncMock(side_effect=fetch)
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id, voice.guild = 20, guild
    voice.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
    chat = MagicMock(spec=discord.TextChannel)
    chat.id, chat.guild = 30, guild
    chat.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, attach_files=True)
    chat.send = AsyncMock(return_value=SimpleNamespace(id=88))
    channels = {20: voice, 30: chat}
    guild.get_channel = channels.get
    guild.get_channel_or_thread = channels.get
    config = SimpleNamespace(enabled=True, channel_ids=(), actions_enabled=True, audio_actions_enabled=True, voice_actions_enabled=True,
                             moderation_actions_enabled=True, action_staff_role_ids=())
    tts = SimpleNamespace(synthesize_chatbot_attachment=AsyncMock(return_value=b"mp3 bytes"),
                          chatbot_join_voice=AsyncMock(return_value={"ok": True, "status": "executed"}),
                          chatbot_speak_voice=AsyncMock(return_value={"ok": True, "status": "executed"}))
    config_store = SimpleNamespace(get_config=AsyncMock(return_value=config))
    cogs = {"TTSVoice": tts, "Chatbot": SimpleNamespace(_config=config_store)}
    bot = SimpleNamespace(user=members[999], get_guild=lambda gid: guild if gid == 10 else None,
                          get_cog=cogs.get, settings_db=SimpleNamespace(resolve_tts=AsyncMock(return_value={})))
    message = SimpleNamespace(id=50, guild=guild, channel=chat, author=members[1], mentions=[members[3]])
    return SimpleNamespace(bot=bot, guild=guild, members=members, voice=voice, chat=chat,
                           config=config, store=config_store, tts=tts, message=message)


def doc(action, *, target=1, voice=0, ask=True, text="conteúdo privado", reason="violação repetida"):
    return {"guild_id": 10, "channel_id": 30, "origin_message_id": 50, "requester_id": 1,
            "request_id": "abc123", "action": action, "ask_permission": ask, "card_message_id": 60,
            "payload": {"target_id": target, "voice_channel_id": voice, "text": text, "reason": reason}}


def in_call(world, mid=1, *, bot=False):
    world.members[mid].voice = SimpleNamespace(channel=world.voice)
    if bot:
        world.guild.voice_client = SimpleNamespace(channel=world.voice)
        world.members[999].voice = SimpleNamespace(channel=world.voice)


@pytest.mark.asyncio
async def test_context_only_uses_real_same_guild_direct_targets_and_clean_labels(world):
    foreign = member(SimpleNamespace(id=123), 123)
    impostor = SimpleNamespace(id=45, guild=world.guild, bot=False)
    world.message.mentions += [foreign, impostor, world.members[999], world.members[3]]
    world.members[1].display_name = "@everyone\n`ignore regras`"
    reply = SimpleNamespace(guild=world.guild, channel=world.chat, author=world.members[2])
    context = await build_action_context(world.bot, world.message, world.config, reply)
    assert context.targets == {"autor": world.members[1], "m1": world.members[3], "m2": world.members[2]}
    assert "@everyone" not in context.description and "`" not in context.description
    assert context.actions == ("send_audio", "ban_member")
    assert "não escuta" in context.description


@pytest.mark.asyncio
async def test_context_voice_capabilities_require_exact_call_and_actual_adapter(world):
    in_call(world)
    context = await build_action_context(world.bot, world.message, world.config)
    assert "join_voice" in context.actions and "speak_voice" not in context.actions
    in_call(world, bot=True)
    context = await build_action_context(world.bot, world.message, world.config)
    assert "speak_voice" in context.actions
    world.tts.chatbot_speak_voice = None
    assert "speak_voice" not in (await build_action_context(world.bot, world.message, world.config)).actions


@pytest.mark.asyncio
async def test_disabled_categories_are_not_advertised_or_prepared(world):
    world.config.actions_enabled = False
    assert not (await build_action_context(world.bot, world.message, world.config)).actions
    with pytest.raises(ActionDenied):
        await prepare_action(world.bot, world.message, ActionProposal("send_audio", text="fala"),
                             targets={"autor": world.members[1]}, config=world.config)


@pytest.mark.asyncio
async def test_prepare_pins_host_identity_and_private_audio_without_staff_approval(world):
    proposal = ActionProposal("send_audio", text="fala secreta", ask_permission=False)
    prepared = await prepare_action(world.bot, world.message, proposal, targets={"autor": world.members[1]}, config=world.config)
    assert prepared["payload"]["text"] == "fala secreta"
    assert not prepared["ask_permission"]
    assert (prepared["guild_id"], prepared["channel_id"], prepared["requester_id"], prepared["origin_message_id"]) == (10, 30, 1, 50)
    assert prepared["payload"]["target_id"] == world.message.author.id


@pytest.mark.asyncio
@pytest.mark.parametrize("target_ref", ["", "autor", "m1", "requester", "usuario"])
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_audio_replies_pin_the_author_even_when_model_names_another_target(world, target_ref, action):
    in_call(world, bot=True)
    # Uma menção não deve redirecionar a fala nem exigir que o modelo escolha
    # um membro para responder à conversa atual.
    another_call = MagicMock(spec=discord.VoiceChannel)
    another_call.id, another_call.guild = 21, world.guild
    world.members[3].voice = SimpleNamespace(channel=another_call)
    prepared = await prepare_action(
        world.bot, world.message, ActionProposal(action, target_ref, text="fala privada"),
        targets={"m1": world.members[3]}, config=world.config,
    )
    assert prepared["payload"]["target_id"] == world.message.author.id
    assert prepared["payload"]["voice_channel_id"] == (20 if action == "speak_voice" else 0)
    assert prepared["requester_id"] == world.message.author.id


@pytest.mark.asyncio
async def test_speech_cannot_follow_a_mentioned_member_when_author_is_outside_the_bot_call(world):
    in_call(world, 3, bot=True)
    # m1 está junto do bot; o autor está fora da call. O alvo do modelo não
    # pode servir como autorização para falar em nome de quem pediu.
    with pytest.raises(ActionDenied, match="mesma call"):
        await prepare_action(
            world.bot, world.message, ActionProposal("speak_voice", "m1", text="fala privada"),
            targets={"m1": world.members[3]}, config=world.config,
        )
    other_call = MagicMock(spec=discord.VoiceChannel)
    other_call.id, other_call.guild = 21, world.guild
    world.members[1].voice = SimpleNamespace(channel=other_call)
    with pytest.raises(ActionDenied, match="mesma call"):
        await prepare_action(
            world.bot, world.message, ActionProposal("speak_voice", "m1", text="fala privada"),
            targets={"m1": world.members[3]}, config=world.config,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
@pytest.mark.parametrize("target_ref", ["requester", "usuario", "m77"])
async def test_operations_on_members_still_require_trusted_target_references(world, action, target_ref):
    in_call(world, 3)
    with pytest.raises(ActionDenied, match="membro identificado"):
        await prepare_action(
            world.bot, world.message, ActionProposal(action, target_ref, reason="spam repetido"),
            targets={"autor": world.members[1], "m1": world.members[3]}, config=world.config,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_common_member_can_approve_own_audio_but_other_member_cannot(world, action):
    in_call(world, bot=True)
    action_doc = doc(action, voice=20)
    await validate_action(world.bot, action_doc, 1)
    world.store.get_config.assert_awaited_with(10, fresh=True)
    with pytest.raises(ActionDenied, match="Somente o membro"):
        await validate_action(world.bot, action_doc, 2)


@pytest.mark.asyncio
async def test_speech_rejects_moved_requester_or_disconnected_bot(world):
    in_call(world, bot=True)
    action_doc = doc("speak_voice", voice=20)
    world.members[1].voice = None
    with pytest.raises(ActionDenied, match="saiu ou mudou"):
        await validate_action(world.bot, action_doc, 1)
    in_call(world)
    world.guild.voice_client = None
    world.members[999].voice = None
    with pytest.raises(ActionDenied, match="continuar na call"):
        await validate_action(world.bot, action_doc, 1)


@pytest.mark.asyncio
async def test_join_forces_staff_approval_and_channel_pin(world):
    in_call(world, 3)
    prepared = await prepare_action(world.bot, world.message, ActionProposal("join_voice", "m1"),
                                    targets={"m1": world.members[3]}, config=world.config)
    assert prepared["ask_permission"] and prepared["payload"]["voice_channel_id"] == 20
    with pytest.raises(ActionDenied, match="Somente a staff"):
        await validate_action(world.bot, prepared, 1)
    await validate_action(world.bot, prepared, 4)
    world.members[3].voice = None
    with pytest.raises(ActionDenied, match="saiu ou mudou"):
        await validate_action(world.bot, prepared, 4)
    # Sair da call não impede a staff de encerrar o pedido.
    await validate_action(world.bot, prepared, 4, reject=True)


@pytest.mark.asyncio
async def test_configured_staff_role_can_join_without_manage_guild(world):
    in_call(world, 3)
    world.config.action_staff_role_ids = (77,)
    world.members[1].roles = [SimpleNamespace(id=77)]
    await validate_action(world.bot, doc("join_voice", target=3, voice=20), 1)


@pytest.mark.asyncio
async def test_stage_and_missing_voice_permissions_are_denied(world):
    stage = MagicMock(spec=discord.StageChannel)
    stage.id, stage.guild = 21, world.guild
    world.members[3].voice = SimpleNamespace(channel=stage)
    with pytest.raises(ActionDenied):
        await prepare_action(world.bot, world.message, ActionProposal("join_voice", "m1"),
                             targets={"m1": world.members[3]}, config=world.config)
    in_call(world, 3)
    world.voice.permissions_for.return_value.connect = False
    with pytest.raises(ActionDenied, match="permissão"):
        await validate_action(world.bot, doc("join_voice", target=3, voice=20), 4)


@pytest.mark.asyncio
async def test_ban_requires_reason_and_cannot_target_owner_or_bots(world):
    for target in (world.members[1], world.members[99], world.members[999]):
        with pytest.raises(ActionDenied):
            await prepare_action(world.bot, world.message, ActionProposal("ban_member", "m1", reason="motivo"),
                                 targets={"m1": target}, config=world.config)
    with pytest.raises(ActionDenied, match="motivo"):
        await prepare_action(world.bot, world.message, ActionProposal("ban_member", "m1"),
                             targets={"m1": world.members[3]}, config=world.config)
    prepared = await prepare_action(world.bot, world.message, ActionProposal("ban_member", "m1", reason="motivo"),
                                    targets={"m1": world.members[3]}, config=world.config)
    assert prepared["ask_permission"] and prepared["payload"]["text"] == ""


@pytest.mark.asyncio
async def test_manage_guild_does_not_grant_ban_and_fresh_hierarchy_is_enforced(world):
    action_doc = doc("ban_member", target=3)
    with pytest.raises(ActionDenied, match="Banir membros"):
        await validate_action(world.bot, action_doc, 4)
    await validate_action(world.bot, action_doc, 2)
    world.members[3].top_role = 10
    with pytest.raises(ActionDenied, match="Seu cargo"):
        await validate_action(world.bot, action_doc, 2)
    world.members[3].top_role = 20
    with pytest.raises(ActionDenied, match="hierarquia"):
        await validate_action(world.bot, action_doc, 2)


@pytest.mark.asyncio
async def test_configured_staff_roles_restrict_ban_without_removing_permission_requirement(world):
    world.config.action_staff_role_ids = (77,)
    with pytest.raises(ActionDenied, match="autorização"):
        await validate_action(world.bot, doc("ban_member", target=3), 2)
    world.members[2].roles = [SimpleNamespace(id=77)]
    await validate_action(world.bot, doc("ban_member", target=3), 2)
    world.members[1].roles = [SimpleNamespace(id=77)]
    with pytest.raises(ActionDenied, match="Banir membros"):
        await validate_action(world.bot, doc("ban_member", target=3), 1)


@pytest.mark.asyncio
async def test_lost_member_or_disabled_action_denied_on_fresh_validation(world):
    action_doc = doc("send_audio")
    world.members.pop(1)
    with pytest.raises(ActionDenied, match="não está mais"):
        await validate_action(world.bot, action_doc, 1)
    world.config.audio_actions_enabled = False
    with pytest.raises(ActionDenied, match="desativada"):
        await validate_action(world.bot, action_doc, 2)


@pytest.mark.asyncio
async def test_authorized_rejection_can_close_a_request_after_action_disabled(world):
    world.config.actions_enabled = False
    await validate_action(world.bot, doc("ban_member", target=3), 2, reject=True)
    await validate_action(world.bot, doc("send_audio"), 1, reject=True)
    with pytest.raises(ActionDenied):
        await validate_action(world.bot, doc("send_audio"), 2, reject=True)


@pytest.mark.asyncio
async def test_audio_executes_in_original_channel_without_transcript_or_mentions(world):
    result = await execute_action(world.bot, doc("send_audio"), actor_id=1)
    assert result.public_result == "Áudio enviado." and result.message_id == 88
    kwargs = world.chat.send.await_args.kwargs
    assert "content" not in kwargs and kwargs["reference"].message_id == 60
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert kwargs["file"].filename == "resposta.mp3"
    assert world.tts.synthesize_chatbot_attachment.await_args.kwargs["text"] == "conteúdo privado"
    assert "conteúdo privado" not in str(result)


@pytest.mark.asyncio
async def test_oversize_audio_or_missing_attach_permission_sends_nothing(world):
    world.tts.synthesize_chatbot_attachment.return_value = b"a" * (8 * 1024 * 1024 + 1)
    with pytest.raises(ActionDenied):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.chat.send.assert_not_awaited()
    world.tts.synthesize_chatbot_attachment.return_value = b"mp3"
    world.chat.permissions_for.return_value.attach_files = False
    with pytest.raises(ActionDenied):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.chat.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_speech_adapter_receives_private_text_and_uncertain_does_not_retry(world):
    in_call(world, bot=True)
    world.tts.chatbot_speak_voice.return_value = {"ok": False, "status": "uncertain"}
    with pytest.raises(ActionExecutionUncertain) as error:
        await execute_action(world.bot, doc("speak_voice", voice=20), actor_id=1)
    assert "conteúdo privado" not in str(error.value)
    kwargs = world.tts.chatbot_speak_voice.await_args.kwargs
    assert (kwargs["guild_id"], kwargs["user_id"], kwargs["channel_id"], kwargs["request_id"], kwargs["text"]) == (
        10, 1, 20, "abc123", "conteúdo privado")
    assert callable(kwargs["before_effect"])
    world.tts.chatbot_speak_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_calls_canonical_adapter_after_fresh_staff_check(world):
    in_call(world, 3)
    result = await execute_action(world.bot, doc("join_voice", target=3, voice=20), actor_id=4)
    assert result.public_result == "Entrou na call autorizada."
    kwargs = world.tts.chatbot_join_voice.await_args.kwargs
    assert (kwargs["guild_id"], kwargs["user_id"], kwargs["channel_id"], kwargs["request_id"]) == (10, 3, 20, "abc123")
    assert callable(kwargs["before_effect"])
    world.tts.chatbot_join_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_ban_preserves_history_audits_approval_and_rechecks_permission_before_api(world):
    result = await execute_action(world.bot, doc("ban_member", target=3), actor_id=2)
    assert "preservado" in result.public_result
    kwargs = world.members[3].ban.await_args.kwargs
    assert kwargs["delete_message_seconds"] == 0
    assert "aprovação 2" in kwargs["reason"] and "abc123" in kwargs["reason"]
    world.members[3].ban.reset_mock()
    world.members[2].guild_permissions.ban_members = False
    with pytest.raises(ActionDenied):
        await execute_action(world.bot, doc("ban_member", target=3), actor_id=2)
    world.members[3].ban.assert_not_awaited()


@pytest.mark.asyncio
async def test_ban_timeout_is_uncertain_and_never_retried(world):
    import asyncio
    world.members[3].ban.side_effect = asyncio.TimeoutError()
    with pytest.raises(ActionExecutionUncertain):
        await execute_action(world.bot, doc("ban_member", target=3), actor_id=2)
    world.members[3].ban.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["enabled", "actions_enabled", "audio_actions_enabled"])
async def test_synthesis_does_not_keep_authority_after_live_setting_disabled(world, setting):
    async def synthesize(**kwargs):
        setattr(world.config, setting, False)
        return b"mp3"
    world.tts.synthesize_chatbot_attachment.side_effect = synthesize
    with pytest.raises(ActionDenied):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.chat.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_rechecks_channel_allowlist_and_requester_visibility_after_synthesis(world):
    async def synthesize(**kwargs):
        world.config.channel_ids = (9999,)
        return b"mp3"
    world.tts.synthesize_chatbot_attachment.side_effect = synthesize
    with pytest.raises(ActionDenied, match="canal original"):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.chat.send.assert_not_awaited()
    world.config.channel_ids = ()
    async def no_access(**kwargs):
        world.chat.permissions_for.side_effect = lambda m: SimpleNamespace(
            view_channel=m.id != 1, send_messages=True, attach_files=True)
        return b"mp3"
    world.tts.synthesize_chatbot_attachment.side_effect = no_access
    with pytest.raises(ActionDenied, match="solicitante"):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.chat.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_actions_require_requester_to_keep_original_channel_access(world):
    world.chat.permissions_for.side_effect = lambda m: SimpleNamespace(
        view_channel=m.id != 1, send_messages=True, attach_files=True)
    with pytest.raises(ActionDenied, match="solicitante"):
        await validate_action(world.bot, doc("ban_member", target=3), 2)
    with pytest.raises(ActionDenied, match="solicitante"):
        await validate_action(world.bot, doc("join_voice", target=3, voice=20), 4)


@pytest.mark.asyncio
async def test_safe_mode_blocks_effects_after_synthesis_without_blocking_rejection(world, monkeypatch):
    from cogs.chatbot import constants as C
    monkeypatch.setattr(C, "SAFE_MODE", False)
    async def synthesize(**kwargs):
        monkeypatch.setattr(C, "SAFE_MODE", True)
        return b"mp3"
    world.tts.synthesize_chatbot_attachment.side_effect = synthesize
    with pytest.raises(ActionDenied):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.chat.send.assert_not_awaited()
    await validate_action(world.bot, doc("send_audio"), 1, reject=True)


@pytest.mark.asyncio
async def test_absent_config_store_fails_closed(world):
    chatbot = world.bot.get_cog("Chatbot")
    chatbot._config = None
    with pytest.raises(ActionDenied, match="desativado"):
        await validate_action(world.bot, doc("send_audio"), 1)


@pytest.mark.asyncio
async def test_voice_adapter_receives_guard_that_checks_live_authority_after_waits(world):
    in_call(world, bot=True)
    async def adapter(**kwargs):
        world.config.voice_actions_enabled = False
        await kwargs["before_effect"]()
        raise AssertionError("must not reach voice effect")
    world.tts.chatbot_speak_voice.side_effect = adapter
    with pytest.raises(ActionDenied):
        await execute_action(world.bot, doc("speak_voice", voice=20), actor_id=1)


@pytest.mark.asyncio
async def test_private_thread_membership_checked_fresh_for_requester(world):
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.guild, thread.parent_id = 31, world.guild, 30
    thread.is_private.return_value = True
    thread.permissions_for.return_value = SimpleNamespace(view_channel=True, manage_threads=False,
                                                          send_messages_in_threads=True, attach_files=True)
    thread.fetch_member = AsyncMock(return_value=SimpleNamespace(id=1))
    world.guild.get_channel_or_thread = lambda cid: thread if cid == 31 else None
    world.config.channel_ids = (30,)
    action_doc = doc("ban_member", target=3)
    action_doc["channel_id"] = 31
    await validate_action(world.bot, action_doc, 2)
    thread.fetch_member.assert_awaited_with(1)
    thread.fetch_member.return_value = None
    with pytest.raises(ActionDenied, match="conversa privada"):
        await validate_action(world.bot, action_doc, 2)


@pytest.mark.asyncio
async def test_member_lookups_cannot_preserve_config_authority_changed_during_validation(world):
    disabled = SimpleNamespace(**vars(world.config))
    disabled.enabled = False
    world.store.get_config.side_effect = [world.config, disabled]
    with pytest.raises(ActionDenied, match="desativado"):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.chat.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_voice_target_not_advertised_or_pinned_without_requester_visibility(world):
    in_call(world, 3)
    world.voice.permissions_for.side_effect = lambda m: SimpleNamespace(
        view_channel=m.id != 1, connect=True, speak=True)
    context = await build_action_context(world.bot, world.message, world.config)
    assert "join_voice" not in context.actions
    assert "m1: membro 3 (call indisponível nesta conversa)" in context.description
    with pytest.raises(ActionDenied, match="poder ver"):
        await prepare_action(world.bot, world.message, ActionProposal("join_voice", "m1"),
                             targets=context.targets, config=world.config)


@pytest.mark.asyncio
async def test_join_approval_requires_staff_and_requester_to_see_target_call(world):
    in_call(world, 3)
    action_doc = doc("join_voice", target=3, voice=20)
    world.voice.permissions_for.side_effect = lambda m: SimpleNamespace(
        view_channel=m.id != 4, connect=True, speak=True)
    with pytest.raises(ActionDenied, match="Você precisa poder ver"):
        await validate_action(world.bot, action_doc, 4)
    world.voice.permissions_for.side_effect = lambda m: SimpleNamespace(
        view_channel=m.id != 1, connect=True, speak=True)
    with pytest.raises(ActionDenied, match="solicitante precisa poder ver"):
        await validate_action(world.bot, action_doc, 4)


@pytest.mark.asyncio
async def test_voice_visibility_rechecked_after_final_config_await(world):
    in_call(world, 3)
    count = 0
    async def config_getter(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            world.voice.permissions_for.side_effect = lambda m: SimpleNamespace(
                view_channel=m.id != 4, connect=True, speak=True)
        return world.config
    world.store.get_config.side_effect = config_getter
    with pytest.raises(ActionDenied, match="Você precisa poder ver"):
        await validate_action(world.bot, doc("join_voice", target=3, voice=20), 4)


@pytest.mark.asyncio
async def test_busy_readonly_voice_precheck_filters_both_capabilities(world):
    in_call(world, 3)
    world.tts._chatbot_voice_precheck = MagicMock(return_value=(world.guild, world.voice, "música ocupada"))
    context = await build_action_context(world.bot, world.message, world.config)
    assert "join_voice" not in context.actions
    world.tts._chatbot_voice_precheck.assert_called_with(guild_id=10, user_id=3, channel_id=20, require_connected=False)
    in_call(world, bot=True)
    context = await build_action_context(world.bot, world.message, world.config)
    assert "speak_voice" not in context.actions and "join_voice" not in context.actions
    assert "música ocupada" not in context.description
    assert "já está na call do autor" in context.description
    world.tts._chatbot_voice_precheck.assert_called_with(guild_id=10, user_id=1, channel_id=20, require_connected=True)


@pytest.mark.asyncio
async def test_existing_bot_call_never_advertises_join_or_proposes_movement(world):
    in_call(world, 3)
    another = MagicMock(spec=discord.VoiceChannel)
    another.id, another.guild = 4567, world.guild
    world.guild.voice_client = SimpleNamespace(channel=another)
    context = await build_action_context(world.bot, world.message, world.config)
    assert "join_voice" not in context.actions
    assert "4567" not in context.description
    with pytest.raises(ActionDenied, match="sessão de voz"):
        await prepare_action(world.bot, world.message, ActionProposal("join_voice", "m1"),
                             targets=context.targets, config=world.config)


@pytest.mark.asyncio
async def test_cross_guild_voice_channel_is_never_available_or_pinned(world):
    foreign = MagicMock(spec=discord.VoiceChannel)
    foreign.id, foreign.guild = 54321, SimpleNamespace(id=999999)
    foreign.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
    world.members[3].voice = SimpleNamespace(channel=foreign)
    context = await build_action_context(world.bot, world.message, world.config)
    assert "join_voice" not in context.actions
    with pytest.raises(ActionDenied, match="poder ver"):
        await prepare_action(world.bot, world.message, ActionProposal("join_voice", "m1"),
                             targets=context.targets, config=world.config)
