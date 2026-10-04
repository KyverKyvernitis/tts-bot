"""Todas as falas do chatbot confirmam um anexo e reutilizam seu áudio na call."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from cogs.chatbot import action_execution as execution
from cogs.chatbot.action_execution import ActionExecutionUncertain, execute_action
from cogs.chatbot.action_policy import ActionDenied
from cogs.chatbot.memory import MemoryEpoch
from test_chatbot_action_policy import doc, in_call, world
from test_chatbot_audio_mirror import _MirrorProbe


@pytest_asyncio.fixture
async def delivery(world):
    """Política real, fila real e player fake; nenhuma rede/provedor externo."""
    in_call(world, bot=True)
    probe = _MirrorProbe()
    probe.guild = world.guild
    probe.channel = world.voice
    probe.channel.members = [world.members[1]]
    probe.channel.name = "Call original"
    probe.member = world.members[1]
    probe.text_channel = world.chat
    probe.guild.get_member = world.members.get
    probe.other.guild = world.guild
    channels = {20: probe.channel, 21: probe.other, 30: probe.text_channel}
    probe.guild.get_channel = probe.guild.get_channel_or_thread = channels.get
    cog = world.bot.get_cog("Chatbot")
    world.bot.get_cog = {"Chatbot": cog, "TTSVoice": probe}.get
    world.bot.audio_router = None
    probe.bot = world.bot
    probe._connect_now(probe.channel)
    probe.chatbot_speak_voice = AsyncMock(side_effect=AssertionError("old call-only adapter must not run"))
    probe.chatbot_mirror_audio = AsyncMock(wraps=probe.chatbot_mirror_audio)
    world.tts = probe
    world.data = b"audio-original-semantics-preserved"
    world.chat_bytes = []
    probe.synthesize_chatbot_attachment.return_value = world.data

    async def send(**kwargs):
        assert "content" not in kwargs
        world.chat_bytes.append(kwargs["file"].fp.read())
        return SimpleNamespace(id=88, attachments=[SimpleNamespace(id=456, filename="resposta.mp3", size=len(world.data))])

    world.chat.send.side_effect = send
    try:
        yield world
    finally:
        state = probe.guild_states.get(world.guild.id)
        if state and state.worker_task:
            state.worker_task.cancel()
            await asyncio.gather(state.worker_task, return_exceptions=True)


async def drain(world):
    await asyncio.wait_for(world.tts._get_state(world.guild.id).queue.join(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_one_synthesis_one_chat_attachment_and_exact_same_bytes_in_actual_fifo(delivery, action):
    w = delivery
    result = await execute_action(w.bot, doc(action, voice=20), actor_id=1)
    await drain(w)
    assert result.public_result == ("Áudio enviado." if action == "send_audio" else "Áudio enviado no chat e enfileirado para a call.") and result.message_id == 88
    assert result.chat_audio_sent and result.voice_status == "enqueued"
    assert w.chat_bytes == [w.data] and w.tts.played_bytes == [w.data]
    assert w.guild.voice_client.play_calls == 1
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()
    w.chat.send.assert_awaited_once()
    w.tts.chatbot_mirror_audio.assert_awaited_once()
    assert w.tts.chatbot_mirror_audio.await_args.kwargs["audio"] is w.data
    assert w.tts.chatbot_mirror_audio.await_args.kwargs["expected_voice_channel_id"] == 20
    assert w.tts.chatbot_mirror_audio.await_args.kwargs["expected_session_ref"]
    w.tts.chatbot_speak_voice.assert_not_awaited()
    assert "conteúdo privado" not in str(result)


@pytest.mark.asyncio
async def test_speech_preserves_existing_voice_language_rate_and_pitch_for_single_synthesis(delivery):
    w = delivery
    w.bot.settings_db.resolve_tts.return_value = {
        "edge_voice": "pt-BR-AntonioNeural", "gtts_language": "pt-br", "edge_rate": "+18%", "edge_pitch": "-1Hz"}
    await execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1)
    await drain(w)
    kwargs = w.tts.synthesize_chatbot_attachment.await_args.kwargs
    assert {key: kwargs[key] for key in ("voice", "language", "rate", "pitch")} == {
        "voice": "pt-BR-AntonioNeural", "language": "pt-br", "rate": "+18%", "pitch": "-1Hz"}
    assert w.chat_bytes == w.tts.played_bytes == [w.data]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
@pytest.mark.parametrize("replace_session", [True, False])
async def test_changed_session_during_synthesis_never_plays_in_new_session(delivery, action, replace_session):
    w = delivery
    previous = w.guild.voice_client

    async def synthesis(**kwargs):
        if replace_session:
            w.tts._connect_now(w.voice)
        else:
            previous.session_id = "fresh-gateway-session"
        return w.data

    w.tts.synthesize_chatbot_attachment.side_effect = synthesis
    if action == "speak_voice":
        with pytest.raises(ActionDenied, match="sessão de voz mudou"):
            await execute_action(w.bot, doc(action, voice=20), actor_id=1)
        w.chat.send.assert_not_awaited()
    else:
        result = await execute_action(w.bot, doc(action, voice=20), actor_id=1)
        assert result.chat_audio_sent and result.voice_status == "skipped"
        assert w.chat_bytes == [w.data]
    await drain(w)
    assert previous.play_calls == 0 and w.guild.voice_client.play_calls == 0
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_changed_call_after_confirmed_chat_keeps_attachment_and_never_moves_voice(delivery, action):
    w = delivery
    original_sender = w.chat.send.side_effect

    async def send(**kwargs):
        sent = await original_sender(**kwargs)
        w.guild.voice_client.channel = w.tts.other
        w.guild.me.voice.channel = w.tts.other
        return sent

    w.chat.send.side_effect = send
    result = await execute_action(w.bot, doc(action, voice=20), actor_id=1)
    await drain(w)
    assert result.chat_audio_sent and result.voice_status == "skipped" and result.message_id == 88
    assert w.chat_bytes == [w.data] and w.tts.played_bytes == []
    assert w.guild.voice_client.move_calls == [] and w.guild.voice_client.play_calls == 0
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_call_created_during_synthesis_does_not_receive_attachment_copy(delivery):
    w = delivery
    w.guild.voice_client = None
    w.guild.me.voice.channel = None

    async def synthesis(**kwargs):
        w.tts._connect_now(w.voice)
        return w.data

    w.tts.synthesize_chatbot_attachment.side_effect = synthesis
    result = await execute_action(w.bot, doc("send_audio"), actor_id=1)
    assert result.chat_audio_sent and result.voice_status == "skipped"
    assert w.chat_bytes == [w.data] and w.guild.voice_client.play_calls == 0
    w.tts.chatbot_mirror_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_audience_without_chat_access_receives_no_copy_of_spoken_audio(delivery):
    w = delivery
    w.voice.members = [w.members[1], w.members[3]]
    w.chat.permissions_for.side_effect = lambda member: SimpleNamespace(
        view_channel=member.id != 3, send_messages=True, attach_files=True)
    result = await execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1)
    await drain(w)
    assert result.chat_audio_sent and result.voice_status == "skipped"
    assert w.chat_bytes == [w.data] and w.tts.played_bytes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
@pytest.mark.parametrize("revocation", ["voice", "memory", "text_preference"])
async def test_copy_queued_before_revocation_is_discarded_without_losing_chat(delivery, action, revocation):
    w = delivery
    epoch = MemoryEpoch(1, 2, 3)
    cog = w.bot.get_cog("Chatbot")
    cog._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=epoch))
    cog.get_conversation_preferences = AsyncMock(return_value=SimpleNamespace(mode="auto", voice="", language=""))
    request = doc(action, voice=20)
    request["memory_epoch"] = {"global_generation": 1, "guild_generation": 2, "user_generation": 3}
    lock = w.tts._get_tts_playback_lock(w.guild.id)
    await lock.acquire()
    try:
        result = await execute_action(w.bot, request, actor_id=1)
        assert result.chat_audio_sent and result.voice_status == "enqueued"
        if revocation == "voice":
            w.config.voice_actions_enabled = False
        elif revocation == "memory":
            cog._memory.capture_epoch.return_value = MemoryEpoch(1, 2, 4)
        else:
            cog.get_conversation_preferences.return_value = SimpleNamespace(mode="text", voice="", language="")
    finally:
        lock.release()
    await drain(w)
    assert w.chat_bytes == [w.data] and w.guild.voice_client.play_calls == 0
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()
    w.chat.send.assert_awaited_once()
    w.tts._notify_tts_failure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["voice_actions_enabled", "audio_actions_enabled"])
async def test_speech_requires_both_audio_and_voice_configuration_before_synthesis(delivery, flag):
    w = delivery
    setattr(w.config, flag, False)
    with pytest.raises(ActionDenied):
        await execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1)
    w.tts.synthesize_chatbot_attachment.assert_not_awaited()
    w.chat.send.assert_not_awaited()
    w.tts.chatbot_mirror_audio.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["attach_files", "session_getter"])
async def test_speech_missing_chat_permissions_or_real_session_does_not_synthesize(delivery, missing):
    w = delivery
    if missing == "attach_files":
        w.chat.permissions_for.return_value.attach_files = False
    else:
        w.tts.chatbot_voice_session_ref = None
    with pytest.raises(ActionDenied):
        await execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1)
    w.tts.synthesize_chatbot_attachment.assert_not_awaited()
    w.chat.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
@pytest.mark.parametrize("failure", ["timeout", "http500", "http400", "connection"])
async def test_failed_or_unconfirmed_chat_upload_never_starts_voice_or_resends(delivery, action, failure):
    w = delivery
    failures = {"timeout": asyncio.TimeoutError(), "connection": ConnectionError("lost connection"),
        "http500": discord.HTTPException(SimpleNamespace(status=500, reason="error"), "upload not confirmed"),
        "http400": discord.HTTPException(SimpleNamespace(status=400, reason="bad request"), "upload rejected")}
    w.chat.send.side_effect = failures[failure]
    expected = ActionDenied if failure == "http400" else ActionExecutionUncertain
    with pytest.raises(expected):
        await execute_action(w.bot, doc(action, voice=20), actor_id=1)
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()
    w.chat.send.assert_awaited_once()
    w.tts.chatbot_mirror_audio.assert_not_awaited()
    assert w.guild.voice_client.play_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "cancelled"])
async def test_post_delivery_metadata_failure_never_invalidates_confirmed_attachment(delivery, monkeypatch, failure):
    w = delivery
    cog = w.bot.get_cog("Chatbot")
    metadata_entered = asyncio.Event()

    async def record(**kwargs):
        metadata_entered.set()
        if failure == "exception":
            raise RuntimeError("private metadata failure")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        await asyncio.Future()

    cog._reply_store = SimpleNamespace(record_sent=AsyncMock(side_effect=record))
    monkeypatch.setattr(execution, "_AUDIO_METADATA_TIMEOUT_SECONDS", .01)
    result = await execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1)
    assert metadata_entered.is_set() and result.chat_audio_sent and result.message_id == 88
    await drain(w)
    assert w.chat_bytes == [w.data]
    assert result.voice_status == ("skipped" if failure == "cancelled" else "enqueued")
    w.chat.send.assert_awaited_once()
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "cancelled", "unknown_result"])
async def test_post_delivery_queue_uncertainty_keeps_chat_receipt_and_never_retries(delivery, monkeypatch, failure):
    w = delivery
    entered = asyncio.Event()

    async def mirror(**kwargs):
        entered.set()
        if failure == "exception":
            raise RuntimeError("queue result lost")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        if failure == "unknown_result":
            return None
        await asyncio.Future()

    w.tts.chatbot_mirror_audio.side_effect = mirror
    monkeypatch.setattr(execution, "_AUDIO_MIRROR_ADMISSION_TIMEOUT_SECONDS", .01)
    result = await execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1)
    assert entered.is_set() and result.chat_audio_sent and result.message_id == 88
    assert result.voice_status == "uncertain" and result.public_result.startswith("Áudio enviado no chat;")
    assert w.chat_bytes == [w.data] and w.tts.played_bytes == []
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()
    w.chat.send.assert_awaited_once()
    w.tts.chatbot_mirror_audio.assert_awaited_once()


@pytest.mark.asyncio
async def test_external_cancel_after_chat_returns_confirmed_receipt_without_retry(delivery):
    w = delivery
    entered = asyncio.Event()

    async def mirror(**kwargs):
        entered.set()
        await asyncio.Future()

    w.tts.chatbot_mirror_audio.side_effect = mirror
    task = asyncio.create_task(execute_action(w.bot, doc("speak_voice", voice=20), actor_id=1))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    result = await task
    assert result.chat_audio_sent and result.message_id == 88 and result.voice_status == "uncertain"
    assert w.chat_bytes == [w.data]
    w.chat.send.assert_awaited_once()
    w.tts.synthesize_chatbot_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_change_while_admission_guard_waits_never_enqueues(delivery):
    w = delivery
    token = w.tts.chatbot_voice_session_ref(w.guild.id, require_idle=False)

    async def guard():
        w.tts._connect_now(w.voice)

    result = await w.tts.chatbot_mirror_audio(
        guild_id=10, user_id=1, text_channel_id=30, audio=w.data, request_id="guarded-copy",
        expected_voice_channel_id=20, expected_session_ref=token, before_effect=guard)
    await drain(w)
    assert result == {"ok": False, "status": "skipped"}
    assert w.tts.played_bytes == [] and w.guild.voice_client.play_calls == 0
    w.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("host_override", [False, True])
async def test_saved_text_only_allows_audio_with_trusted_temporary_host_override(delivery, host_override):
    from cogs.chatbot.preferences import ConversationPreferences
    w = delivery
    cog = w.bot.get_cog("Chatbot")
    epoch = MemoryEpoch(1, 2, 3)
    saved = ConversationPreferences(mode="text", voice="pt-BR-AntonioNeural", language="pt-br")
    cog._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=epoch))
    cog.get_conversation_preferences = AsyncMock(return_value=saved)
    w.bot.settings_db.resolve_tts.return_value = {"edge_voice": "baseVoice", "gtts_language": "en", "edge_rate": "+5%", "edge_pitch": "+2Hz"}
    request = doc("speak_voice", voice=20)
    request["memory_epoch"] = {"global_generation": 1, "guild_generation": 2, "user_generation": 3}
    # A model/payload field is not the host's persisted per-turn selection.
    request["payload"]["response_format"] = "audio"
    if host_override:
        request["response_format"] = "audio"
        result = await execute_action(w.bot, request, actor_id=1)
        await drain(w)
        assert result.chat_audio_sent and result.voice_status == "enqueued"
        kwargs = w.tts.synthesize_chatbot_attachment.await_args.kwargs
        assert kwargs["voice"] == saved.voice and kwargs["language"] == saved.language
        assert kwargs["rate"] == "+5%" and kwargs["pitch"] == "+2Hz"
        assert w.chat_bytes == w.tts.played_bytes == [w.data]
    else:
        with pytest.raises(ActionDenied, match="respostas em texto"):
            await execute_action(w.bot, request, actor_id=1)
        w.tts.synthesize_chatbot_attachment.assert_not_awaited()
        w.chat.send.assert_not_awaited()
        w.tts.chatbot_mirror_audio.assert_not_awaited()
    assert saved.mode == "text" and saved.voice == "pt-BR-AntonioNeural" and saved.language == "pt-br"
