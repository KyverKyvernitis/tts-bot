"""O anexo já entregue é copiado para a fila atual, sem nova síntese/conexão."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from cogs.chatbot.action_execution import execute_action
from cogs.chatbot.memory import MemoryEpoch
from cogs.tts import audio as tts_audio
from cogs.tts.audio import QueueItem, TTSAudioMixin
from test_chatbot_action_policy import doc, world
from test_chatbot_voice_actions import _Probe
from test_tts_chatbot_join_integration import _CodecVoice, _make_codec_fixture


class _MirrorProbe(_Probe):
    def __init__(self, *, connected=True):
        super().__init__(connected=connected)
        self.member.bot = False
        self.channel.members = [self.member]
        self.text_channel = Mock(spec=discord.TextChannel)
        self.text_channel.id = 30
        self.text_channel.permissions_for.return_value = SimpleNamespace(view_channel=True)
        self.guild.get_channel = lambda ident: {20: self.channel, 21: self.other, 30: self.text_channel}.get(ident)
        self._try_worker_voice_direct_tts = AsyncMock(return_value=None)
        self._try_get_cached_path = Mock(return_value=None)
        self._schedule_cache_maintenance = Mock()
        self._record_queue_timing = Mock()
        self._schedule_worker_voice_agent_register_session = Mock()
        self._notify_tts_failure = AsyncMock()
        self.played_bytes = []

    def _make_discord_tts_source(self, path):
        self.played_bytes.append(Path(path).read_bytes())
        return super()._make_discord_tts_source(path)

    async def mirror(self, audio=b"exact-mp3", request_id="mirror-1", **kwargs):
        return await self.chatbot_mirror_audio(guild_id=1, user_id=2, text_channel_id=30,
                                             audio=audio, request_id=request_id, **kwargs)

    async def drain(self):
        await asyncio.wait_for(self._get_state(1).queue.join(), timeout=3)

    async def close(self):
        state = self.guild_states.get(1)
        if state and state.worker_task:
            state.worker_task.cancel()
            await asyncio.gather(state.worker_task, return_exceptions=True)


class ChatbotAudioMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if hasattr(self, "probe"):
            await self.probe.close()

    async def test_same_bytes_play_in_current_call_without_author_in_that_call(self):
        self.probe = probe = _MirrorProbe()
        probe.member.voice.channel = None
        guard = AsyncMock()
        result = await probe.mirror(before_effect=guard)
        self.assertEqual(result, {"ok": True, "status": "enqueued"})
        await probe.drain()
        self.assertEqual(probe.played_bytes, [b"exact-mp3"])
        self.assertEqual(probe.guild.voice_client.play_calls, 1)
        self.assertEqual(probe.guild.voice_client.move_calls, [])
        probe.synthesize_chatbot_attachment.assert_not_awaited()
        probe._try_worker_voice_direct_tts.assert_not_awaited()
        probe.channel.connect.assert_not_awaited()
        probe._notify_tts_failure.assert_not_awaited()
        self.assertEqual(guard.await_count, 2)
        self.assertFalse(Path(probe.playback_path).exists())

    async def test_fifo_with_normal_tts_and_prefetched_mirror_never_synthesizes_copy(self):
        self.probe = probe = _MirrorProbe()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normal.mp3"
            path.write_bytes(b"normal-audio")
            probe._resolve_audio_path = AsyncMock(return_value=(str(path), False))
            normal = QueueItem(1, 20, 2, "Normal", "edge", "pt-BR-FranciscaNeural", "pt-br", "+0%", "+0Hz")
            await probe._enqueue_tts_item(1, normal)
            await probe.mirror(audio=b"first-copy", request_id="first")
            await probe.mirror(audio=b"second-copy", request_id="second")
            with patch.object(tts_audio, "TTS_FFMPEG_PRIME_ENABLED", False):
                await probe.drain()
            self.assertEqual(probe.played_bytes, [b"normal-audio", b"first-copy", b"second-copy"])
            probe._resolve_audio_path.assert_awaited_once()
            self.assertIs(probe._resolve_audio_path.await_args.args[1], normal)
            probe.synthesize_chatbot_attachment.assert_not_awaited()

    async def test_full_queue_does_not_evict_existing_speech(self):
        self.probe = probe = _MirrorProbe()
        state = probe._get_state(1)
        state.queue = asyncio.Queue(maxsize=1)
        normal = QueueItem(1, 20, 2, "Normal", "edge", "pt-BR-FranciscaNeural", "pt-br", "+0%", "+0Hz")
        state.queue.put_nowait(normal)
        self.assertEqual((await probe.mirror())["status"], "skipped")
        self.assertIs(state.queue.get_nowait(), normal)
        state.queue.task_done()
        self.assertIsNone(state.worker_task)

    async def test_disconnect_or_same_channel_new_session_discards_without_reentry_or_notice(self):
        for changed in ("disconnect", "replace", "move"):
            with self.subTest(changed=changed):
                self.probe = probe = _MirrorProbe()
                voice = probe.guild.voice_client
                await probe.mirror()
                if changed == "disconnect":
                    voice.connected = False
                    probe.guild.me.voice.channel = None
                elif changed == "replace":
                    probe._connect_now(probe.channel)
                else:
                    voice.channel = probe.other
                    probe.guild.me.voice.channel = probe.other
                await probe.drain()
                self.assertEqual(voice.play_calls, 0)
                self.assertEqual(probe.guild.voice_client.play_calls, 0)
                probe.channel.connect.assert_not_awaited()
                probe._notify_tts_failure.assert_not_awaited()
                self.assertEqual(probe.played_bytes, [])
                await probe.close()

    async def test_music_or_permissions_changed_before_playback_omit_copy(self):
        for changed in ("music", "muted", "permissions", "audience"):
            with self.subTest(changed=changed):
                self.probe = probe = _MirrorProbe()
                await probe.mirror()
                if changed == "music":
                    probe.music = True
                elif changed == "muted":
                    probe.guild.me.voice.mute = True
                elif changed == "permissions":
                    probe.channel.permissions_for.return_value.speak = False
                else:
                    probe.text_channel.permissions_for.side_effect = lambda member: SimpleNamespace(view_channel=member is not probe.member)
                await probe.drain()
                self.assertEqual(probe.guild.voice_client.play_calls, 0)
                probe._try_worker_voice_direct_tts.assert_not_awaited()
                probe._notify_tts_failure.assert_not_awaited()
                await probe.close()

    async def test_private_thread_or_inaccessible_listener_omits_copy_at_admission(self):
        self.probe = probe = _MirrorProbe()
        listener = SimpleNamespace(id=3, bot=False)
        probe.channel.members.append(listener)
        probe.text_channel.permissions_for.side_effect = lambda member: SimpleNamespace(view_channel=member is not listener)
        self.assertFalse((await probe.mirror())["ok"])
        private = Mock(spec=discord.Thread)
        private.is_private.return_value = True
        probe.guild.get_channel = lambda ident: private if ident == 30 else probe.channel
        self.assertFalse((await probe.mirror())["ok"])
        self.assertEqual(probe.guild.voice_client.play_calls, 0)

    async def test_guard_reset_while_waiting_for_playback_never_plays_or_retries(self):
        self.probe = probe = _MirrorProbe()
        guard = AsyncMock(side_effect=[None, ValueError("context changed")])
        await probe.mirror(before_effect=guard)
        await probe.drain()
        self.assertEqual(probe.guild.voice_client.play_calls, 0)
        probe._notify_tts_failure.assert_not_awaited()
        probe.synthesize_chatbot_attachment.assert_not_awaited()

    async def test_boolean_false_guard_is_respected_at_both_boundaries(self):
        self.probe = probe = _MirrorProbe()
        self.assertFalse((await probe.mirror(before_effect=AsyncMock(return_value=False)))["ok"])
        await probe.mirror(request_id="second", before_effect=AsyncMock(side_effect=[None, False]))
        await probe.drain()
        self.assertEqual(probe.guild.voice_client.play_calls, 0)

    async def test_concurrent_same_request_is_played_once_even_after_first_dequeues(self):
        self.probe = probe = _MirrorProbe()
        arrived, release = asyncio.Event(), asyncio.Event()

        async def slow_guard():
            arrived.set()
            await release.wait()

        second = asyncio.create_task(probe.mirror(before_effect=slow_guard))
        await arrived.wait()
        self.assertTrue((await probe.mirror())["ok"])
        await probe.drain()
        release.set()
        self.assertFalse((await second)["ok"])
        self.assertFalse((await probe.mirror())["ok"])
        self.assertEqual(probe.guild.voice_client.play_calls, 1)

    async def test_mirror_waits_for_canonical_playback_lock(self):
        self.probe = probe = _MirrorProbe()
        lock = probe._get_tts_playback_lock(1)
        await lock.acquire()
        await probe.mirror()
        await asyncio.sleep(.02)
        self.assertEqual(probe.guild.voice_client.play_calls, 0)
        lock.release()
        await probe.drain()
        self.assertEqual(probe.guild.voice_client.play_calls, 1)

    async def test_audio_memory_admission_is_bounded_globally(self):
        self.probe = probe = _MirrorProbe()
        probe._ensure_worker = Mock()
        audio = b"a" * (8 * 1024 * 1024)
        self.assertTrue((await probe.mirror(audio=audio, request_id="first"))["ok"])
        self.assertTrue((await probe.mirror(audio=audio, request_id="second"))["ok"])
        self.assertFalse((await probe.mirror(audio=b"another", request_id="third"))["ok"])

    def test_timeout_uses_existing_ceiling_without_private_transcript(self):
        item = QueueItem(1, 20, 2, "Áudio do chatbot", "chatbot_audio", "", "pt-br", "+0%", "+0Hz", chatbot_is_mirror=True)
        self.assertEqual(TTSAudioMixin._estimate_playback_timeout(_MirrorProbe(), item),
                         tts_audio.TTS_PLAYBACK_TIMEOUT_MAX_SECONDS)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg não instalado")
    async def test_real_ffmpeg_and_discord_player_use_same_mp3_without_provider(self):
        self.probe = probe = _MirrorProbe()
        with tempfile.TemporaryDirectory() as directory:
            _, path = _make_codec_fixture(directory)
            data = path.read_bytes()
            voice = _CodecVoice(probe.guild, probe.channel)
            probe.guild.voice_client = voice
            probe.member.voice.channel = None
            probe._make_discord_tts_source = TTSAudioMixin._make_discord_tts_source.__get__(probe)
            probe._path_audio_format = TTSAudioMixin._path_audio_format.__get__(probe)
            await probe.mirror(audio=data)
            await probe.drain()
            await voice.finish_thread()
            self.assertEqual(voice.play_calls, 1)
            self.assertTrue(voice.sent_packets)
            probe.synthesize_chatbot_attachment.assert_not_awaited()
            probe.channel.connect.assert_not_awaited()
            # Outros testes simulam a ausência de libopus. Libere este encoder
            # real enquanto sua biblioteca ainda está disponível, sem depender
            # de uma coleta posterior dentro daquela simulação.
            if hasattr(voice, "encoder"):
                encoder = voice.encoder
                del voice.encoder
                del encoder


@pytest.mark.asyncio
async def test_native_audio_mirror_receives_exact_delivered_bytes_after_one_synthesis(world):
    events = []
    data = b"delivered-mp3"
    world.tts.synthesize_chatbot_attachment.return_value = data

    async def send(**kwargs):
        self_data = kwargs["file"].fp.read()
        assert self_data == data
        events.append("chat")
        return SimpleNamespace(id=88)

    async def mirror(**kwargs):
        assert kwargs["audio"] is data
        assert (kwargs["guild_id"], kwargs["user_id"], kwargs["text_channel_id"], kwargs["request_id"]) == (10, 1, 30, "abc123")
        await kwargs["before_effect"]()
        events.append("call_queue")
        return {"ok": True, "status": "enqueued"}

    world.chat.send.side_effect = send
    world.tts.chatbot_mirror_audio = AsyncMock(side_effect=mirror)
    world.bot.get_cog("Chatbot").record_audio_reply_sent = AsyncMock()
    result = await execute_action(world.bot, doc("send_audio"), actor_id=1)
    assert result.message_id == 88
    assert events == ["chat", "call_queue"]
    world.tts.synthesize_chatbot_attachment.assert_awaited_once()
    world.chat.send.assert_awaited_once()
    world.bot.get_cog("Chatbot").record_audio_reply_sent.assert_awaited_once_with(guild_id=10, channel_id=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["voice_disabled", "memory_reset", "safe_mode"])
async def test_native_mirror_guard_rechecks_voice_setting_and_memory_after_enqueue(world, revocation, monkeypatch):
    from cogs.chatbot import constants as C
    action = doc("send_audio")
    action["memory_epoch"] = {"global_generation": 1, "guild_generation": 2, "user_generation": 3}
    world.bot.get_cog("Chatbot")._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=MemoryEpoch(1, 2, 3)))
    world.tts.chatbot_mirror_audio = AsyncMock(return_value={"ok": True, "status": "enqueued"})
    result = await execute_action(world.bot, action, actor_id=1)
    assert result.message_id == 88
    guard = world.tts.chatbot_mirror_audio.await_args.kwargs["before_effect"]
    await guard()
    if revocation == "voice_disabled":
        world.config.voice_actions_enabled = False
    elif revocation == "memory_reset":
        world.bot.get_cog("Chatbot")._memory.capture_epoch.return_value = MemoryEpoch(1, 2, 4)
    else:
        monkeypatch.setattr(C, "SAFE_MODE", True)
    with pytest.raises(ValueError):
        await guard()
    world.chat.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_mirror_failure_preserves_confirmed_chat_send_without_retry(world):
    world.tts.chatbot_mirror_audio = AsyncMock(side_effect=RuntimeError("failed"))
    result = await execute_action(world.bot, doc("send_audio"), actor_id=1)
    assert result.public_result == "Áudio enviado." and result.message_id == 88
    world.tts.synthesize_chatbot_attachment.assert_awaited_once()
    world.chat.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_send_failure_never_enqueues_mirror(world):
    world.tts.chatbot_mirror_audio = AsyncMock()
    world.chat.send.side_effect = asyncio.TimeoutError()
    with pytest.raises(ValueError):
        await execute_action(world.bot, doc("send_audio"), actor_id=1)
    world.tts.chatbot_mirror_audio.assert_not_awaited()
