"""Formato decidido pelo host, arquivo sem prévia e mesma síntese na call."""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.action_protocol import ActionProposal, ChatReply
from cogs.chatbot.audio_format import AudioReplySelector
from cogs.chatbot.preferences import ConversationPreferences
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.config import ConfigStore, GuildChatbotConfig
from cogs.chatbot.memory import MemoryEpoch


def config(**changes):
    return GuildChatbotConfig(guild_id=10, enabled=True, **changes)


def test_host_draws_once_and_preserves_probability_boundary():
    draw = Mock(side_effect=[0.199, 0.2])
    selector = AudioReplySelector(draw=draw, clock=lambda: 100)
    arguments = dict(config=config(), guild_id=10, channel_id=20, content="oi", reply="E aí!")
    assert selector.select(**arguments) == "random"
    assert draw.call_count == 1
    assert selector.select(**arguments) == "text"
    assert draw.call_count == 2


@pytest.mark.parametrize("setting", ["enabled", "actions_enabled", "audio_actions_enabled"])
def test_audio_disabled_does_not_draw_or_accept_explicit_audio(setting):
    draw = Mock(return_value=0)
    selector = AudioReplySelector(draw=draw)
    cfg = replace(config(), **{setting: False})
    assert selector.select(config=cfg, guild_id=10, channel_id=20,
                           requested=True, reply="Oi!") == "text"
    draw.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"eligible": False}, {"reply": ""}, {"reply": "```python\nprint(1)\n```"},
    {"reply": "x" * 801}, {"mode": "text"},
    {"config": config(audio_reply_chance_percent=0)},
])
def test_ineligible_operational_long_code_or_text_preference_never_draws(changes):
    draw = Mock(return_value=0)
    selector = AudioReplySelector(draw=draw)
    arguments = dict(config=config(), guild_id=10, channel_id=20, content="oi", reply="Olá!")
    assert selector.select(**{**arguments, **changes}) == "text"
    draw.assert_not_called()


def test_explicit_audio_ignores_random_chance_and_cooldown_without_draw():
    draw = Mock(return_value=0)
    selector = AudioReplySelector(draw=draw, clock=lambda: 100)
    selector.record_sent(guild_id=10, channel_id=20, cooldown_seconds=60)
    assert selector.select(config=config(audio_reply_chance_percent=0), guild_id=10,
                           channel_id=20, requested=True, reply="Oi!") == "requested"
    draw.assert_not_called()


def test_random_cooldown_is_per_channel_and_only_after_actual_send():
    now = [100]
    draw = Mock(return_value=0)
    selector = AudioReplySelector(draw=draw, clock=lambda: now[0])
    arguments = dict(config=config(), guild_id=10, channel_id=20, content="oi", reply="Olá!")
    assert selector.select(**arguments) == "random"
    # Uma síntese que falha não reserva o intervalo.
    assert selector.select(**arguments) == "random"
    selector.record_sent(guild_id=10, channel_id=20, cooldown_seconds=60)
    assert selector.select(**arguments) == "text"
    assert selector.select(**{**arguments, "channel_id": 21}) == "random"
    assert draw.call_count == 3
    now[0] = 160
    assert selector.select(**arguments) == "random"
    selector.cleanup()
    assert not selector._cooldowns


@pytest.mark.parametrize("text", ["Só texto", "responda em áudio", "A partir de agora só converse em áudio"])
def test_free_text_does_not_act_as_an_operational_command(text):
    draw = Mock(return_value=0.8)
    selector = AudioReplySelector(draw=draw)
    assert selector.select(config=config(), guild_id=10, channel_id=20,
                           content=text, reply="Oi!") == "text"
    draw.assert_called_once()


def test_saved_audio_mode_ignores_random_cooldown_between_turns():
    draw = Mock(return_value=0.99)
    selector = AudioReplySelector(draw=draw, clock=lambda: 100)
    selector.record_sent(guild_id=10, channel_id=20, cooldown_seconds=60)
    assert selector.select(config=config(audio_reply_chance_percent=0), guild_id=10,
                           channel_id=20, mode="audio", reply="Como foi o seu dia?") == "requested"
    draw.assert_not_called()


class Collection:
    def __init__(self, initial):
        self.doc = initial.to_doc()
        self.before_write = None

    async def find_one(self, query):
        return copy.deepcopy(self.doc)

    async def update_one(self, query, update, *, upsert):
        if self.before_write:
            self.before_write()
        self.doc.update(copy.deepcopy(update["$set"]))


