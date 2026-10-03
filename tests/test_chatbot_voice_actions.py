"""Ações de voz autorizadas, sem Discord/FFmpeg/provedores reais."""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.tts.audio import TTSAudioMixin, QueueItem
from cogs.tts.chatbot_actions import ChatbotVoiceActionsMixin, ChatbotVoiceActionBlocked
from cogs.tts.cog import TTSVoice


class _Source(discord.AudioSource):
    def __init__(self, *, audible=True):
        self.audible = audible

    def read(self):
        return b"\0" * 3840 if self.audible else b""


class _Voice:
    def __init__(self, guild, channel):
        self.guild, self.channel = guild, channel
        self.connected, self.playing = True, False
        self.played = asyncio.Event()
        self.after = None
        self.auto_finish = True
        self.play_calls = 0
        self.stop_calls = 0
        self.move_calls = []

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def play(self, source, *, after):
        self.play_calls += 1
        self.after, self.playing = after, True
        source.read()
        self.played.set()
        if self.auto_finish:
            asyncio.get_running_loop().call_soon(self.finish)

    def finish(self, error=None):
        self.playing = False
        if self.after:
            callback, self.after = self.after, None
            callback(error)

    def stop(self):
        self.stop_calls += 1
        self.finish()

    async def move_to(self, channel):
        self.move_calls.append(channel.id)
        self.channel = channel
        self.guild.me.voice.channel = channel


class _Probe(ChatbotVoiceActionsMixin, TTSAudioMixin):
    _ensure_connected = TTSVoice._ensure_connected
    _set_remembered_voice_channel = TTSVoice._set_remembered_voice_channel
    _runtime_should_restore_voice = TTSVoice._runtime_should_restore_voice

    def __init__(self, *, connected=True):
        self.guild_states = {}
        self.locks = {}
        self._chatbot_temporary_voice_channels = {}
        self._runtime_voice_restore_failures = {}
        self._runtime_voice_restore_next_allowed_at = {}
        self._voice_auto_restore_enabled = True
        self.music = False
        self.metrics = {}
        self.audible = True
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id, self.channel.name = 20, "Call aprovada"
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        self.other = Mock(spec=discord.VoiceChannel)
        self.other.id, self.other.name = 21, "Outra call"
        self.member = SimpleNamespace(id=2, voice=SimpleNamespace(channel=self.channel))
        self.guild = SimpleNamespace(id=1, voice_client=None, me=SimpleNamespace(id=9, voice=SimpleNamespace(channel=None, self_deaf=True, mute=False, suppress=False)))
        self.guild.get_channel = lambda ident: {20: self.channel, 21: self.other}.get(ident)
        self.guild.get_member = lambda ident: self.member if ident == 2 else None
        self.guild.change_voice_state = AsyncMock()
        self.bot = SimpleNamespace(get_guild=lambda ident: self.guild if ident == 1 else None, audio_router=None)
        self.db = SimpleNamespace(resolve_tts=lambda *_: {}, set_tts_voice_channel_id=AsyncMock())
        self.channel.connect = AsyncMock(side_effect=self.connect)
        self._voice_should_self_deaf = AsyncMock(return_value=True)
        self._recover_stale_voice_client = AsyncMock()
        self._clear_manual_voice_disconnect = Mock()
        self._cancel_runtime_voice_restore = Mock()
        self._schedule_voice_incident_recovery = Mock()
        self._schedule_voice_failure_report = Mock()
        self._remember_expected_voice_channel = Mock()
        self.synthesize_chatbot_attachment = AsyncMock(return_value=b"fake-mp3")
        if connected:
            self._connect_now(self.channel)

    def _connect_now(self, channel):
        self.guild.voice_client = _Voice(self.guild, channel)
        self.guild.me.voice.channel = channel
        return self.guild.voice_client

    async def connect(self, **kwargs):
        return self._connect_now(self.channel)

    def _get_db(self):
        return self.db

    def _get_voice_client_for_guild(self, guild):
        return guild.voice_client

    def _get_bot_voice_state_channel(self, guild):
        return guild.me.voice.channel

    def _voice_client_channel(self, vc):
        return getattr(vc, "channel", None)

    def _voice_client_is_connected(self, vc):
        return bool(vc and vc.connected)

    def _voice_client_is_playing_or_paused(self, vc):
        return bool(vc and vc.playing)

    def _voice_client_owned_by_music(self, vc):
        return False

    def _music_player_is_active(self, guild_id):
        return self.music

    def _music_should_own_voice(self, guild):
        return self.music

    def _is_music_active_for_guild(self, guild_id):
        return self.music

    def _get_voice_connect_lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def _diagnose_voice_connect_precheck(self, guild, channel):
        return None

    def _is_voice_client_stale(self, guild, vc):
        return False

    def _edge_stream_handle_for_path(self, path):
        return None

    def _make_discord_tts_source(self, path):
        assert os.path.isfile(path)
        self.playback_path = path
        return _Source(audible=self.audible), "fake"

    def _get_metrics_store(self):
        return self.metrics

    def _record_latency_sample(self, *args):
        pass

    def _path_audio_format(self, path):
        return "mp3"

    def _estimate_playback_timeout(self, item):
        return 2.0

    async def _get_guild_toggle_value(self, *args, **kwargs):
        return False


