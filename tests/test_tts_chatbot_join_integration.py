"""Join aprovado seguido do pipeline real de prefixo, fila e playback do TTS."""
from __future__ import annotations

import asyncio
import base64
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
from cogs.tts import cog as tts_cog
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


class _RealProducerPrefixProbe(_DecodedPrefixProbe):
    _produce_shared_job = tts_audio.TTSAudioMixin._produce_shared_job


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
    async def test_actual_cog_initialization_and_load_allow_short_prefixes_after_approved_join(self):
        """Instância TTSVoice normal, com bootstrap e helpers de produção reais."""
        with tempfile.TemporaryDirectory() as directory:
            _, mp3_path = _make_codec_fixture(directory)
            external = _Probe(connected=False)
            external.member.bot = False
            external.member.display_name = "Membro"
            external.member.roles = []
            external.channel.user_limit = 0
            external.channel.members = [external.member]
            external.guild.name = "Servidor de teste"
            external.guild.me.bot = True
            bot = external.bot
            bot.voice_clients = []
            bot.user = external.guild.me
            bot.settings_db = external.db
            bot.get_cog = lambda _: None
            bot.get_channel = external.guild.get_channel
            bot.wait_until_ready = asyncio.Event().wait
            bot.is_closed = lambda: False
            bot.guilds = [external.guild]
            external.db.guild_cache = {1: {}}
            external.db.get_guild_tts_defaults = lambda _: {
                "enabled": True, "edge_prefix": ".", "gtts_prefix": ",", "bot_prefix": "_",
                "tts_prefix": "_", "auto_leave": False, "announce_author": False,
            }
            external.db.resolve_tts = lambda *_: {"engine": "gtts", "language": "pt-br"}
            text_channel = SimpleNamespace(id=30, send=AsyncMock())

            async def connect(**kwargs):
                voice = _CodecVoice(external.guild, external.channel)
                external.guild.voice_client = voice
                external.guild.me.voice.channel = external.channel
                bot.voice_clients.append(voice)
                return voice

            external.channel.connect = AsyncMock(side_effect=connect)
            edge_requests = []

            async def edge_stream(communicate):
                edge_requests.append(communicate)
                yield {"type": "audio", "data": mp3_path.read_bytes()}

            encoded = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
            response = Mock()
            response.iter_lines.return_value = [('jQ1olc","[\\"' + encoded + '\\"]').encode("ascii")]
            with _offline_runtime(directory), patch.object(tts_cog, "TTS_TEMP_DIR", directory), patch.multiple(
                tts_audio, PHONE_WORKER_ENABLED=False, WORKER_VOICE_AGENT_ENABLED=False,
                TTS_WORKER_AGENT_ENABLED=False, TTS_FFMPEG_PRIME_ENABLED=True,
            ), patch.object(tts_audio.edge_tts.Communicate, "stream", edge_stream), patch.object(
                tts_audio.edge_tts, "list_voices", AsyncMock(return_value=[{"ShortName": "pt-BR-FranciscaNeural"}]),
            ), patch.object(tts_audio.requests.Session, "send", return_value=response) as send:
                cog = TTSVoice(bot)
                try:
                    await cog.cog_load()
                    self.assertTrue(cog._tts_runtime_primed)
                    joined = await cog.chatbot_join_voice(guild_id=1, user_id=2, channel_id=20, request_id="join-init")
                    self.assertTrue(joined["ok"])
                    voice = external.guild.voice_client
                    for index, content in enumerate((". Ajanabav", ", A"), start=1):
                        message = SimpleNamespace(id=400 + index, guild=external.guild, author=external.member,
                                                  channel=text_channel, content=content, attachments=[],
                                                  mentions=[], role_mentions=[], channel_mentions=[])
                        await cog.on_message(message)
                        await asyncio.wait_for(cog._get_state(1).queue.join(), 5)
                        await voice.finish_thread()
                        self.assertEqual(voice.play_calls, index)
                    self.assertEqual(len(edge_requests), 1)
                    send.assert_called_once()
                    self.assertEqual(cog._get_engine_metrics("edge")["synth_failures"], 0)
                    self.assertEqual(cog._get_engine_metrics("gtts")["synth_failures"], 0)
                    text_channel.send.assert_not_awaited()
                    external.db.set_tts_voice_channel_id.assert_not_awaited()
                    self.assertEqual(cog._chatbot_temporary_voice_channels, {1: 20})
                    external.channel.connect.assert_awaited_once()
                    self.assertEqual(voice.move_calls, [])
                finally:
                    cog.cog_unload()
                    tasks = set(cog._get_tts_background_tasks())
                    tasks.update(task for task in (cog._voice_restore_task, cog._voice_incident_report_worker_task)
                                 if task is not None)
                    tasks.update(state.worker_task for state in cog.guild_states.values() if state.worker_task)
                    await asyncio.gather(*tasks, return_exceptions=True)
                    if external.guild.voice_client:
                        await external.guild.voice_client.finish_thread()

    @unittest.skipUnless(os.name == "posix" and callable(getattr(os, "mkfifo", None)), "FIFO POSIX indisponível")
    async def test_short_prefixes_use_real_provider_producer_and_prime_after_approved_join(self):
        """Exercita os textos da falha relatada sem simular o producer compartilhado."""
        with tempfile.TemporaryDirectory() as directory:
            _, mp3_path = _make_codec_fixture(directory)
            probe = _RealProducerPrefixProbe(mp3_path)
            edge_requests = []

            async def edge_stream(communicate):
                edge_requests.append(communicate)
                yield {"type": "audio", "data": probe.provider_audio}

            # Mantém gTTS(), preparação HTTP, parser e bridge de thread reais.
            # Apenas a resposta externa contém o MP3 audível gerado acima.
            encoded = base64.b64encode(probe.provider_audio).decode("ascii")
            response = Mock()
            response.iter_lines.return_value = [('jQ1olc","[\\"' + encoded + '\\"]').encode("ascii")]
            with _offline_runtime(directory), patch.object(discord.opus, "_lib", None), patch.object(discord.opus, "_load_default", return_value=False), patch.multiple(
                tts_audio, TTS_EDGE_VPS_FAST_PATH_ENABLED=True, TTS_EDGE_STREAMING_ENABLED=True,
                TTS_GTTS_STREAMING_ENABLED=True, TTS_GTTS_STREAM_MIN_CHARS=100,
                TTS_FFMPEG_PRIME_ENABLED=True, TTS_GTTS_PERSISTENT_SESSION_ENABLED=True,
            ), patch.object(tts_audio.edge_tts.Communicate, "stream", edge_stream), patch.object(
                tts_audio.requests.Session, "send", return_value=response,
            ) as send:
                joined = await probe.chatbot_join_voice(guild_id=1, user_id=2, channel_id=20, request_id="join-short")
                self.assertTrue(joined["ok"])
                voice = probe.guild.voice_client
                try:
                    for index, content in enumerate((". Aaakjaha", ", A"), start=1):
                        message = SimpleNamespace(id=300 + index, guild=probe.guild, author=probe.member,
                                                  channel=SimpleNamespace(id=30), content=content)
                        await probe.on_message(message)
                        await asyncio.wait_for(probe._get_state(1).queue.join(), 5)
                        await voice.finish_thread()
                        self.assertEqual(voice.play_calls, index)
                        result = probe.playback_kinds[-1]
                        self.assertTrue(result["first_frame_observed"])
                        self.assertTrue(result["source_primed"])
                        self.assertEqual(result["playback_source"], "ffmpeg_opus_fallback")
                        self.assertEqual(bool(result.get("progressive_stream")), index == 1)
                    self.assertEqual(len(edge_requests), 1)
                    send.assert_called_once()
                    self.assertEqual(probe.metrics["edge_stream_completed"], 1)
                    self.assertEqual(probe.metrics["gtts_stream_completed"], 1)
                    self.assertEqual(probe.metrics.get("edge_stream_failures", 0), 0)
                    self.assertEqual(probe.metrics.get("gtts_stream_failures", 0), 0)
                    self.assertEqual(probe._chatbot_temporary_voice_channels, {1: 20})
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
                    background = list(probe._get_tts_background_tasks())
                    for task in background:
                        task.cancel()
                    await asyncio.gather(*background, return_exceptions=True)
                    executor = getattr(probe, "_tts_gtts_executor", None)
                    if executor:
                        await asyncio.to_thread(executor.shutdown, wait=True)

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