def test_defaults_survive_old_docs_and_zero_roundtrips():
    for doc in (None, {"enabled": True}, {"audio_reply_chance_percent": None,
                                         "audio_reply_cooldown_seconds": "bad"}):
        cfg = GuildChatbotConfig.from_doc(doc, guild_id=10)
        assert (cfg.audio_reply_chance_percent, cfg.audio_reply_cooldown_seconds) == (20, 60)
    cfg = config(audio_reply_chance_percent=0, audio_reply_cooldown_seconds=0)
    assert GuildChatbotConfig.from_doc(cfg.to_doc(), guild_id=10) == cfg


@pytest.mark.asyncio
async def test_audio_panel_save_is_partial_and_survives_restart():
    collection = Collection(config(channel_ids=(20,), audio_reply_chance_percent=5))
    store = ConfigStore(collection)
    await store.get_config(10)
    collection.before_write = lambda: collection.doc.update(actions_enabled=False, channel_ids=[21])
    saved = await store.save_audio_config(guild_id=10, audio_reply_chance_percent=40,
                                        audio_reply_cooldown_seconds=120, updated_by=2)
    assert not saved.actions_enabled and saved.channel_ids == (21,)
    assert saved.audio_reply_chance_percent == 40 and saved.audio_reply_cooldown_seconds == 120
    assert await ConfigStore(collection).get_config(10) == saved


@pytest.mark.asyncio
async def test_conversation_save_does_not_overwrite_audio_settings_from_another_panel():
    collection = Collection(config(audio_reply_chance_percent=5))
    store = ConfigStore(collection)
    collection.before_write = lambda: collection.doc.update(
        audio_reply_chance_percent=35, audio_reply_cooldown_seconds=90,
    )
    await store.save_config(guild_id=10, enabled=True, channel_ids=(20,),
                            spontaneous_enabled=False, spontaneous_channel_ids=(),
                            spontaneous_chance_percent=5, updated_by=2)
    fresh = await store.get_config(10, fresh=True)
    assert (fresh.audio_reply_chance_percent, fresh.audio_reply_cooldown_seconds) == (35, 90)


@pytest.mark.asyncio
@pytest.mark.parametrize("chance,cooldown", [(-1, 60), (101, 60), (20, -1), (20, 3601)])
async def test_audio_panel_rejects_bad_range_without_write(chance, cooldown):
    collection = Collection(config())
    before = copy.deepcopy(collection.doc)
    with pytest.raises(ValueError):
        await ConfigStore(collection).save_audio_config(
            guild_id=10, audio_reply_chance_percent=chance,
            audio_reply_cooldown_seconds=cooldown, updated_by=2,
        )
    assert collection.doc == before


@pytest.fixture
def turn(monkeypatch):
    monkeypatch.setattr(C, "SAFE_MODE", False)
    cfg = config(audio_reply_chance_percent=100)
    epoch = MemoryEpoch(1, 2, 3)
    tts = SimpleNamespace(
        synthesize_chatbot_attachment=AsyncMock(return_value=b"the exact mp3 bytes"),
        chatbot_mirror_audio=AsyncMock(return_value={"ok": True, "status": "enqueued"}),
    )
    cog = object.__new__(ChatbotCog)
    cog.bot = SimpleNamespace(
        user=SimpleNamespace(id=999), get_cog=lambda name: tts if name == "TTSVoice" else None,
        settings_db=SimpleNamespace(resolve_tts=AsyncMock(return_value={})),
    )
    cog._session, cog._master, cog._actions = object(), None, None
    cog._config = SimpleNamespace(get_config=AsyncMock(return_value=cfg))
    cog._memory = SimpleNamespace(
        load_context=AsyncMock(return_value=(epoch, [], [])),
        capture_epoch=AsyncMock(return_value=epoch), append_turn=AsyncMock(),
    )
    cog.get_conversation_preferences = AsyncMock(return_value=ConversationPreferences())
    cog._router = SimpleNamespace(chat=AsyncMock(return_value="E aí, bora jogar?"))
    cog._message_index = SimpleNamespace(remember=AsyncMock())
    cog._can_respond = AsyncMock(return_value=True)
    cog._add_processing_reaction = AsyncMock(return_value=None)
    cog._remove_processing_reaction = AsyncMock()
    draw = Mock(return_value=0)
    cog._audio_reply_selector = AudioReplySelector(draw=draw, clock=lambda: 100)
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.nsfw = 20, False
    message = SimpleNamespace(
        id=30, guild=SimpleNamespace(id=10), channel=channel,
        author=SimpleNamespace(id=40, name="ana", display_name="Ana", voice=None),
        reference=None, attachments=[], reply=AsyncMock(return_value=SimpleNamespace(id=50)),
    )
    yield SimpleNamespace(cog=cog, message=message, tts=tts, config=cfg, draw=draw, epoch=epoch)
    for call in message.reply.await_args_list:
        files = call.kwargs.get("files")
        if isinstance(files, list):
            for file in files:
                file.close()