class ChatbotVoiceActionsTests(unittest.IsolatedAsyncioTestCase):
    async def speak(self, probe, **kwargs):
        return await probe.chatbot_speak_voice(guild_id=1, user_id=2, channel_id=20, request_id="req-1", text="Olá", **kwargs)

    async def join(self, probe, **kwargs):
        return await probe.chatbot_join_voice(guild_id=1, user_id=2, channel_id=20, request_id="req-1", **kwargs)

    async def test_common_member_speaks_and_waits_for_actual_playback(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        voice.auto_finish = False
        task = asyncio.create_task(self.speak(probe))
        await voice.played.wait()
        self.assertFalse(task.done())
        self.assertTrue(probe._get_voice_connect_lock(1).locked())
        voice.finish()
        result = await task
        self.assertEqual(result["status"], "executed")
        self.assertTrue(result["ok"])
        self.assertEqual(voice.play_calls, 1)
        self.assertEqual(voice.move_calls, [])
        probe.channel.connect.assert_not_awaited()
        self.assertFalse(os.path.exists(probe.playback_path))

    async def test_speak_does_not_join_or_move(self):
        for connected in (False, True):
            with self.subTest(connected=connected):
                probe = _Probe(connected=connected)
                if connected:
                    probe.guild.voice_client.channel = probe.other
                    probe.guild.me.voice.channel = probe.other
                result = await self.speak(probe)
                self.assertFalse(result["ok"])
                probe.synthesize_chatbot_attachment.assert_not_awaited()
                probe.channel.connect.assert_not_awaited()

    async def test_speak_rechecks_user_channel_after_synthesis(self):
        probe = _Probe()

        async def moved(**kwargs):
            probe.member.voice.channel = probe.other
            return b"mp3"

        probe.synthesize_chatbot_attachment.side_effect = moved
        result = await self.speak(probe)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(probe.guild.voice_client.play_calls, 0)

    async def test_music_takeover_during_synthesis_blocks_speech(self):
        probe = _Probe()

        async def takeover(**kwargs):
            probe.music = True
            return b"mp3"

        probe.synthesize_chatbot_attachment.side_effect = takeover
        result = await self.speak(probe)
        self.assertFalse(result["ok"])
        self.assertEqual(probe.guild.voice_client.play_calls, 0)

    async def test_full_tts_queue_is_preserved(self):
        probe = _Probe()
        state = probe._get_state(1)
        normal = QueueItem(1, 20, 2, "TTS normal", "edge", "pt-BR-FranciscaNeural", "pt-br", "+0%", "+0Hz")
        state.queue.put_nowait(normal)
        result = await self.speak(probe)
        self.assertFalse(result["ok"])
        self.assertIs(state.queue.get_nowait(), normal)
        probe.synthesize_chatbot_attachment.assert_not_awaited()

    async def test_synthesis_bounded_and_no_worker_or_queue_created_for_speech(self):
        probe = _Probe()
        result = await probe.chatbot_speak_voice(guild_id=1, user_id=2, channel_id=20, request_id="req-1", text="a" * 2000)
        self.assertTrue(result["ok"])
        self.assertEqual(len(probe.synthesize_chatbot_attachment.await_args.kwargs["text"]), 800)
        state = probe._get_state(1)
        self.assertIsNone(state.worker_task)
        self.assertTrue(state.queue.empty())

    async def test_current_audio_is_not_stopped(self):
        probe = _Probe()
        probe.guild.voice_client.playing = True
        result = await self.speak(probe)
        self.assertFalse(result["ok"])
        self.assertEqual(probe.guild.voice_client.stop_calls, 0)

    async def test_speak_permission_and_stage_checks(self):
        for name in ("view_channel", "connect", "speak"):
            with self.subTest(permission=name):
                probe = _Probe()
                setattr(probe.channel.permissions_for.return_value, name, False)
                self.assertFalse((await self.speak(probe))["ok"])
        probe = _Probe()
        stage = Mock(spec=discord.StageChannel)
        stage.id = 20
        probe.guild.get_channel = lambda _: stage
        self.assertFalse((await self.speak(probe))["ok"])

    async def test_join_is_temporary_and_does_not_persist_or_restore(self):
        probe = _Probe(connected=False)
        result = await self.join(probe)
        self.assertTrue(result["ok"])
        self.assertEqual(probe._chatbot_temporary_voice_channels, {1: 20})
        probe.channel.connect.assert_awaited_once_with(self_deaf=True, reconnect=False)
        probe.db.set_tts_voice_channel_id.assert_not_awaited()
        await probe._set_remembered_voice_channel(1, 20)
        self.assertFalse(await probe._runtime_should_restore_voice(1))
        probe.db.set_tts_voice_channel_id.assert_not_awaited()

    async def test_join_rechecks_pin_after_waiting_for_lock(self):
        probe = _Probe(connected=False)
        lock = probe._get_voice_connect_lock(1)
        await lock.acquire()
        task = asyncio.create_task(self.join(probe))
        await asyncio.sleep(0)
        probe.member.voice.channel = probe.other
        lock.release()
        result = await task
        self.assertEqual(result["status"], "failed")
        probe.channel.connect.assert_not_awaited()

    async def test_temporary_voice_disconnect_listener_does_not_restore(self):
        probe = _Probe(connected=False)
        self.assertTrue((await self.join(probe))["ok"])
        probe.guild.me.guild = probe.guild
        probe.guild.me.voice.channel = None
        probe.guild.voice_client = None
        probe._expected_voice_channel_ids = {1: 20}
        probe._get_remembered_voice_channel_id = AsyncMock(return_value=0)
        probe._is_manual_voice_disconnect_recent = Mock(return_value=False)
        probe._runtime_voice_restore_is_suppressed = Mock(return_value=False)
        probe._clear_remembered_voice_channel = TTSVoice._clear_remembered_voice_channel.__get__(probe)
        probe._schedule_runtime_voice_restore = AsyncMock()
        await TTSVoice.on_voice_state_update(
            probe, probe.guild.me, SimpleNamespace(channel=probe.channel), SimpleNamespace(channel=None),
        )
        probe._schedule_runtime_voice_restore.assert_not_awaited()
        probe.db.set_tts_voice_channel_id.assert_not_awaited()
        self.assertEqual(probe._chatbot_temporary_voice_channels, {})

    async def test_join_rechecks_music_ownership_after_waiting_for_lock(self):
        probe = _Probe(connected=False)
        lock = probe._get_voice_connect_lock(1)
        await lock.acquire()
        task = asyncio.create_task(self.join(probe))
        await asyncio.sleep(0)
        probe.music = True
        lock.release()
        result = await task
        self.assertEqual(result["status"], "failed")
        probe.channel.connect.assert_not_awaited()

    async def test_join_music_or_other_call_busy(self):
        probe = _Probe(connected=False)
        probe.music = True
        self.assertFalse((await self.join(probe))["ok"])
        probe.channel.connect.assert_not_awaited()
        probe = _Probe()
        probe.guild.voice_client.channel = probe.other
        probe.guild.me.voice.channel = probe.other
        self.assertFalse((await self.join(probe))["ok"])
        self.assertEqual(probe.guild.voice_client.move_calls, [])

    async def test_join_network_error_is_not_retried(self):
        probe = _Probe(connected=False)
        probe.channel.connect.side_effect = OSError("network")
        result = await self.join(probe)
        self.assertEqual(result["status"], "uncertain")
        probe.channel.connect.assert_awaited_once()

    async def test_playback_failure_does_not_reconnect_or_retry(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        voice.auto_finish = False
        task = asyncio.create_task(self.speak(probe))
        await voice.played.wait()
        voice.finish(RuntimeError("voice transport lost"))
        result = await task
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(voice.play_calls, 1)
        probe.channel.connect.assert_not_awaited()

    async def test_cancellation_stops_only_this_started_audio(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        voice.auto_finish = False
        task = asyncio.create_task(self.speak(probe))
        await voice.played.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(voice.stop_calls, 1)
        self.assertFalse(os.path.exists(probe.playback_path))
        self.assertFalse(probe._chatbot_voice_speaking)

    async def test_same_request_concurrent_speech_is_not_duplicated(self):
        probe = _Probe()
        gate = asyncio.Event()

        async def synth(**kwargs):
            await gate.wait()
            return b"mp3"

        probe.synthesize_chatbot_attachment.side_effect = synth
        first = asyncio.create_task(self.speak(probe))
        await asyncio.sleep(0)
        self.assertFalse((await self.speak(probe))["ok"])
        gate.set()
        self.assertTrue((await first)["ok"])
        self.assertEqual(probe.guild.voice_client.play_calls, 1)

    async def test_empty_source_does_not_claim_success(self):
        probe = _Probe()
        probe.audible = False
        result = await self.speak(probe)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(probe.guild.voice_client.play_calls, 1)

    async def test_speak_guard_runs_after_synthesis_source_and_inside_both_locks(self):
        probe = _Probe()

        async def guard():
            self.assertTrue(probe._get_voice_connect_lock(1).locked())
            self.assertTrue(probe._get_tts_playback_lock(1).locked())
            self.assertTrue(os.path.isfile(probe.playback_path))
            self.assertEqual(probe.guild.voice_client.play_calls, 0)
            probe.synthesize_chatbot_attachment.assert_awaited_once()
            raise ValueError("permissão revogada")

        with self.assertRaisesRegex(ValueError, "permissão revogada"):
            await self.speak(probe, before_effect=guard)
        self.assertEqual(probe.guild.voice_client.play_calls, 0)
        self.assertFalse(os.path.exists(probe.playback_path))
        self.assertFalse(probe._chatbot_voice_speaking)

    async def test_speak_rechecks_channel_after_async_effect_guard(self):
        probe = _Probe()

        async def guard():
            await asyncio.sleep(0)
            probe.member.voice.channel = probe.other

        result = await self.speak(probe, before_effect=guard)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(probe.guild.voice_client.play_calls, 0)

    async def test_join_guard_rechecks_after_handshake_settings_await(self):
        probe = _Probe(connected=False)
        ready = False

        async def self_deaf(_guild_id):
            nonlocal ready
            await asyncio.sleep(0)
            ready = True
            return True

        probe._voice_should_self_deaf.side_effect = self_deaf
        seen = []

        async def guard():
            self.assertTrue(probe._get_voice_connect_lock(1).locked())
            seen.append(ready)
            if ready:
                raise ValueError("ação desativada")

        with self.assertRaisesRegex(ValueError, "ação desativada"):
            await self.join(probe, before_effect=guard)
        self.assertEqual(seen, [False, True])
        probe.channel.connect.assert_not_awaited()
        self.assertEqual(probe._chatbot_temporary_voice_channels, {})

    async def test_join_rechecks_pin_after_effect_guard(self):
        probe = _Probe(connected=False)

        async def guard():
            await asyncio.sleep(0)
            probe.member.voice.channel = probe.other

        result = await self.join(probe, before_effect=guard)
        self.assertEqual(result["status"], "failed")
        probe.channel.connect.assert_not_awaited()
