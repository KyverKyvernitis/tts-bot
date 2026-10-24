"""Production routing/startup methods with buffer/FFmpeg boundaries adapted.

These stdlib checks execute the real preparation method AST. They do not
replace packet/native codec tests and do not make network or timing claims.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from cogs.musica.runtime_telefone.agente.configuracao import audio_output_policy, opus_passthrough_eligibility
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'cogs/musica/runtime_telefone/agente/preparacao_audio.py'


class Buffer:
    opus_direct_capable = False

    def __init__(self, source=None, **kwargs):
        self.source = source
        self.kwargs = kwargs
        self.error = None
        self.waits = 0
        self.closed = False
        self.on_wait = None

    async def wait_ready(self, **kwargs):
        self.waits += 1
        if self.on_wait is not None:
            await self.on_wait()
        if self.error is not None:
            raise self.error

    def cleanup(self):
        self.closed = True


class OpusBuffer(Buffer):
    opus_direct_capable = True


def load_preparation():
    tree = ast.parse(SOURCE.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PreparacaoAudioMixin')
    wanted = {'_opus_direct_preflight', '_create_music_pcm_source', '_prepare_current_pcm'}
    methods = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in wanted]
    assert {node.name for node in methods} == wanted
    boundary = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, boundary], type_ignores=[]))
    reader = SimpleNamespace(from_ffmpeg=Mock())
    namespace = dict(SimpleNamespace=SimpleNamespace, GuildMusicState=GuildMusicState,
                     opus_passthrough_eligibility=opus_passthrough_eligibility,
                     OpusPCMDecoderSource=reader, BufferedOpusPCMSource=OpusBuffer,
                     BufferedPCMSource=Buffer, ArchiveSegmentedPCMSource=Buffer,
                     ContinuousArchiveDSPSource=Buffer)
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['Agent'], reader


class OpusPreparationTests(unittest.TestCase):
    def setUp(self):
        self.Agent, self.reader = load_preparation()
        self.agent = self.Agent()
        self.track = AgentTrack(audio_codec='opus', audio_sample_rate=48000, audio_channels=2,
                                audio_abr=128, stream_url='https://media.invalid/not-requested')
        self.state = GuildMusicState(1, voice_channel_id=19, volume_percent=100,
                                     playback_token=7, audio_output_settings={'opus_direct': 'auto'})
        self.agent.states = {1: self.state}
        self.agent.default_volume_percent = 55
        self.agent.audio_output_default_opus_direct = 'off'
        self.agent.client = SimpleNamespace(get_channel=Mock(return_value=SimpleNamespace(id=19, bitrate=256000)))
        self.agent._audio_output_policy = lambda voice, track, state: audio_output_policy(self.agent, voice, track, state)
        self.agent._tts_active_for_guild = Mock(return_value=False)
        self.agent._create_pcm_source = Mock(return_value=Buffer())
        self.agent._take_prepared_audio = Mock(return_value=None)
        self.agent._take_initial_audio = Mock(return_value=None)
        self.agent._starting_pcm = {}
        self.agent.ffmpeg_executable = 'ffmpeg'
        self.agent._ffmpeg_before_options_for_offset = Mock(return_value='-nostdin')
        self.agent.prepare_timeout = 30
        self.agent.log = Mock()
        self.reader.from_ffmpeg.return_value = SimpleNamespace(cleanup=Mock())

    def prepare(self):
        return asyncio.run(self.agent._prepare_current_pcm(1, self.track, 7))

    def test_default_off_preserves_pcm_path_without_looking_up_channel_or_starting_copy(self):
        self.state.audio_output_settings = {}
        result = self.prepare()
        self.assertIs(result, self.agent._create_pcm_source.return_value)
        self.agent._create_pcm_source.assert_called_once_with(self.track, effects=(0, 0, 0))
        self.agent.client.get_channel.assert_not_called()
        self.reader.from_ffmpeg.assert_not_called()
        self.assertEqual(result.waits, 1)

    def test_auto_at_normal_volume_preserves_pcm_instead_of_raising_volume(self):
        self.state.volume_percent = 55
        self.assertIs(self.prepare(), self.agent._create_pcm_source.return_value)
        self.reader.from_ffmpeg.assert_not_called()
        self.assertEqual(self.state.volume_percent, 55)

    def test_validated_buffer_is_awaited_and_returns_with_start_registry_released(self):
        result = self.prepare()
        self.assertIsInstance(result, OpusBuffer)
        self.assertEqual(result.waits, 1)
        self.assertEqual(self.agent._starting_pcm, {})
        self.agent._create_pcm_source.assert_not_called()
        self.reader.from_ffmpeg.assert_called_once_with(
            self.track.stream_url, executable='ffmpeg', before_options='-nostdin', audio_stream_index=-1)

    def test_copy_constructor_failure_marks_track_and_uses_ordinary_pcm(self):
        self.reader.from_ffmpeg.side_effect = ValueError('PRIVATE-MEDIA-URL')
        self.assertIs(self.prepare(), self.agent._create_pcm_source.return_value)
        self.assertTrue(self.track._opus_direct_failed)
        self.assertNotIn('PRIVATE-MEDIA-URL', str(self.agent.log.call_args_list))
        self.assertEqual(self.agent.log.call_args.kwargs['error_type'], 'ValueError')
        self.assertFalse(self.agent._opus_direct_preflight(1, self.track)['candidate'])

    def test_copy_startup_validation_failure_cleans_up_before_pcm_retry(self):
        invalid = OpusBuffer()
        invalid.error = ValueError('PRIVATE-HEADER')
        self.agent._take_prepared_audio.return_value = invalid
        self.assertIs(self.prepare(), self.agent._create_pcm_source.return_value)
        self.assertTrue(invalid.closed)
        self.assertTrue(self.track._opus_direct_failed)
        self.assertEqual(self.agent._starting_pcm, {})
        self.reader.from_ffmpeg.assert_not_called()
        self.assertNotIn('PRIVATE-HEADER', str(self.agent.log.call_args_list))

    def test_cancelled_copy_preparation_does_not_start_a_fallback_decoder(self):
        cancelled = OpusBuffer()
        cancelled.error = asyncio.CancelledError()
        self.agent._take_prepared_audio.return_value = cancelled
        with self.assertRaises(asyncio.CancelledError):
            self.prepare()
        self.assertTrue(cancelled.closed)
        self.agent._create_pcm_source.assert_not_called()
        self.assertEqual(self.agent._starting_pcm, {})
        self.assertFalse(getattr(self.track, '_opus_direct_failed', False))

    def test_stale_playback_failure_does_not_retry_for_old_track(self):
        stale = OpusBuffer()
        async def invalidate():
            self.state.playback_token += 1
        stale.on_wait = invalidate
        stale.error = ValueError('validation failed')
        self.agent._take_prepared_audio.return_value = stale
        with self.assertRaises(ValueError):
            self.prepare()
        self.agent._create_pcm_source.assert_not_called()
        self.assertTrue(stale.closed)
        self.assertEqual(self.agent._starting_pcm, {})

    def test_active_tts_or_unknown_channel_keeps_pcm(self):
        self.agent._tts_active_for_guild.return_value = True
        self.assertFalse(self.agent._opus_direct_preflight(1, self.track)['candidate'])
        self.agent._tts_active_for_guild.return_value = False
        self.agent.client.get_channel.return_value = None
        self.assertEqual(self.agent._opus_direct_preflight(1, self.track)['reason'], 'channel_bitrate_unknown')

    def test_ordinary_initial_source_can_be_replaced_after_explicit_opt_in(self):
        initial = Buffer()
        self.agent._take_initial_audio.return_value = initial
        result = self.prepare()
        self.assertTrue(initial.closed)
        self.assertIsInstance(result, OpusBuffer)
        self.reader.from_ffmpeg.assert_called_once()
        self.agent._create_pcm_source.assert_not_called()


if __name__ == '__main__':
    unittest.main()
