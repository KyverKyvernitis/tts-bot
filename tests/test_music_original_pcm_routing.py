"""Original-media handoff through production preparation methods.

The real routing AST runs against explicit decoder and voice-thread boundaries.
No Discord service, network stream, or audible latency claim is exercised here.
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
from unittest import mock

from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from cogs.musica.runtime_telefone.agente.ciclo_vida import consume_task_result, remove_owned_task


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'cogs/musica/runtime_telefone/agente/preparacao_audio.py'
FRAME = bytes([7, 0, 249, 255]) * 960


def load_routing():
    tree = ast.parse(SOURCE.read_text())
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == 'PreparacaoAudioMixin')
    methods = [node for node in original.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and 'original_pcm' in node.name]
    wanted = {'_install_original_pcm_request', '_request_original_pcm_handoff',
              '_cancel_original_pcm_handoff', '_original_pcm_handoff'}
    if not wanted <= {node.name for node in methods}:
        raise AssertionError('Production original-media handoff methods are missing')
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    klass = ast.ClassDef(name='Agent', bases=[], keywords=[], body=methods, decorator_list=[])
    namespace = dict(asyncio=asyncio, contextlib=contextlib, replace=replace, time=time,
                     threading=threading, SimpleNamespace=SimpleNamespace,
                     AgentTrack=AgentTrack, GuildMusicState=GuildMusicState,
                     consume_task_result=consume_task_result, remove_owned_task=remove_owned_task)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, klass], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    return namespace['Agent']


class PCM:
    def __init__(self, *, prepared=False):
        self._prepared_opus_source = prepared
        self.opus_metadata = {'pre_skip_samples': 312}
        self.closed = False
        self._closed = False
        self.last_read_had_audio = False
        self.reads = 0
        self.cleanups = 0
        self.stall_next = False
        self.eof = False
        self.wait_error = None
        self.wait_hook = None
        self.wait_entered = asyncio.Event()
        self.activated = False

    def read(self):
        self.reads += 1
        if self.eof or self.closed:
            self.last_read_had_audio = False
            return b''
        if self.stall_next:
            self.stall_next = False
            self.last_read_had_audio = False
            return bytes(3840)
        self.last_read_had_audio = True
        return FRAME

    async def wait_ready(self, **kwargs):
        self.wait_entered.set()
        if self.wait_hook is not None:
            await self.wait_hook()
        if self.wait_error is not None:
            raise self.wait_error

    def activate_playback(self):
        self.activated = True

    def cleanup(self):
        self.closed = self._closed = True
        self.cleanups += 1


class Mixer:
    """An explicit controllable next-read boundary, including a commit/cancel race."""
    def __init__(self, source):
        self.music_source = source
        self._closed = False
        self._music_ended = False
        self.music_ended = False
        self.pending = None
        self.queued = asyncio.Event()
        self.commit_on_cancel = False
        self.reset_original_pcm_request = mock.Mock(return_value=True)

    def queue_music_source_handoff(self, expected, candidate, callback, *, validate=None):
        if self.music_source is not expected or self.pending is not None:
            return False
        self.pending = (expected, candidate, callback, validate)
        self.queued.set()
        return True

    def commit(self):
        pending, self.pending = self.pending, None
        if pending is None:
            return False
        expected, candidate, callback, validate = pending
        accepted = self.music_source is expected and (validate is None or validate())
        if accepted:
            self.music_source = candidate
        callback(accepted)
        return accepted

    def cancel_music_source_handoff(self, expected, candidate):
        pending = self.pending
        if pending is None or pending[:2] != (expected, candidate):
            return False
        if self.commit_on_cancel:
            self.commit()
            return False
        self.pending = None
        pending[2](False)
        return True


class OriginalPCMClockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.Agent = load_routing()
        self.agent = self.Agent()
        self.agent._loop = asyncio.get_running_loop()
        self.agent.states = {}
        self.agent.log = mock.Mock()
        self.track = AgentTrack(stream_url='https://media.invalid/original-aac',
                                audio_codec='aac', audio_channels=2, audio_sample_rate=48000,
                                audio_abr=128, duration=180)
        self.source = PCM(prepared=True)

    async def test_clock_counts_only_consumed_audio_and_removes_initial_preskip(self):
        self.agent._install_original_pcm_request(1, self.track, self.source)
        self.assertEqual(self.source.original_pcm_position_seconds(), 0)
        self.source.read()
        self.assertAlmostEqual(self.source.original_pcm_position_seconds(), (960 - 312) / 48000)
        self.source.stall_next = True
        self.source.read()
        self.assertAlmostEqual(self.source.original_pcm_position_seconds(), (960 - 312) / 48000)
        self.source.read()
        self.assertAlmostEqual(self.source.original_pcm_position_seconds(), (1920 - 312) / 48000)
        self.source.eof = True
        self.source.read()
        self.assertAlmostEqual(self.source.original_pcm_position_seconds(), (1920 - 312) / 48000)

    async def test_snapshot_retains_original_url_and_isolated_archive_identity(self):
        self.track.archive_segments = [{'order': 0, 'sha256': 'a' * 64, 'size_bytes': 80}]
        self.track.archive_ref = {'attachment_id': 1}
        self.track.attachment_ref = {'message_id': 2}
        self.source._prepared_opus_cache_path = '/cache/generated.opus'
        self.agent._install_original_pcm_request(1, self.track, self.source)
        snapshot = self.source._original_pcm_track
        self.assertIsNot(snapshot, self.track)
        self.assertEqual(snapshot.stream_url, 'https://media.invalid/original-aac')
        self.track.stream_url = 'https://media.invalid/edited'
        self.track.archive_segments[0]['sha256'] = 'b' * 64
        self.track.archive_ref['attachment_id'] = 3
        self.track.attachment_ref['message_id'] = 4
        self.assertEqual(snapshot.stream_url, 'https://media.invalid/original-aac')
        self.assertEqual(snapshot.archive_segments[0]['sha256'], 'a' * 64)
        self.assertEqual(snapshot.archive_ref['attachment_id'], 1)
        self.assertEqual(snapshot.attachment_ref['message_id'], 2)

    async def test_voice_thread_hook_schedules_original_track_ownership_on_event_loop(self):
        self.agent._request_original_pcm_handoff = mock.Mock()
        self.agent._install_original_pcm_request(1, self.track, self.source)
        worker = threading.Thread(target=self.source.request_original_pcm, args=('tts_overlay',))
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.agent._request_original_pcm_handoff.assert_not_called()
        await asyncio.sleep(0)
        self.agent._request_original_pcm_handoff.assert_called_once_with(
            1, self.track, self.source, 'tts_overlay')

    async def test_rebinding_confirmed_track_keeps_consumed_clock_and_single_read_wrapper(self):
        self.agent._request_original_pcm_handoff = mock.Mock()
        self.track.audio_stream_index = -1
        self.agent._install_original_pcm_request(1, self.track, self.source)
        for _ in range(2):
            self.source.read()
        wrapped_read = self.source.read
        clock = self.source.original_pcm_position_seconds
        before = clock()
        confirmed = replace(self.track, audio_stream_index=2,
                            stream_url='https://media.invalid/confirmed-original-aac')
        self.agent._install_original_pcm_request(1, confirmed, self.source)
        self.assertIs(self.source.read, wrapped_read)
        self.assertIs(self.source.original_pcm_position_seconds, clock)
        self.assertEqual(clock(), before)
        self.assertIs(self.source._original_pcm_owner_track, confirmed)
        self.assertEqual(self.source._original_pcm_track.audio_stream_index, 2)
        self.assertEqual(self.source._original_pcm_track.stream_url, confirmed.stream_url)
        self.source.read()
        self.assertAlmostEqual(clock(), (3 * 960 - 312) / 48000)
        self.source.request_original_pcm('effects_require_pcm')
        await asyncio.sleep(0)
        self.agent._request_original_pcm_handoff.assert_called_once_with(
            1, confirmed, self.source, 'effects_require_pcm')


class OriginalPCMRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.Agent = load_routing()
        self.agent = self.Agent()
        self.agent._loop = asyncio.get_running_loop()
        self.agent.log = mock.Mock()
        self.agent._original_pcm_handoff_tasks = {}
        self.agent._claim_audio_prepare_slot = mock.Mock(return_value=True)
        self.agent._release_audio_prepare_slot = mock.Mock()
        self.agent._track_stream_needs_refresh = mock.Mock(return_value=False)
        self.agent._resolve_discord_attachment = mock.AsyncMock()
        self.agent._arm_music_packet_trace = mock.Mock()
        self.agent._audio_prepare_stopping = False
        self.track = AgentTrack(stream_url='https://media.invalid/original-aac',
                                audio_codec='aac', audio_channels=2, audio_sample_rate=48000,
                                audio_abr=128, duration=180)
        self.source = PCM(prepared=True)
        self.candidate = PCM()
        self.agent._create_pcm_source = mock.Mock(return_value=self.candidate)
        self.cleanup_order = []
        async def cleanup(source):
            self.cleanup_order.append(source)
            source.cleanup()
        self.agent._cleanup_audio_off_loop = cleanup
        self.mixer = Mixer(self.source)
        self.player = SimpleNamespace(source=self.mixer, is_connected=lambda: True)
        self.state = GuildMusicState(1, current=self.track, player=self.player,
                                     voice_channel_id=19, playback_token=7,
                                     transport='direct', status='playing',
                                     volume_percent=55, normal_volume_percent=55)
        self.agent.states = {1: self.state}
        self.agent._install_original_pcm_request(1, self.track, self.source)
        for _ in range(50):
            self.source.read()

    async def asyncTearDown(self):
        pending = list(self.agent._original_pcm_handoff_tasks.values())
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def request(self, reason='volume_requires_pcm'):
        self.agent._request_original_pcm_handoff(1, self.track, self.source, reason)
        return self.agent._original_pcm_handoff_tasks.get(1)

    async def queued(self):
        await asyncio.wait_for(self.mixer.queued.wait(), timeout=1)

    async def test_handoff_decodes_original_preserves_playback_identity_and_closes_old_only_after_commit(self):
        identity = self.state.playback_token, self.player, self.mixer, self.track
        task = self.request()
        self.assertIsNotNone(task)
        await self.queued()
        resumed = self.agent._create_pcm_source.call_args.args[0]
        self.assertIsNot(resumed, self.track)
        self.assertEqual(resumed.stream_url, self.track.stream_url)
        self.assertEqual(resumed.audio_codec, 'aac')
        self.assertAlmostEqual(resumed.start_offset_seconds, (50 * 960 - 312) / 48000)
        self.assertEqual(self.agent._create_pcm_source.call_args.kwargs.get('effects'), (0, 0, 0))
        self.assertFalse(self.source.closed)
        self.assertFalse(self.candidate.closed)
        self.assertTrue(self.mixer.commit())
        await task
        self.assertIs(self.mixer.music_source, self.candidate)
        self.assertTrue(self.source.closed)
        self.assertFalse(self.candidate.closed)
        self.assertEqual(identity, (self.state.playback_token, self.player, self.mixer, self.track))
        self.agent._arm_music_packet_trace.assert_called_once()
        self.assertEqual(self.agent._arm_music_packet_trace.call_args.kwargs['token'], 7)
        self.assertTrue(self.agent._arm_music_packet_trace.call_args.kwargs['output_refresh'])
        self.agent._release_audio_prepare_slot.assert_called_once()
        self.assertNotIn(1, self.agent._original_pcm_handoff_tasks)

    async def test_preparation_coalesces_multiple_controls_for_same_source(self):
        task = self.request()
        self.assertIs(self.request('tts_overlay'), task)
        self.assertIs(self.request('effects_require_pcm'), task)
        await self.queued()
        self.mixer.commit()
        await task
        self.agent._create_pcm_source.assert_called_once()

    async def test_warmed_original_catches_moving_consumer_clock_and_ignores_stall_frames(self):
        async def audio_continues():
            for _ in range(5):
                self.source.read()
            self.candidate.stall_next = True
        self.candidate.wait_hook = audio_continues
        task = self.request()
        await self.queued()
        resumed = self.agent._create_pcm_source.call_args.args[0]
        # Five 20 ms source ticks elapsed while the decoder warmed. Its first
        # synthetic stall cannot count as decoded original media.
        self.assertGreaterEqual(self.candidate.reads, 5)
        self.assertLessEqual(self.candidate.reads, 6)
        actual_position = resumed.start_offset_seconds + (self.candidate.reads - 1) * .02
        self.assertAlmostEqual(actual_position, self.source.original_pcm_position_seconds(), delta=.020001)
        self.assertTrue(self.mixer.commit())
        await task

    async def test_native_opus_and_stale_ownership_never_open_another_decoder(self):
        self.source._prepared_opus_source = False
        self.assertIsNone(self.request())
        self.source._prepared_opus_source = True
        self.state.current = AgentTrack(title='next song')
        self.assertIsNone(self.request())
        self.agent._create_pcm_source.assert_not_called()
        self.agent._claim_audio_prepare_slot.assert_not_called()

    async def test_pcm_effect_change_before_boundary_rejects_original_with_wrong_filter(self):
        task = self.request()
        await self.queued()
        self.state.nightcore_level = 1
        self.assertFalse(self.mixer.commit())
        await task
        self.assertIs(self.mixer.music_source, self.source)
        self.assertFalse(self.source.closed)
        self.assertTrue(self.candidate.closed)

    async def test_live_bassboost_change_keeps_original_candidate_because_mixer_owns_bass_dsp(self):
        task = self.request()
        await self.queued()
        self.state.bassboost_level = 3
        self.assertTrue(self.mixer.commit())
        await task
        self.assertIs(self.mixer.music_source, self.candidate)
        self.assertFalse(self.candidate.closed)

    async def test_skip_before_audio_boundary_rejects_candidate_and_preserves_healthy_source(self):
        task = self.request()
        await self.queued()
        self.state.playback_token += 1
        self.assertFalse(self.mixer.commit())
        await task
        self.assertIs(self.mixer.music_source, self.source)
        self.assertFalse(self.source.closed)
        self.assertTrue(self.candidate.closed)
        self.agent._arm_music_packet_trace.assert_not_called()
        self.agent._release_audio_prepare_slot.assert_called_once()

    async def test_decoder_timeout_keeps_prepared_audio_and_releases_quota_after_cleanup(self):
        self.candidate.wait_error = asyncio.TimeoutError('original source did not become ready')
        def release(owner):
            self.assertTrue(self.candidate.closed)
            self.assertFalse(self.source.closed)
        self.agent._release_audio_prepare_slot.side_effect = release
        task = self.request()
        await task
        self.assertIs(self.mixer.music_source, self.source)
        self.assertTrue(self.candidate.closed)
        self.assertFalse(self.source.closed)
        self.assertIsNone(self.mixer.pending)
        self.mixer.reset_original_pcm_request.assert_called_once_with(self.source)
        self.agent._release_audio_prepare_slot.assert_called_once()

    async def test_full_shared_quota_times_out_without_starting_or_releasing_someone_elses_source(self):
        self.agent._claim_audio_prepare_slot.return_value = False
        namespace = self.Agent._original_pcm_handoff.__globals__
        original_time = namespace['time']
        # Advance only this production method's deadline clock; asyncio retains
        # its real event-loop clock. No four-second blocking test is necessary.
        namespace['time'] = SimpleNamespace(monotonic=mock.Mock(side_effect=(0, 5)))
        try:
            task = self.request()
            await task
        finally:
            namespace['time'] = original_time
        self.assertIs(self.mixer.music_source, self.source)
        self.assertFalse(self.source.closed)
        self.agent._create_pcm_source.assert_not_called()
        self.agent._release_audio_prepare_slot.assert_not_called()
        self.assertEqual(self.agent.log.call_args.kwargs['outcome'], 'timeout')

    async def test_expired_attachment_is_refreshed_before_opening_original_pcm(self):
        self.source._original_pcm_track.attachment_ref = {'message_id': 20, 'attachment_id': 30}
        self.agent._track_stream_needs_refresh.return_value = True
        self.agent._resolve_discord_attachment.return_value = replace(
            self.track, stream_url='https://media.invalid/renewed-original-aac',
            stream_resolved_monotonic=1234)
        task = self.request()
        await self.queued()
        resumed = self.agent._create_pcm_source.call_args.args[0]
        self.assertEqual(resumed.stream_url, 'https://media.invalid/renewed-original-aac')
        self.assertEqual(self.track.stream_url, 'https://media.invalid/original-aac')
        self.agent._resolve_discord_attachment.assert_awaited_once()
        self.mixer.commit()
        await task

    async def test_refresh_failure_leaves_prepared_audio_running_and_logs_no_source_url(self):
        self.source._original_pcm_track.attachment_ref = {'message_id': 20}
        self.agent._track_stream_needs_refresh.return_value = True
        self.agent._resolve_discord_attachment.side_effect = RuntimeError(
            'private cookie and https://media.invalid/signed-source-url')
        task = self.request()
        await task
        self.assertIs(self.mixer.music_source, self.source)
        self.assertFalse(self.source.closed)
        self.agent._create_pcm_source.assert_not_called()
        self.agent._release_audio_prepare_slot.assert_called_once()
        logged = repr(self.agent.log.call_args_list)
        self.assertNotIn('https://', logged)
        self.assertNotIn('private cookie', logged)

    async def test_cancel_pending_handoff_disposes_candidate_without_closing_live_source(self):
        task = self.request()
        await self.queued()
        self.agent._cancel_original_pcm_handoff(1)
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(self.mixer.music_source, self.source)
        self.assertFalse(self.source.closed)
        self.assertTrue(self.candidate.closed)
        self.assertIsNone(self.mixer.pending)
        self.agent._release_audio_prepare_slot.assert_called_once()

    async def test_cancel_while_decoder_warms_keeps_old_source_and_releases_ownership(self):
        gate = asyncio.Event()
        self.candidate.wait_hook = gate.wait
        task = self.request()
        await asyncio.wait_for(self.candidate.wait_entered.wait(), timeout=1)
        self.agent._cancel_original_pcm_handoff(1)
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(self.mixer.music_source, self.source)
        self.assertFalse(self.source.closed)
        self.assertTrue(self.candidate.closed)
        self.assertFalse(self.mixer.queued.is_set())
        self.agent._release_audio_prepare_slot.assert_called_once()

    async def test_next_source_request_waits_for_cancelled_old_cleanup_then_retries_once(self):
        old_candidate = self.candidate
        cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()
        async def cleanup(source):
            self.cleanup_order.append(source)
            if source is old_candidate:
                cleanup_started.set()
                await cleanup_release.wait()
            source.cleanup()
        self.agent._cleanup_audio_off_loop = cleanup
        old_task = self.request()
        await self.queued()
        self.agent._cancel_original_pcm_handoff(1)
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        self.assertFalse(old_task.done())
        self.assertIsNone(self.mixer.pending)

        next_track = replace(self.track, title='next song',
                             stream_url='https://media.invalid/next-original-aac')
        next_source, next_candidate = PCM(prepared=True), PCM()
        self.state.current = next_track
        self.state.playback_token += 1
        self.mixer.music_source = next_source
        self.mixer.queued.clear()
        self.agent._install_original_pcm_request(1, next_track, next_source)
        self.agent._create_pcm_source.return_value = next_candidate
        self.agent._request_original_pcm_handoff(1, next_track, next_source, 'tts_overlay')
        self.agent._request_original_pcm_handoff(1, next_track, next_source, 'volume_requires_pcm')
        self.assertIs(self.agent._original_pcm_handoff_tasks[1], old_task)
        self.assertEqual(self.agent._create_pcm_source.call_count, 1)
        self.agent._release_audio_prepare_slot.assert_not_called()

        cleanup_release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        await self.queued()
        next_task = self.agent._original_pcm_handoff_tasks[1]
        self.assertIsNot(next_task, old_task)
        self.assertTrue(old_candidate.closed)
        self.assertEqual(self.agent._create_pcm_source.call_count, 2)
        self.assertEqual(self.agent._create_pcm_source.call_args.args[0].stream_url,
                         next_track.stream_url)
        self.assertEqual(self.agent._release_audio_prepare_slot.call_count, 1)
        self.assertTrue(self.mixer.commit())
        await next_task
        self.assertIs(self.mixer.music_source, next_candidate)
        self.assertFalse(next_candidate.closed)
        self.assertTrue(next_source.closed)
        self.assertEqual(self.agent._release_audio_prepare_slot.call_count, 2)
        self.assertNotIn(1, self.agent._original_pcm_handoff_tasks)

    async def test_cancel_racing_committed_handoff_never_closes_new_live_source(self):
        task = self.request()
        await self.queued()
        self.mixer.commit_on_cancel = True
        self.agent._cancel_original_pcm_handoff(1)
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(self.mixer.music_source, self.candidate)
        self.assertFalse(self.candidate.closed)
        self.assertTrue(self.source.closed)
        self.agent._release_audio_prepare_slot.assert_called_once()

    async def test_next_track_after_commit_before_callback_delivery_does_not_close_candidate_twice(self):
        task = self.request()
        await self.queued()
        self.assertTrue(self.mixer.commit())
        # Another owner can replace/clean the committed candidate before the
        # routing event loop sees the voice thread's acceptance callback.
        self.candidate.cleanup()
        self.mixer.music_source = PCM()
        self.state.playback_token += 1
        self.state.current = AgentTrack(title='next song')
        await task
        self.assertEqual(self.candidate.cleanups, 1)
        self.assertEqual(self.source.cleanups, 1)
        self.agent._arm_music_packet_trace.assert_not_called()


if __name__ == '__main__':
    unittest.main()
