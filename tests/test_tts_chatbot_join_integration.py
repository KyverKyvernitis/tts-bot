"""Join aprovado seguido do pipeline real de prefixo, fila e playback do TTS."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_chatbot_voice_actions import _Probe
from cogs.tts import audio as tts_audio
from cogs.tts.cog import TTSVoice


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