@pytest.mark.asyncio
async def test_host_forces_audio_once_and_mirrors_same_bytes_without_author_in_call(turn):
    assert await turn.cog._generate_and_send(turn.message, "oi")
    assert turn.draw.call_count == 1
    turn.tts.synthesize_chatbot_attachment.assert_awaited_once()
    assert turn.message.reply.await_args.args == (None,)
    file = turn.message.reply.await_args.kwargs["files"][0]
    assert file.fp.read() == b"the exact mp3 bytes"
    turn.tts.chatbot_mirror_audio.assert_awaited_once()
    arguments = turn.tts.chatbot_mirror_audio.await_args.kwargs
    assert arguments["audio"] == b"the exact mp3 bytes"
    assert (arguments["guild_id"], arguments["user_id"], arguments["text_channel_id"],
            arguments["request_id"]) == (10, 40, 20, "reply-50")
    assert await arguments["before_effect"]() is None
    assert turn.cog._memory.append_turn.await_args.kwargs["assistant_message"] == "E aí, bora jogar?"
    assert await turn.cog._generate_and_send(turn.message, "e depois?")
    assert turn.draw.call_count == 1 and turn.tts.synthesize_chatbot_attachment.await_count == 1
    assert turn.message.reply.await_args.args == ("E aí, bora jogar?",)


@pytest.mark.asyncio
async def test_spontaneous_response_can_be_audio_without_extra_message(turn):
    assert await turn.cog._generate_and_send(turn.message, "bora jogar?", behavior_hint="Participe brevemente.")
    turn.message.reply.assert_awaited_once()
    assert turn.message.reply.await_args.args == (None,)
    turn.tts.synthesize_chatbot_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_synthesis_failure_delivers_text_and_does_not_reserve_cooldown(turn):
    turn.tts.synthesize_chatbot_attachment.side_effect = RuntimeError("upstream failed")
    assert await turn.cog._generate_and_send(turn.message, "oi")
    assert turn.message.reply.await_args.args == ("E aí, bora jogar?",)
    turn.tts.chatbot_mirror_audio.assert_not_awaited()
    assert not turn.cog._audio_reply_selector._cooldowns
    assert turn.cog._memory.append_turn.await_args.kwargs["assistant_message"] == "E aí, bora jogar?"


@pytest.mark.asyncio
async def test_random_audio_without_attach_permission_stays_text_without_synthesis(turn):
    turn.message.guild.me = SimpleNamespace(id=999)
    turn.message.channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, send_messages=True, attach_files=False,
    )
    assert await turn.cog._generate_and_send(turn.message, "oi")
    turn.draw.assert_not_called()
    turn.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert turn.message.reply.await_args.args == ("E aí, bora jogar?",)


@pytest.mark.asyncio
async def test_mirror_failure_never_repeats_synthesis_or_loses_delivered_memory(turn):
    turn.cog.get_conversation_preferences.return_value = ConversationPreferences(mode="audio")
    turn.tts.chatbot_mirror_audio.side_effect = RuntimeError("call ended")
    assert await turn.cog._generate_and_send(turn.message, "manda um áudio")
    turn.tts.synthesize_chatbot_attachment.assert_awaited_once()
    turn.message.reply.assert_awaited_once()
    turn.cog._memory.append_turn.assert_awaited_once()
    turn.draw.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reset", "voice_disabled", "audio_disabled", "channel_changed"])
async def test_queued_mirror_guard_rejects_reset_or_revocation(turn, change):
    assert await turn.cog._generate_and_send(turn.message, "manda um áudio")
    guard = turn.tts.chatbot_mirror_audio.await_args.kwargs["before_effect"]
    if change == "reset":
        turn.cog._memory.capture_epoch.return_value = MemoryEpoch(99, 99, 99)
    else:
        changes = {"voice_disabled": {"voice_actions_enabled": False},
                   "audio_disabled": {"audio_actions_enabled": False},
                   "channel_changed": {"channel_ids": (21,)}}[change]
        turn.cog._config.get_config.return_value = replace(turn.config, **changes)
    with pytest.raises(ValueError):
        await guard()
    assert turn.message.reply.await_count == 1


