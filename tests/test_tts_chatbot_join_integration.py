"""Join aprovado seguido do pipeline real de prefixo, fila e playback do TTS."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import ExitStack
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import discord

from test_chatbot_voice_actions import _Probe
from cogs.tts import audio as tts_audio
from cogs.tts.cog import TTSVoice


class _CodecVoice(discord.VoiceClient):
    """VoiceClient/AudioPlayer reais; somente socket e websocket são simulados."""

    def __init__(self, guild, channel):
        self.channel = channel
        self.channel.guild = guild
        self.client = SimpleNamespace(loop=asyncio.get_running_loop())
        self._connection = SimpleNamespace(is_connected=lambda: True,
                                           ws=SimpleNamespace(speak=AsyncMock()))
        self._player = None
        self.play_calls = 0
        self.move_calls = []
        self.sent_packets = []

    @property
    def connected(self):
        return self.is_connected()

    @property
    def playing(self):
        return self.is_playing() or self.is_paused()

    def play(self, source, *, after=None):
        super().play(source, after=after)
        self.play_calls += 1

    def send_audio_packet(self, data, *, encode=True):
        # Exercita o encoder nativo quando a fonte é PCM, sem enviar à rede.
        encoded = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME) if encode else data
        self.sent_packets.append((encoded, encode))

    async def finish_thread(self):
        player = self._player
        if player is not None:
            await asyncio.to_thread(player.join, 2)
            if player.is_alive():
                player.stop()
                await asyncio.to_thread(player.join, 2)


def _make_codec_fixture(directory):
    """Áudio audível válido, gerado localmente sem Edge/gTTS/rede."""
    wav_path = Path(directory) / "fixture.wav"
    mp3_path = Path(directory) / "fixture.mp3"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / 16000)))
                                   for n in range(3200)))
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-y",
                    "-i", str(wav_path), str(mp3_path)], check=True,
                   capture_output=True, timeout=10)
    return wav_path, mp3_path


def _offline_runtime(directory):
    runtime = Path(directory) / "runtime"
    cache = Path(directory) / "cache"
    runtime.mkdir()
    cache.mkdir()
    stack = ExitStack()
    stack.enter_context(patch.object(tts_audio, "TTS_TEMP_DIR", str(directory)))
    stack.enter_context(patch.object(tts_audio, "_RUNTIME_DIR", str(runtime)))
    stack.enter_context(patch.object(tts_audio, "_CACHE_DIR", str(cache)))
    stack.enter_context(patch.object(tts_audio, "_TTS_REQUIRED_DIRS", (str(directory), str(runtime), str(cache))))
    return stack


class _PrefixProbe(_Probe):
    on_message = TTSVoice.on_message
    _process_tts_message = TTSVoice._process_tts_message
    _mark_tts_message_seen = TTSVoice._mark_tts_message_seen
    _was_tts_message_seen = TTSVoice._was_tts_message_seen
    _record_tts_message_gate = TTSVoice._record_tts_message_gate
    _guild_announce_author_enabled = TTSVoice._guild_announce_author_enabled

    def __init__(self, audio_path):
        super().__init__(connected=False)
        self._recent_tts_message_ids = OrderedDict()
        self.member.bot, self.member.display_name = False, "Membro"
        self.db.get_guild_tts_defaults = AsyncMock(return_value={
            "enabled": True, "bot_prefix": "_", "edge_prefix": ".",
            "gtts_prefix": ",", "atts_prefix": "%", "teto_prefix": "'",
        })
        self.db.resolve_tts = AsyncMock(return_value={"engine": "gtts", "language": "pt-br"})
        self._schedule_worker_voice_agent_register_session = Mock()
        self._schedule_tts_turbo_benchmark_if_needed = Mock(return_value=False)
        self._try_worker_voice_direct_tts = AsyncMock(return_value=None)
        self._try_get_cached_path = Mock(return_value=None)
        self._schedule_cache_maintenance = Mock()
        self._resolve_audio_path = AsyncMock(return_value=(str(audio_path), False))
        self._record_queue_timing = Mock()

    def _render_tts_text(self, message, text):
        return text.strip()

    def _apply_author_prefix_if_needed(self, guild_id, author, text, *, enabled):
        return text


class _DecodedPrefixProbe(_PrefixProbe):
    _make_discord_tts_source = tts_audio.TTSAudioMixin._make_discord_tts_source
    _edge_stream_handle_for_path = tts_audio.TTSAudioMixin._edge_stream_handle_for_path
    _path_audio_format = tts_audio.TTSAudioMixin._path_audio_format

    def __init__(self, mp3_path):
        super().__init__(mp3_path)
        # Os passos resolve/cache/FIFO/FFmpeg abaixo usam a implementação real.
        del self._resolve_audio_path
        del self._try_get_cached_path
        self.provider_audio = Path(mp3_path).read_bytes()
        self.produced_engines = []
        self.playback_kinds = []
        self._schedule_worker_turbo_cache_store = Mock()

    def _tts_agent_route_available(self):
        return False

    def _connect_now(self, channel):
        self.guild.voice_client = _CodecVoice(self.guild, channel)
        self.guild.me.voice.channel = channel
        return self.guild.voice_client

    async def _produce_shared_job(self, job):
        # Única fronteira do provedor simulada: seus bytes MP3 válidos.
        self.produced_engines.append(job.item.engine)
        job.deadline = time.monotonic() + 5
        job.slot_ready.set()
        await self._append_shared_audio(job, self.provider_audio)
        await job.buffer.finish()

    async def _play_file(self, *args, **kwargs):
        result = await super()._play_file(*args, **kwargs)
        self.playback_kinds.append(result)
        return result


class ChatbotJoinPrefixIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_temporary_join_allows_edge_and_gtts_prefixes_without_new_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speech.mp3"
            path.write_bytes(b"fake-provider-audio")
            probe = _PrefixProbe(path)
            guard = AsyncMock()
            joined = await probe.chatbot_join_voice(
                guild_id=1, user_id=2, channel_id=20, request_id="join-1", before_effect=guard,
            )
            self.assertTrue(joined["ok"])
            voice = probe.guild.voice_client
            for index, prefix in enumerate((".", ","), start=1):
                message = SimpleNamespace(id=100 + index, guild=probe.guild, author=probe.member,
                                          channel=SimpleNamespace(id=30), content=prefix + "teste de prefixo")
                with patch.object(tts_audio, "TTS_FFMPEG_PRIME_ENABLED", False):
                    await probe.on_message(message)
                    state = probe._get_state(1)
                    await asyncio.wait_for(state.queue.join(), timeout=1)
                self.assertEqual(voice.play_calls, index)
                self.assertIsNotNone(state.worker_task)
            items = [call.args[1] for call in probe._resolve_audio_path.await_args_list]
            self.assertEqual([item.engine for item in items], ["edge", "gtts"])
            self.assertTrue(all(not item.chatbot_no_auto_connect for item in items))
            self.assertTrue(all(item.chatbot_before_effect is None for item in items))
            self.assertEqual(probe._chatbot_temporary_voice_channels, {1: 20})
            self.assertFalse(await probe._runtime_should_restore_voice(1))
            probe.db.set_tts_voice_channel_id.assert_not_awaited()
            self.assertEqual(voice.move_calls, [])
            probe.channel.connect.assert_awaited_once()
            self.assertEqual(guard.await_count, 2)
            state.worker_task.cancel()
            await asyncio.gather(state.worker_task, return_exceptions=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg não instalado")
class ChatbotJoinRealCodecIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_native_opus_reproduces_old_pcm_failure_and_actual_fallback_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path, mp3_path = _make_codec_fixture(directory)
            probe = _DecodedPrefixProbe(mp3_path)
            voice = probe._connect_now(probe.channel)
            with patch.object(discord.opus, "_lib", None), patch.object(discord.opus, "_load_default", return_value=False):
                legacy = discord.FFmpegPCMAudio(str(mp3_path), before_options="-nostdin", options="-vn -loglevel error")
                try:
                    self.assertEqual(len(await asyncio.to_thread(legacy.read)), 3840)
                    with self.assertRaises(discord.opus.OpusNotLoaded):
                        voice.play(legacy)
                    self.assertIsNone(voice._player)
                finally:
                    legacy.cleanup()
                for path in (wav_path, mp3_path):
                    with self.subTest(format=path.suffix):
                        source, kind = probe._make_discord_tts_source(str(path))
                        self.assertEqual(kind, "ffmpeg_opus_fallback")
                        self.assertTrue(source.is_opus())
                        finished = asyncio.get_running_loop().create_future()
                        loop = asyncio.get_running_loop()

                        def after(error):
                            loop.call_soon_threadsafe(finished.set_result, error)

                        previous = len(voice.sent_packets)
                        voice.play(source, after=after)
                        error = await asyncio.wait_for(finished, 3)
                        await voice.finish_thread()
                        self.assertIsNone(error)
                        packets = voice.sent_packets[previous:]
                        audio = [data for data, _ in packets if data != b"\xf8\xff\xfe" and not data.startswith((b"OpusHead", b"OpusTags"))]
                        self.assertGreaterEqual(len(audio), 8)
                        self.assertTrue(all(not encode for _, encode in packets))

    @unittest.skipUnless(os.name == "posix" and callable(getattr(os, "mkfifo", None)), "FIFO POSIX indisponível")
    async def test_prefixes_use_real_shared_audio_fifo_decoder_and_player_after_temporary_join(self):
        with tempfile.TemporaryDirectory() as directory:
            _, mp3_path = _make_codec_fixture(directory)
            with _offline_runtime(directory), patch.object(discord.opus, "_lib", None), patch.object(discord.opus, "_load_default", return_value=False), patch.multiple(
                tts_audio, TTS_EDGE_VPS_FAST_PATH_ENABLED=True, TTS_EDGE_STREAMING_ENABLED=True,
                TTS_GTTS_STREAMING_ENABLED=True, TTS_GTTS_STREAM_MIN_CHARS=100, TTS_FFMPEG_PRIME_ENABLED=True,
            ):
                probe = _DecodedPrefixProbe(mp3_path)
                joined = await probe.chatbot_join_voice(guild_id=1, user_id=2, channel_id=20, request_id="join-codec")
                self.assertTrue(joined["ok"])
                voice = probe.guild.voice_client
                try:
                    for index, prefix in enumerate((".", ","), start=1):
                        message = SimpleNamespace(id=200 + index, guild=probe.guild, author=probe.member,
                                                  channel=SimpleNamespace(id=30), content=prefix + "fala pelo pipeline completo " * 6)
                        await probe.on_message(message)
                        await asyncio.wait_for(probe._get_state(1).queue.join(), 5)
                        await voice.finish_thread()
                        self.assertEqual(voice.play_calls, index)
                        self.assertTrue(probe.playback_kinds[-1]["first_frame_observed"])
                        self.assertTrue(probe.playback_kinds[-1]["progressive_stream"])
                        self.assertEqual(probe.playback_kinds[-1]["playback_source"], "ffmpeg_opus_fallback")
                    self.assertEqual(probe.produced_engines, ["edge", "gtts"])
                    self.assertGreaterEqual(sum(data != b"\xf8\xff\xfe" and not data.startswith((b"OpusHead", b"OpusTags"))
                                                for data, _ in voice.sent_packets), 16)
                    self.assertTrue(all(not encode for _, encode in voice.sent_packets))
                    self.assertEqual(probe._chatbot_temporary_voice_channels, {1: 20})
                    self.assertFalse(await probe._runtime_should_restore_voice(1))
                    probe.db.set_tts_voice_channel_id.assert_not_awaited()
                    probe.channel.connect.assert_awaited_once()
                    self.assertEqual(voice.move_calls, [])
                finally:
                    state = probe._get_state(1)
                    if state.worker_task:
                        state.worker_task.cancel()
                        await asyncio.gather(state.worker_task, return_exceptions=True)
                    await voice.finish_thread()
                    probe._shutdown_shared_synthesis()
