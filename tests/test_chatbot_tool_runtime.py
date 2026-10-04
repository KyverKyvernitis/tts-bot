"""Ferramentas operacionais usam dados reais e confirmação de envio exata."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.chatbot import action_policy as policy
from cogs.chatbot.action_protocol import NativeToolCall
from cogs.chatbot.memory import MemoryEntry, MemoryEpoch
from cogs.chatbot.preferences import ConversationPreferences
from cogs.chatbot.tool_registry import ToolSpec
from cogs.chatbot.tool_runtime import build_tool_registry, drain_action_proposals, execute_native_tool, refresh_action_spec
from cogs.chatbot.tool_runtime import refresh_tool_context
from cogs.chatbot.action_drafts import ActionDraftStore
from tests.test_chatbot_action_flow import world, _member
from tests.test_chatbot_tool_memory import Collection


class DraftCollection(Collection):
    async def find_one_and_replace(self, query, replacement, **kwargs):
        from copy import deepcopy
        from tests.test_chatbot_action_flow import _matches
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs[index] = deepcopy(replacement)
                return deepcopy(replacement)
        return None


def install_drafts(w):
    clock = [100.0]
    collection = DraftCollection()
    w.cog._action_drafts = ActionDraftStore(collection, memory=w.cog._memory, clock=lambda: clock[0])
    return collection, clock


async def registry_for(w):
    return await build_tool_registry(w.cog, w.message, w.config, epoch=w.epoch,
                                     visibility_scope="channel:30")


async def call(registry, name, **arguments):
    return await execute_native_tool(registry, NativeToolCall(name + "-id", name, arguments))


def install_replies(w, monkeypatch, *, original_format="audio"):
    record = {"message_id": 70, "format": original_format, "text": "",
              "spoken_text": "Essa é a resposta exata, pô.", "provider": "groq", "model": "real"}
    w.cog._reply_store = SimpleNamespace(resolve=AsyncMock(return_value=record), record_sent=AsyncMock())
    w.cog._remember_sent_message = AsyncMock()
    w.cog._mirror_sent_audio = AsyncMock()
    w.cog.record_audio_reply_sent = AsyncMock()
    validate = AsyncMock(return_value=w.card)
    synthesis = AsyncMock(return_value=b"exact-original-mp3")
    monkeypatch.setattr("cogs.chatbot.reply_store.validate_recorded_reply", validate)
    monkeypatch.setattr("cogs.chatbot.audio.recorded_reply_audio", synthesis)
    return record, validate, synthesis


@pytest.mark.asyncio
async def test_action_catalog_requires_real_backend_not_only_enabled_flags(world):
    original = world.cog.bot.get_cog
    world.cog.bot.get_cog = lambda name: None if name == "TTSVoice" else original(name)
    registry = await registry_for(world)
    assert not set(registry.runtime.action_context.actions) & {"send_audio", "speak_voice", "join_voice"}
    assert "ban_member" in registry.runtime.action_context.actions
    assert not registry.get("list_tts_voices_languages").available


@pytest.mark.asyncio
async def test_unready_action_service_preserves_reads_but_never_proposes_or_schedules(world):
    world.cog._actions.ready = False
    registry = await registry_for(world)
    assert not registry.runtime.action_context.actions
    spec = next(spec for spec in registry.snapshot() if spec.name == "propor_acao")
    assert not spec.available and spec.why
    result = await call(registry, "propor_acao", action="ban_member", target_ref="m1", reason="spam")
    assert result["status"] == "unavailable"
    assert (await call(registry, "get_operational_state"))["ok"]
    assert drain_action_proposals(registry) == ()
    assert not world.collection.docs and not world.cog._supervisor.jobs
    world.members[3].ban.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_service_stopping_after_catalog_snapshot_prevents_preparation(world, monkeypatch):
    registry = await registry_for(world)
    world.cog._actions.ready = False
    prepare = AsyncMock()
    monkeypatch.setattr(policy, "prepare_action", prepare)
    result = await call(registry, "propor_acao", action="ban_member", target_ref="m1", reason="spam")
    assert result["status"] == "unavailable"
    prepare.assert_not_awaited()
    assert drain_action_proposals(registry) == () and not world.collection.docs


@pytest.mark.asyncio
async def test_resolved_member_updates_real_action_schema_without_mutating_other_members(world):
    world.members[4] = _member(world.guild, 4, rank=2)
    registry = await registry_for(world)
    result = await call(registry, "resolve_member", query="<@4>")
    assert result["ok"] and result["data"]["members"][0]["id"] == "4"
    ref = result["data"]["members"][0]["ref"]
    assert registry.runtime.targets[ref] is world.members[4]
    enums = registry.get("propor_acao").parameters["properties"]["target_ref"]["enum"]
    assert ref in enums and "<@4>" in enums and "4" in enums
    assert not world.collection.docs


@pytest.mark.asyncio
async def test_temporary_response_format_does_not_write_saved_preferences(world):
    world.cog._preferences = SimpleNamespace(
        get_current=AsyncMock(return_value=ConversationPreferences("audio")), set_current=AsyncMock())
    registry = await registry_for(world)
    result = await call(registry, "select_response_format", mode="text")
    assert result["data"] == {"mode": "text", "persistent": False}
    assert registry.runtime.response_format == "text"
    world.cog._preferences.set_current.assert_not_awaited()
    assert (await registry_for(world)).runtime.response_format is None


@pytest.mark.asyncio
async def test_voice_preferences_use_real_catalog_and_preserve_scope(world):
    world.tts.edge_voice_names = {"pt-BR-FranciscaNeural"}
    world.tts.gtts_languages = {"pt": "Português"}
    world.cog._preferences = SimpleNamespace(set_current=AsyncMock(return_value=ConversationPreferences("audio", "pt-BR-FranciscaNeural", "pt")))
    registry = await registry_for(world)
    invalid = await call(registry, "set_conversation_preferences", voice="inventada")
    assert not invalid["ok"] and invalid["status"] == "failed"
    world.cog._preferences.set_current.assert_not_awaited()
    catalog = await call(registry, "list_tts_voices_languages", query="pt")
    assert catalog["data"]["voices"] == ["pt-BR-FranciscaNeural"]
    assert catalog["data"]["languages"] == [{"code": "pt", "name": "Português"}]
    result = await call(registry, "set_conversation_preferences", voice="pt-BR-FranciscaNeural", language="pt", mode="audio")
    assert result["ok"]
    world.cog._preferences.set_current.assert_awaited_once_with(10, 30, 1, world.epoch,
        voice="pt-BR-FranciscaNeural", language="pt", mode="audio")


@pytest.mark.asyncio
async def test_own_requests_return_json_dates_and_only_bound_requester_scope(world):
    created = datetime(2026, 10, 3, tzinfo=timezone.utc)
    world.cog._actions.list_pending = AsyncMock(return_value=[{"request_id": "safe-id", "created_at": created}])
    world.cog._actions.cancel_pending = AsyncMock(return_value=True)
    registry = await registry_for(world)
    result = await call(registry, "list_own_action_requests")
    assert result["data"]["requests"] == [{"request_id": "safe-id", "created_at": created.isoformat()}]
    world.cog._actions.list_pending.assert_awaited_once_with(guild_id=10, channel_id=30, requester_id=1)
    await call(registry, "cancel_own_action_request", request_id="safe-id")
    world.cog._actions.cancel_pending.assert_awaited_once_with("safe-id", guild_id=10, channel_id=30, requester_id=1)


@pytest.mark.asyncio
async def test_query_memory_only_reads_author_history_and_explicit_channel_facts(world):
    world.cog._memory._coll = Collection()
    world.cog._memory.get_user_history = AsyncMock(return_value=[MemoryEntry("user", "Meu gato chama Pipoca", user_id=1)])
    registry = await registry_for(world)
    saved = await call(registry, "remember_own_fact", content="Meu gato chama Pipoca")
    assert saved["ok"]
    result = await call(registry, "query_own_memory", query="Pipoca")
    assert result["data"]["facts"] == [saved["data"]]
    world.cog._memory.get_user_history.assert_awaited_once_with(10, 1, channel_id=30,
        visibility_scope="channel:30", epoch=world.epoch)
    assert (await call(registry, "forget_own_fact", ref=saved["data"]["ref"]))["data"]["removed"]


@pytest.mark.asyncio
async def test_operational_state_reports_bot_identity_avatar_and_no_call_listening(world):
    world.members[999].display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/avatars/public.png")
    world.voice.name = "call real"
    registry = await registry_for(world)
    result = await call(registry, "get_operational_state")
    assert result["data"]["identity"] == {"id": "999", "name": "membro 999", "avatar_url": "https://cdn.discordapp.com/avatars/public.png"}
    assert result["data"]["voice"]["connected"]
    assert result["data"]["voice"]["listening"] is False
    assert result["data"]["voice"]["channel"] == "call real"


@pytest.mark.asyncio
async def test_guard_rechecks_epoch_after_last_configuration_await(world):
    registry = await registry_for(world)
    calls = 0
    async def reset_during_final_config(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            world.cog._memory.capture_epoch.return_value = MemoryEpoch(2, 3, 5)
        return world.config
    world.cog._config.get_config.side_effect = reset_during_final_config
    result = await call(registry, "select_response_format", mode="audio")
    assert not result["ok"] and "reiniciada" in result["reason"]
    assert registry.runtime.response_format is None


@pytest.mark.asyncio
async def test_audio_guard_rechecks_configuration_after_fetching_fresh_bot_member(world):
    registry = await registry_for(world)
    disabled = replace(world.config, audio_actions_enabled=False)
    async def revoke_during_fetch(identifier):
        if identifier == 999:
            world.cog._config.get_config.return_value = disabled
        return world.members[identifier]
    world.guild.fetch_member.side_effect = revoke_during_fetch
    with pytest.raises(policy.ActionDenied):
        await registry.runtime.guard(audio=True)


@pytest.mark.asyncio
async def test_audio_conversion_delivers_and_mirrors_identical_bytes_despite_auxiliary_errors(world, monkeypatch):
    record, validator, synthesis = install_replies(world, monkeypatch)
    world.cog._remember_sent_message.side_effect = OSError("PRIVATE_SECRET")
    world.cog._reply_store.record_sent.side_effect = OSError("PRIVATE_SECRET")
    captured = []
    async def send(**kwargs):
        captured.append(kwargs["file"].fp.getvalue())
        return SimpleNamespace(id=88, attachments=[])
    world.channel.send.side_effect = send
    registry = await registry_for(world)
    result = await call(registry, "convert_reply_audio")
    assert result == {"ok": True, "status": "audio_sent", "data": {"message_id": "88", "reused_audio": True}}
    assert captured == [b"exact-original-mp3"] and registry.runtime.response_sent
    world.cog._mirror_sent_audio.assert_awaited_once()
    assert world.cog._mirror_sent_audio.await_args.kwargs["audio"] == captured[0]
    synthesis.assert_awaited_once()
    validator.assert_awaited_once_with(world.cog.bot, record, user_id=1)
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversion_revalidates_original_source_before_publication(world, monkeypatch):
    _record, validator, _synthesis = install_replies(world, monkeypatch)
    validator.return_value = None
    registry = await registry_for(world)
    assert not (await call(registry, "convert_reply_audio"))["ok"]
    assert not (await call(registry, "convert_reply_text"))["ok"]
    world.channel.send.assert_not_awaited()
    world.cog._mirror_sent_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_conversion_uses_exact_spoken_text_and_keeps_confirmation_if_metadata_fails(world, monkeypatch):
    record, _validator, _synthesis = install_replies(world, monkeypatch)
    world.cog._remember_sent_message.side_effect = OSError("private")
    world.cog._reply_store.record_sent.side_effect = OSError("private")
    registry = await registry_for(world)
    result = await call(registry, "convert_reply_text")
    assert result["status"] == "reply_sent" and result["data"]["exact_text"]
    assert world.channel.send.await_args.args == (record["spoken_text"],)
    world.cog._reply_store.record_sent.assert_awaited_once()
    world.cog._mirror_sent_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivered_audio_remains_confirmed_if_turn_is_cancelled_during_metadata(world, monkeypatch):
    install_replies(world, monkeypatch)
    world.cog._remember_sent_message.side_effect = asyncio.CancelledError()
    registry = await registry_for(world)
    result = await call(registry, "convert_reply_audio")
    assert result["status"] == "audio_sent" and registry.runtime.response_sent
    world.channel.send.assert_awaited_once()
    world.cog._mirror_sent_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_send_failure_is_uncertain_and_never_exposes_internal_error(world, monkeypatch):
    install_replies(world, monkeypatch)
    world.channel.send.side_effect = OSError("PRIVATE_SECRET_URL_TOKEN")
    registry = await registry_for(world)
    result = await call(registry, "convert_reply_audio")
    assert not result["ok"] and result["status"] == "uncertain"
    assert "PRIVATE_SECRET" not in str(result)
    world.cog._reply_store.record_sent.assert_not_awaited()
    world.cog._mirror_sent_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_own_speech_pins_existing_session_without_staff_approval(world):
    ref = {"voice_channel_id": 20, "session_ref": "session-existing", "request_id": "own-speech"}
    world.tts.chatbot_own_speech_ref = Mock(return_value=ref)
    world.tts.chatbot_interrupt_speech = AsyncMock(return_value={"ok": True, "status": "executed"})
    registry = await registry_for(world)
    result = await call(registry, "interrupt_own_speech")
    assert result["ok"] and result["data"]["interrupted"]
    world.tts.chatbot_own_speech_ref.assert_called_once_with(10, 1)
    kwargs = world.tts.chatbot_interrupt_speech.await_args.kwargs
    assert kwargs["user_id"] == 1 and kwargs["channel_id"] == 20
    assert kwargs["session_ref"] == "session-existing" and kwargs["speech_request_id"] == "own-speech"
    assert not world.collection.docs


@pytest.mark.asyncio
async def test_unknown_or_malformed_tools_cannot_invoke_handler(world):
    registry = await registry_for(world)
    handler = AsyncMock()
    registry.register(ToolSpec("test_declared", "test", {"type": "object", "properties": {}, "additionalProperties": False}, handler=handler))
    assert (await call(registry, "terminal_command", cmd="echo hi"))["status"] == "unavailable"
    assert not (await call(registry, "test_declared", injected=True))["ok"]
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_proposal_invalidates_entire_group_even_direct_runtime_drain(world, monkeypatch):
    registry = await registry_for(world)
    prepare = AsyncMock(side_effect=[{"action": "ban_member", "payload": {}, "ask_permission": True}, asyncio.CancelledError()])
    monkeypatch.setattr(policy, "prepare_action", prepare)
    assert (await call(registry, "propor_acao", action="ban_member", target_ref="m1", reason="spam"))["ok"]
    with pytest.raises(asyncio.CancelledError):
        await call(registry, "propor_acao", action="ban_member", target_ref="m1", reason="spam")
    assert registry.runtime.proposals_invalid and not registry.runtime.prepared_actions
    assert drain_action_proposals(registry) == ()
    assert not world.collection.docs


@pytest.mark.asyncio
async def test_move_then_speak_passes_pinned_source_and_destination_to_policy(world, monkeypatch):
    registry = await registry_for(world)
    registry.runtime.action_context = replace(registry.runtime.action_context, actions=("move_voice", "speak_voice"))
    refresh_action_spec(registry)
    observed = []
    async def prepare(bot, message, proposal, *, targets, config, deferred_voice_channel_id=None,
                      deferred_voice_source_channel_id=None, resources=None):
        observed.append((proposal.action, deferred_voice_channel_id, deferred_voice_source_channel_id))
        return {"action": proposal.action, "ask_permission": proposal.action == "move_voice",
                "payload": {"voice_channel_id": 20, "source_voice_channel_id": 21}}
    monkeypatch.setattr(policy, "prepare_action", prepare)
    assert (await call(registry, "propor_acao", action="move_voice", target_ref="autor"))["ok"]
    assert (await call(registry, "propor_acao", action="speak_voice", text="fala exata"))["ok"]
    assert observed == [("move_voice", None, None), ("speak_voice", 20, 21)]
    assert len(drain_action_proposals(registry)) == 2


@pytest.mark.asyncio
async def test_action_draft_roundtrip_resolves_same_target_when_old_alias_belongs_to_another_member(world):
    collection, _clock = install_drafts(world)
    registry = await registry_for(world)
    saved = await call(registry, "save_action_draft", action="timeout_member", target_ref="m1", reason="spam confirmado")
    assert saved["status"] == "draft_saved" and saved["data"]["requires_completion"]
    assert saved["data"]["draft"]["target_id"] == 3
    assert saved["data"]["draft"]["missing_fields"] == ["options.duration_seconds"]
    assert collection.docs[0]["target_id"] == 3 and "target_ref" not in collection.docs[0]
    assert not world.collection.docs and not world.cog._supervisor.jobs
    world.members[4] = _member(world.guild, 4, rank=2)
    world.message.mentions = [world.members[4]]
    resumed = await registry_for(world)
    draft = resumed.runtime.action_draft
    assert draft["target_id"] == 3 and draft["target_ref"] != "m1"
    assert resumed.runtime.targets["m1"].id == 4
    assert resumed.runtime.targets[draft["target_ref"]].id == 3
    assert draft["target_name"] == "membro 3"
    completed = await call(resumed, "save_action_draft", options={"duration_seconds": 27 * 60})
    assert completed["ok"] and not completed["data"]["requires_completion"]
    assert completed["data"]["draft"]["target_id"] == 3
    assert completed["data"]["draft"]["reason"] == "spam confirmado"
    assert collection.docs[0]["options"] == {"duration_seconds": 1620}
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_draft_save_rejects_untrusted_member_or_resource_ids_without_creating_authority(world):
    collection, _clock = install_drafts(world)
    registry = await registry_for(world)
    invalid_target = await call(registry, "save_action_draft", action="timeout_member", target_ref="77")
    assert not invalid_target["ok"]
    invalid_resource = await call(registry, "save_action_draft", action="assign_role", target_ref="m1",
                                  options={"role_ref": "77"})
    assert not invalid_resource["ok"] and not collection.docs


@pytest.mark.asyncio
async def test_draft_expiry_and_memory_reset_do_not_restore_old_targets(world):
    _collection, clock = install_drafts(world)
    await world.cog._action_drafts.save(10, 30, 1, world.epoch, action="timeout_member", target_id=3)
    clock[0] += 601
    assert (await registry_for(world)).runtime.action_draft is None
    await world.cog._action_drafts.save(10, 30, 1, world.epoch, action="timeout_member", target_id=3)
    world.epoch = MemoryEpoch(2, 3, 5)
    world.cog._memory.capture_epoch.return_value = world.epoch
    assert (await registry_for(world)).runtime.action_draft is None


@pytest.mark.asyncio
async def test_departed_draft_target_clears_draft_without_selecting_new_member_or_executing(world):
    collection, _clock = install_drafts(world)
    await world.cog._action_drafts.save(10, 30, 1, world.epoch, action="timeout_member", target_id=3)
    world.members.pop(3)
    world.message.mentions = []
    registry = await registry_for(world)
    assert registry.runtime.action_draft is None and registry.runtime.action_draft_error
    assert not collection.docs and not world.collection.docs
    assert not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_draft_resource_aliases_persist_as_ids_and_are_restored_to_actual_objects(world):
    from unittest.mock import MagicMock
    import discord
    collection, _clock = install_drafts(world)
    role = MagicMock(spec=discord.Role)
    role.id, role.guild, role.name, role.managed = 70, world.guild, "cargo real", False
    world.guild.get_role = lambda identifier: role if identifier == 70 else None
    registry = await registry_for(world)
    registry.runtime.resources["r1"] = role
    result = await call(registry, "save_action_draft", action="assign_role", target_ref="m1",
                        reason="cargo solicitado", options={"role_ref": "r1"})
    assert result["ok"] and collection.docs[0]["options"]["role_ref"] == "70"
    resumed = await registry_for(world)
    alias = resumed.runtime.action_draft["options"]["role_ref"]
    assert resumed.runtime.resources[alias] is role and alias != "70"


@pytest.mark.asyncio
async def test_draft_cancel_only_clears_incomplete_context_not_existing_action_requests(world):
    collection, _clock = install_drafts(world)
    await world.cog._action_drafts.save(10, 30, 1, world.epoch, action="timeout_member", target_id=3)
    registry = await registry_for(world)
    world.cog._actions.cancel_pending = AsyncMock()
    result = await call(registry, "cancel_action_draft")
    assert result["data"] == {"cancelled": True, "executed_requests_cancelled": False}
    assert registry.runtime.action_draft is None and not collection.docs
    world.cog._actions.cancel_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_tool_context_observes_gateway_move_and_rebuilds_capabilities_without_effects(world):
    from unittest.mock import MagicMock
    import discord
    registry = await registry_for(world)
    assert registry.runtime.voice_state["same_channel"]
    other = MagicMock(spec=discord.VoiceChannel)
    other.id, other.guild, other.name = 44, world.guild, "nova call"
    other.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
    world.members[1].voice = SimpleNamespace(channel=other)
    state = await refresh_tool_context(registry)
    assert state["author"]["channel_id"] == "44" and not state["same_channel"]
    assert "speak_voice" not in registry.runtime.action_context.actions
    assert not world.collection.docs and not world.cog._supervisor.jobs
    world.tts.chatbot_join_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_operational_and_member_tools_share_current_voice_snapshot_shape(world):
    registry = await registry_for(world)
    world.voice.name = "call atual"
    state = (await call(registry, "get_operational_state"))["data"]["voice_state"]
    member = (await call(registry, "resolve_member", query="autor"))["data"]["members"][0]
    assert state["author"] == member["voice"]
    assert state["author"]["channel_name"] == "call atual" and state["can_listen"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [("save_action_draft", {"options": {"duration_seconds": 1620}}),
                                          ("cancel_action_draft", {})])
async def test_changed_draft_cannot_rebind_short_answer_or_cancel_to_another_target(world, tool, arguments):
    _collection, _clock = install_drafts(world)
    await world.cog._action_drafts.save(10, 30, 1, world.epoch, action="timeout_member", target_id=3)
    registry = await registry_for(world)
    world.members[4] = _member(world.guild, 4, rank=2)
    await world.cog._action_drafts.save(10, 30, 1, world.epoch, action="timeout_member", target_id=4)
    result = await call(registry, tool, **arguments)
    assert not result["ok"] and result["status"] == "failed"
    actual = await world.cog._action_drafts.get_current(10, 30, 1, world.epoch)
    assert actual["target_id"] == 4 and "duration_seconds" not in actual["options"]


@pytest.mark.asyncio
async def test_draft_can_preserve_bot_target_when_real_ban_policy_allows_it(world):
    _collection, _clock = install_drafts(world)
    world.members[3].bot = True
    registry = await registry_for(world)
    result = await call(registry, "save_action_draft", action="ban_member", target_ref="m1", reason="spam confirmado")
    assert result["ok"]
    resumed = await registry_for(world)
    assert resumed.runtime.targets[resumed.runtime.action_draft["target_ref"]].bot


@pytest.mark.asyncio
async def test_unban_draft_restores_public_account_identity_without_membership_or_ban_queries(world):
    from unittest.mock import MagicMock
    import discord
    collection, _clock = install_drafts(world)
    account = MagicMock(spec=discord.User)
    account.id, account.name, account.display_name = 77, "conta real", "conta real"
    world.cog.bot.fetch_user = AsyncMock(return_value=account)
    world.guild.fetch_ban = AsyncMock()
    world.message.content = "Desbane a conta <@77>"
    registry = await registry_for(world)
    saved = await call(registry, "save_action_draft", action="unban_member", target_ref="<@77>")
    assert saved["ok"] and collection.docs[0]["target_id"] == 77
    world.message.content = "motivo explicado"
    resumed = await registry_for(world)
    draft = resumed.runtime.action_draft
    assert draft["target_name"] == "conta real" and draft["target_id"] == 77
    assert resumed.runtime.resources[draft["target_ref"]] is account
    enums = resumed.get("propor_acao").parameters["properties"]["target_ref"]["enum"]
    assert draft["target_ref"] in enums and "77" in enums
    world.guild.fetch_ban.assert_not_awaited()
    assert 77 not in [item.args[0] for item in world.guild.fetch_member.await_args_list]
    assert not world.collection.docs and not world.cog._supervisor.jobs


@pytest.mark.asyncio
async def test_unban_draft_rejects_id_that_only_the_model_supplied(world):
    collection, _clock = install_drafts(world)
    world.cog.bot.fetch_user = AsyncMock()
    registry = await registry_for(world)
    result = await call(registry, "save_action_draft", action="unban_member", target_ref="77")
    assert not result["ok"] and not collection.docs
    world.cog.bot.fetch_user.assert_not_awaited()