@pytest.mark.asyncio
async def test_no_mirror_or_memory_before_failed_chat_send(turn):
    turn.message.reply.side_effect = RuntimeError("Discord failed")
    with pytest.raises(RuntimeError):
        await turn.cog._generate_and_send(turn.message, "manda um áudio")
    turn.tts.chatbot_mirror_audio.assert_not_awaited()
    turn.cog._memory.append_turn.assert_not_awaited()
    assert not turn.cog._audio_reply_selector._cooldowns


@pytest.mark.asyncio
async def test_confirmed_native_audio_also_limits_following_random_format(turn):
    await turn.cog.record_audio_reply_sent(guild_id=10, channel_id=20)
    assert await turn.cog._generate_and_send(turn.message, "oi")
    turn.tts.synthesize_chatbot_attachment.assert_not_awaited()
    turn.draw.assert_not_called()
    assert turn.message.reply.await_args.args == ("E aí, bora jogar?",)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_explicit_text_preference_overrides_even_a_native_audio_proposal(turn, action):
    turn.cog.get_conversation_preferences.return_value = ConversationPreferences(mode="text")
    service = turn.cog._actions = SimpleNamespace(
        ready=True, describe=AsyncMock(return_value=SimpleNamespace(
            description="Pode mandar áudio.", actions=(action,), targets={},
        )), plan=AsyncMock(),
    )
    turn.cog._router.chat.return_value = ChatReply("", (ActionProposal(action, text="Bora jogar!"),))
    assert await turn.cog._generate_and_send(turn.message, "só texto, por favor")
    service.plan.assert_not_awaited()
    turn.draw.assert_not_called()
    turn.tts.synthesize_chatbot_attachment.assert_not_awaited()
    turn.tts.chatbot_mirror_audio.assert_not_awaited()
    assert turn.message.reply.await_args.args == ("Bora jogar!",)
    assert turn.cog._memory.append_turn.await_args.kwargs["assistant_message"] == "Bora jogar!"


@pytest.mark.asyncio
async def test_text_preference_preserves_separate_privileged_permission_step(turn):
    turn.cog.get_conversation_preferences.return_value = ConversationPreferences(mode="text")
    plan = SimpleNamespace(requests=[{"action": "join_voice"}])
    service = turn.cog._actions = SimpleNamespace(
        ready=True, describe=AsyncMock(return_value=SimpleNamespace(
            description="Pode pedir entrada e falar depois.", actions=("join_voice", "speak_voice"),
            targets={"autor": object()},
        )), plan=AsyncMock(return_value=plan), content=lambda _plan: "", bind_and_start=AsyncMock(),
    )
    turn.cog._router.chat.return_value = ChatReply("", (
        ActionProposal("join_voice", "autor"), ActionProposal("speak_voice", text="Bora jogar!"),
    ))
    assert await turn.cog._generate_and_send(turn.message, "entre na call, mas só texto agora")
    filtered = service.plan.await_args.args[1]
    assert filtered.proposals == (ActionProposal("join_voice", "autor"),)
    service.bind_and_start.assert_awaited_once_with(plan, None)
    turn.message.reply.assert_not_awaited()
    turn.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_configure_audio_button_is_staff_guarded_and_no_env_needed():
    from cogs.chatbot.views import AudioReplyConfigModal, EditConfigView
    authorized, save = AsyncMock(return_value=True), AsyncMock()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=40), response=SimpleNamespace(
            is_done=Mock(return_value=False), send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock(),
        ), followup=SimpleNamespace(send=AsyncMock()),
    )
    view = EditConfigView(requester_id=40, current_config=config(), on_submit_config=AsyncMock(),
                          on_submit_audio=save, check_authorized=authorized)
    button = next(item for item in view.children if item.label == "Configurar áudios")
    await button.callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, AudioReplyConfigModal)
    assert modal.chance_input.default == "20" and modal.cooldown_input.default == "60"
    modal.chance_input._value, modal.cooldown_input._value = "35", "90"
    await modal.on_submit(interaction)
    submitted = save.await_args.args[1]
    assert (submitted.audio_reply_chance_percent, submitted.audio_reply_cooldown_seconds) == (35, 90)
    assert authorized.await_count == 2
