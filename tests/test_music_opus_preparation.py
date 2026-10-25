"""Production routing/startup methods with buffer/FFmpeg boundaries adapted.

These stdlib checks execute the real preparation method AST. They do not
replace packet/native codec tests and do not make network or timing claims.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock

from cogs.musica.runtime_telefone.agente.configuracao import (
    audio_output_policy, effective_opus_direct_mode, opus_passthrough_eligibility,
    opus_preparation_eligibility, prepared_opus_cache_key,
)
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from tests.test_music_opus_archive_policy import archive_track


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

    @property
    def opus_metadata(self):
        return getattr(self.source, 'opus_metadata', {})


class ArchiveOpusBuffer(OpusBuffer):
    pass


def load_preparation():
    tree = ast.parse(SOURCE.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'PreparacaoAudioMixin')
    wanted = {'_opus_direct_preflight', '_opus_preparation_preflight', '_create_prepared_opus_source',
              '_install_original_pcm_request',
              '_create_music_pcm_source', '_prepare_current_pcm',
              '_archive_audio_identity', '_take_prepared_audio'}
    methods = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in wanted]
    assert {node.name for node in methods} == wanted
    boundary = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, boundary], type_ignores=[]))
    reader = SimpleNamespace(from_ffmpeg=Mock(), from_prepared_ffmpeg=Mock(),
                             from_prepared_pcm=Mock(), from_prepared_file=Mock())
    namespace = dict(SimpleNamespace=SimpleNamespace, GuildMusicState=GuildMusicState,
                     asyncio=asyncio, contextlib=contextlib, replace=replace, time=time, threading=threading,
                     ArchiveOpusPCMSource=ArchiveOpusBuffer,
                     opus_passthrough_eligibility=opus_passthrough_eligibility,
                     opus_preparation_eligibility=opus_preparation_eligibility,
                     prepared_opus_cache_key=prepared_opus_cache_key,
                     effective_opus_direct_mode=effective_opus_direct_mode,
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
        for method in (self.reader.from_prepared_ffmpeg, self.reader.from_prepared_pcm,
                       self.reader.from_prepared_file):
            method.return_value = SimpleNamespace(cleanup=Mock(), passthrough_reason='',
                opus_metadata={'prepared_opus': True, 'prepared_bitrate_kbps': 256})

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

    def archive(self):
        self.track = AgentTrack(**archive_track().__dict__, stream_url='https://media.invalid/verified')
        self.agent._loop = None
        self.agent._track_stream_needs_refresh = Mock(return_value=False)
        self.agent._resolve_archive_segment = AsyncMock(return_value={'url': 'https://media.invalid/renewed'})
        self.agent._claim_audio_prepare_slot = Mock(return_value=True)
        self.agent._release_audio_prepare_slot = Mock()
        return self.track

    def test_compatible_archive_uses_continuous_source_and_verified_first_url(self):
        self.archive()
        async def scenario():
            result = self.agent._create_music_pcm_source(1, self.track, effects=(0, 0, 0), preparing=True)
            resolve = result.kwargs['resolve_segment']
            first = await resolve(0)
            self.assertEqual(first['url'], self.track.stream_url)
            self.assertEqual(first['attachment_id'], self.track.archive_ref['attachment_id'])
            self.agent._resolve_archive_segment.assert_not_awaited()
            await resolve(1)
            self.agent._resolve_archive_segment.assert_awaited_once_with(self.track, 1, force_refresh=False)
            await resolve(0, force_refresh=True)
            self.agent._resolve_archive_segment.assert_awaited_with(self.track, 0, force_refresh=True)
            return result
        result = asyncio.run(scenario())
        self.assertIsInstance(result, ArchiveOpusBuffer)
        self.assertTrue(result.kwargs['preparing'])
        self.reader.from_ffmpeg.assert_not_called()
        self.agent._create_pcm_source.assert_not_called()

    def test_archive_fallback_uses_consumed_position_without_mutating_original_track(self):
        self.archive()
        async def scenario():
            result = self.agent._create_music_pcm_source(1, self.track, effects=(0, 0, 0), preparing=True)
            await result.kwargs['create_fallback'](.235)
            return result
        asyncio.run(scenario())
        resumed = self.agent._create_pcm_source.call_args.args[0]
        self.assertEqual(resumed.start_offset_seconds, .235)
        self.assertEqual(resumed.archive_segments, self.track.archive_segments)
        self.assertEqual(self.track.start_offset_seconds, 0)
        self.assertEqual(self.agent._create_pcm_source.call_args.kwargs,
                         {'effects': (0, 0, 0), 'preparing': False})

    def test_non_opus_archive_parts_use_continuous_pcm_then_one_preparation_encoder(self):
        self.archive()
        self.track.archive_segments[1]['audio_codec'] = 'vorbis'
        result = self.prepare()
        self.assertIsInstance(result, OpusBuffer)
        self.reader.from_prepared_pcm.assert_called_once()
        self.assertIs(self.reader.from_prepared_pcm.call_args.args[0], self.agent._create_pcm_source.return_value)
        self.assertTrue(result._prepared_opus_source)
        self.reader.from_ffmpeg.assert_not_called()

    def test_reader_keeps_manifest_snapshot_when_queue_metadata_is_refreshed(self):
        self.archive()
        previous_hash = self.track.archive_segments[1]['sha256']
        async def scenario():
            result = self.agent._create_music_pcm_source(1, self.track, effects=(0, 0, 0))
            self.track.archive_segments[1]['sha256'] = 'f' * 64
            self.track.archive_ref['manifest_attachment_id'] = 99
            await result.kwargs['resolve_segment'](1)
            return result
        result = asyncio.run(scenario())
        resolved_track = self.agent._resolve_archive_segment.await_args.args[0]
        self.assertIsNot(resolved_track, self.track)
        self.assertEqual(resolved_track.archive_segments[1]['sha256'], previous_hash)
        self.assertNotIn('manifest_attachment_id', resolved_track.archive_ref)
        self.assertEqual(result.kwargs['segments'][1]['sha256'], previous_hash)

    def test_warm_archive_copy_is_discarded_when_manifest_identity_changes(self):
        for field, replacement in (('sha256', 'f' * 64), ('size_bytes', 999),
                                   ('filename', 'renamed.ogg'), ('audio_codec', 'vorbis'), ('order', 10)):
            with self.subTest(field=field):
                self.archive()
                old_identity = self.Agent._archive_audio_identity(self.track)
                source = SimpleNamespace(cleanup=Mock(), activate_playback=Mock(),
                                         audio_buffer_metrics=Mock(return_value={}))
                prepared = SimpleNamespace(ready=True, item_id=self.track.queue_item_id,
                                           effects=(0, 0, 0), stream_url=self.track.stream_url,
                                           offset=0, created_at=time.monotonic(), source=source,
                                           archive_identity=old_identity)
                self.agent._prepared_audio = {1: prepared}
                self.agent._cancel_audio_preparation = Mock()
                self.track.archive_segments[1][field] = replacement
                actual = self.Agent._take_prepared_audio(self.agent, 1, self.track)
                self.assertIsNone(actual)
                source.cleanup.assert_called_once()
                source.activate_playback.assert_not_called()

    def test_matching_warm_archive_identity_reuses_its_ready_source(self):
        self.archive()
        source = SimpleNamespace(cleanup=Mock(), activate_playback=Mock(),
                                 audio_buffer_metrics=Mock(return_value={}))
        self.agent._prepared_audio = {1: SimpleNamespace(
            ready=True, item_id=self.track.queue_item_id, effects=(0, 0, 0), stream_url=self.track.stream_url,
            offset=0, created_at=time.monotonic(), source=source,
            archive_identity=self.Agent._archive_audio_identity(self.track))}
        self.agent._cancel_audio_preparation = Mock()
        self.assertIs(self.Agent._take_prepared_audio(self.agent, 1, self.track), source)
        source.activate_playback.assert_called_once()
        source.cleanup.assert_not_called()

    def test_preflight_uses_channel_copy_capacity_independent_of_encoder_target(self):
        self.track.audio_abr = 200
        self.agent._audio_output_policy = Mock(return_value={'bitrate_kbps': 96, 'copy_bitrate_kbps': 256})
        self.assertTrue(self.agent._opus_direct_preflight(1, self.track)['candidate'])

    def test_copy_constructor_failure_marks_track_and_prepares_one_derivative(self):
        self.reader.from_ffmpeg.side_effect = ValueError('PRIVATE-MEDIA-URL')
        self.assertIsInstance(self.prepare(), OpusBuffer)
        self.reader.from_prepared_ffmpeg.assert_called_once()
        self.agent._create_pcm_source.assert_not_called()
        self.assertTrue(self.track._opus_direct_failed)
        self.assertNotIn('PRIVATE-MEDIA-URL', str(self.agent.log.call_args_list))
        self.assertEqual(self.agent.log.call_args.kwargs['error_type'], 'ValueError')
        self.assertFalse(self.agent._opus_direct_preflight(1, self.track)['candidate'])

    def test_copy_startup_validation_failure_cleans_up_before_prepared_retry(self):
        invalid = OpusBuffer()
        invalid.error = ValueError('PRIVATE-HEADER')
        self.agent._take_prepared_audio.return_value = invalid
        self.assertIsInstance(self.prepare(), OpusBuffer)
        self.reader.from_prepared_ffmpeg.assert_called_once()
        self.agent._create_pcm_source.assert_not_called()
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

    def aac(self):
        self.track.audio_codec = 'aac'
        self.track.audio_sample_rate = 44100
        self.track.audio_channels = 1
        self.track.audio_format_id = '140'
        self.track.webpage_url = 'https://www.youtube.com/watch?v=abcdefghijk'
        self.track.duration = 195
        return self.track

    def cache(self):
        cache = Mock()
        cache.get.return_value = None
        cache.writer.return_value = None
        self.agent.prepared_opus_cache = cache
        return cache

    def warm_prepared(self, *, error=None, cache_hit=False):
        raw = SimpleNamespace(opus_metadata={'prepared_opus': True, 'prepared_bitrate_kbps': 256})
        source = OpusBuffer(raw)
        source._prepared_opus_source = True
        source._prepared_opus_cache_hit = cache_hit
        source._prepared_opus_cache_key = prepared_opus_cache_key(self.track, 256)
        source.error = error
        self.agent._take_prepared_audio.return_value = source
        return source

    def test_aac_mono_44100_stream_is_prepared_at_capacity_without_mutating_track(self):
        self.aac()
        before = self.track.public()
        result = self.prepare()
        self.assertTrue(result._prepared_opus_source)
        self.reader.from_prepared_ffmpeg.assert_called_once_with(
            self.track.stream_url, bitrate_kbps=256, source_codec='aac', executable='ffmpeg',
            before_options='-nostdin', audio_stream_index=-1, cache_writer=None,
            resample_filter='', fec=True, expected_packet_loss=0.15)
        self.reader.from_ffmpeg.assert_not_called()
        self.agent._create_pcm_source.assert_not_called()
        self.assertEqual(self.track.public(), before)

    def test_prepared_source_preserves_quality_resampling_and_requested_protection(self):
        self.aac()
        quality_filter = 'aresample=48000:resampler=swr:filter_size=64:phase_shift=10:linear_interp=1:exact_rational=1:filter_type=kaiser'
        self.agent._ffmpeg_quality_resample_filter = lambda: quality_filter
        self.state.audio_output_settings.update(fec=False, expected_packet_loss=.05)
        self.prepare()
        settings = self.reader.from_prepared_ffmpeg.call_args.kwargs
        self.assertEqual(settings['resample_filter'], quality_filter)
        self.assertFalse(settings['fec'])
        self.assertEqual(settings['expected_packet_loss'], .05)

    def test_ready_cache_is_used_before_opening_source_or_encoder(self):
        self.aac()
        cache = self.cache()
        cache.get.return_value = '/private/cache/validated.ogg'
        result = self.prepare()
        self.assertTrue(result._prepared_opus_cache_hit)
        args, values = self.reader.from_prepared_file.call_args
        self.assertEqual(args, ('/private/cache/validated.ogg',))
        self.assertEqual(values['bitrate_kbps'], 256)
        self.assertEqual(values['source_codec'], 'aac')
        values['on_error']()
        cache.invalidate.assert_called_with(prepared_opus_cache_key(self.track, 256))
        cache.writer.assert_not_called()
        self.reader.from_prepared_ffmpeg.assert_not_called()
        self.agent._create_pcm_source.assert_not_called()

    def test_cold_preparation_passes_cache_writer_without_waiting_for_complete_file(self):
        self.aac()
        cache = self.cache()
        writer = Mock()
        cache.writer.return_value = writer
        result = self.prepare()
        self.assertFalse(result._prepared_opus_cache_hit)
        self.assertIs(self.reader.from_prepared_ffmpeg.call_args.kwargs['cache_writer'], writer)
        writer.commit.assert_not_called()  # only the validating source may commit EOF
        self.assertEqual(result.waits, 1)

    def test_cache_unavailable_or_writer_failure_does_not_prevent_streaming_audio(self):
        for failure in ('get', 'writer'):
            with self.subTest(failure=failure):
                self.setUp()
                self.aac()
                cache = self.cache()
                getattr(cache, failure).side_effect = OSError('PRIVATE-CACHE-PATH')
                self.assertIsInstance(self.prepare(), OpusBuffer)
                self.reader.from_prepared_ffmpeg.assert_called_once()
                self.assertNotIn('PRIVATE-CACHE-PATH', str(self.agent.log.call_args_list))

    def test_bad_cache_constructor_is_invalidated_before_cold_prepare(self):
        self.aac()
        cache = self.cache()
        cache.get.return_value = '/private/cache/invalid.ogg'
        self.reader.from_prepared_file.side_effect = ValueError('PRIVATE-CACHE-HEADER')
        result = self.prepare()
        self.assertFalse(result._prepared_opus_cache_hit)
        cache.invalidate.assert_called_once_with(prepared_opus_cache_key(self.track, 256))
        self.reader.from_prepared_ffmpeg.assert_called_once()
        self.assertNotIn('PRIVATE-CACHE', str(self.agent.log.call_args_list))

    def test_bad_ready_cached_packets_invalidate_and_retry_cold_source_once(self):
        self.aac()
        cache = self.cache()
        invalid = self.warm_prepared(error=ValueError('PRIVATE-INVALID-CACHE'), cache_hit=True)
        result = self.prepare()
        self.assertTrue(invalid.closed)
        self.assertFalse(result._prepared_opus_cache_hit)
        cache.invalidate.assert_called_once_with(prepared_opus_cache_key(self.track, 256))
        cache.get.assert_not_called()
        self.reader.from_prepared_ffmpeg.assert_called_once()
        self.assertFalse(getattr(self.track, '_opus_prepared_failed', False))
        self.assertNotIn('PRIVATE-INVALID', str(self.agent.log.call_args_list))

    def test_failed_cold_preparation_uses_original_pcm_without_reencoding_loop(self):
        self.aac()
        invalid = self.warm_prepared(error=ValueError('invalid prepared packets'))
        self.assertIs(self.prepare(), self.agent._create_pcm_source.return_value)
        self.assertTrue(invalid.closed)
        self.assertTrue(self.track._opus_prepared_failed)
        self.reader.from_prepared_ffmpeg.assert_not_called()
        self.agent._create_pcm_source.assert_called_once_with(self.track, effects=(0, 0, 0))

    def test_original_40ms_packets_are_prepared_before_first_voice_packet(self):
        invalid = OpusBuffer(SimpleNamespace(passthrough_reason='packet_duration'))
        self.agent._take_prepared_audio.return_value = invalid
        result = self.prepare()
        self.assertTrue(invalid.closed)
        self.assertTrue(result._prepared_opus_source)
        self.assertTrue(self.track._opus_direct_failed)
        self.reader.from_prepared_ffmpeg.assert_called_once()
        self.agent._create_pcm_source.assert_not_called()

    def test_prepared_warm_source_is_rebuilt_if_channel_capacity_changes(self):
        self.aac()
        warmed = self.warm_prepared()
        self.agent.client.get_channel.return_value.bitrate = 96000
        result = self.prepare()
        self.assertTrue(warmed.closed)
        self.assertTrue(result._prepared_opus_source)
        self.assertEqual(self.reader.from_prepared_ffmpeg.call_args.kwargs['bitrate_kbps'], 96)

    def test_reused_initial_prepared_source_rebinds_confirmed_track_and_stream_index(self):
        self.aac()
        async def scenario():
            self.agent._loop = asyncio.get_running_loop()
            provisional = replace(self.track, audio_stream_index=-1)
            source = self.agent._create_prepared_opus_source(1, provisional)
            confirmed = replace(provisional, audio_stream_index=2)
            self.state.current = confirmed
            self.agent._take_initial_audio.return_value = source
            self.agent._request_original_pcm_handoff = Mock()
            result = await self.agent._prepare_current_pcm(1, confirmed, 7)
            self.assertIs(result, source)
            self.assertFalse(source.closed)
            self.assertIs(source._original_pcm_owner_track, confirmed)
            self.assertEqual(source._original_pcm_track.audio_stream_index, 2)
            self.assertEqual(provisional.audio_stream_index, -1)
            source.request_original_pcm('volume_requires_pcm')
            await asyncio.sleep(0)
            self.agent._request_original_pcm_handoff.assert_called_once_with(
                1, confirmed, source, 'volume_requires_pcm')
        asyncio.run(scenario())
        self.reader.from_prepared_ffmpeg.assert_called_once()
        self.agent._create_pcm_source.assert_not_called()

    def test_processing_guards_prevent_aac_conversion_and_preserve_original_pcm(self):
        for guard in ('volume', 'tts', 'off', 'seek', 'live', 'filter', 'unknown_channel'):
            with self.subTest(guard=guard):
                self.setUp()
                self.aac()
                if guard == 'volume': self.state.volume_percent = 55
                if guard == 'tts': self.agent._tts_active_for_guild.return_value = True
                if guard == 'off': self.state.audio_output_settings = {'opus_direct': 'off'}
                if guard == 'seek': self.track.start_offset_seconds = 10
                if guard == 'live': self.track.is_live = True
                if guard == 'filter': self.agent.ffmpeg_options = '-af volume=0.5'
                if guard == 'unknown_channel': self.agent.client.get_channel.return_value = None
                self.assertIs(self.prepare(), self.agent._create_pcm_source.return_value)
                self.reader.from_prepared_ffmpeg.assert_not_called()
                self.reader.from_prepared_pcm.assert_not_called()

    def test_cancelled_prepared_cache_does_not_start_another_source(self):
        self.aac()
        cancelled = self.warm_prepared(error=asyncio.CancelledError(), cache_hit=True)
        with self.assertRaises(asyncio.CancelledError):
            self.prepare()
        self.assertTrue(cancelled.closed)
        self.reader.from_prepared_ffmpeg.assert_not_called()
        self.agent._create_pcm_source.assert_not_called()
        self.assertFalse(getattr(self.track, '_opus_prepared_failed', False))

    def test_replaced_session_during_failed_preparation_never_starts_another_source(self):
        self.aac()
        stale = self.warm_prepared(error=ValueError('stale'))
        async def replace_session():
            self.agent.states[1] = GuildMusicState(1, playback_token=7)
        stale.on_wait = replace_session
        with self.assertRaises(ValueError):
            self.prepare()
        self.reader.from_prepared_ffmpeg.assert_not_called()
        self.agent._create_pcm_source.assert_not_called()

    def test_speculative_first_audio_never_commits_unconfirmed_stream_to_cache(self):
        self.aac()
        self.track.audio_stream_index = 7
        self.track.attachment_ref = {'attachment_id': 4}
        cache = self.cache()
        result = self.agent._create_music_pcm_source(1, self.track, first_audio_stream=True)
        self.assertTrue(result._prepared_opus_source)
        self.assertEqual(self.reader.from_prepared_ffmpeg.call_args.kwargs['audio_stream_index'], -1)
        self.assertIsNone(self.reader.from_prepared_ffmpeg.call_args.kwargs['cache_writer'])
        cache.get.assert_not_called()
        cache.writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
