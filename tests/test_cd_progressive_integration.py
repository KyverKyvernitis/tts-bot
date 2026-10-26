"""Progressive CD orchestration contracts without Discord or native voicebanks."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import logging
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from test_cd_arrangement_audio_contract import ContractProbe, Probe, QueueItem, queued
from osaka_cd.lyrics import prepare_message_arrangement
from cogs.tts.cd_stream import PCMStreamBuffer

ROOT = Path(__file__).resolve().parents[1]


def audio_method(name, **extra):
    tree = ast.parse((ROOT / 'cogs/tts/audio.py').read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == 'TTSAudioMixin')
    method = next(node for node in owner.body if getattr(node, 'name', '') == name)
    namespace = dict(globals(), **extra)
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'production_cd_audio_hook', 'exec'), namespace)
    return namespace[name]


def measured_source_class():
    tree = ast.parse((ROOT / 'cogs/tts/audio.py').read_text())
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == '_FirstFrameAudioSource')
    namespace = dict(globals(), _DiscordAudioSourceBase=object)
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), klass], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'real_first_frame_source', 'exec'), namespace)
    return namespace['_FirstFrameAudioSource']


def long_item():
    item = queued()
    item.text = 'a a a a a a'
    item.ttv_arrangement['duration_ms'] = 6000
    for part in item.ttv_arrangement['parts']:
        pitch = part['notes'][0]['midi_note']
        part['notes'] = [dict(midi_note=pitch, start_ms=number * 1000,
                              duration_ms=1000, velocity=100) for number in range(6)]
    item.ttv_arrangement_hash = ''
    return item


def controlled_compositor(arrangement, text, render_part, emit_pcm, **options):
    """Exercise real HTTP-delegation ownership with two physical requests.

    The production compositor has its own waveform tests; this fixture makes
    the boundary between usable PCM and unfinished native work deterministic.
    """
    score = {'version': 1, 'track_index': 0, 'duration_ms': 3000,
             'lyrics_source': 'message', 'notes': [
                 {'midi_note': 60, 'start_ms': 0, 'duration_ms': 3000, 'velocity': 100}]}
    call = dict(score=score, text='a', voice_id='kasane-teto', language='pt-BR',
                pitch=0.0, rate=1.0, part_id='lead', role='lead')
    render_part(**call)
    emit_pcm(struct.pack('<hh', 1000, 1000) * (3000 * 48))
    render_part(**call)
    cancelled = options.get('cancelled')
    if cancelled is not None and cancelled():
        raise RuntimeError('render_cancelled')
    emit_pcm(struct.pack('<hh', 2000, 2000) * (3350 * 48))
    return {'duration_ms': 6350, 'musical_duration_ms': 6000, 'sample_rate': 48000,
            'channels': 2, 'sample_width_bytes': 2, 'midi_preroll_ms': 350,
            'audio_bytes': 6350 * 192, 'stems': [], 'timing_ms': {},
            'first_nonzero_offset_ms': 0}


class ProgressiveProbe(Probe):
    def __init__(self, directory):
        super().__init__(directory)
        self.bot = SimpleNamespace(audio_router=None, get_guild=lambda _: None)
        self.guild_states = {}
        self.block_call = 0
        self._tts_background_tasks = set()

    def _get_edge_stream_handles(self):
        return {}

    def _tts_agent_route_available(self):
        return True

    def _tts_agent_should_try_worker(self, item):
        return True, 'fixture'

    def _is_music_active_for_guild(self, guild_id):
        return False

    def _get_voice_client_for_guild(self, guild):
        return None

    def _schedule_tts_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tts_background_tasks.add(task)
        task.add_done_callback(self._tts_background_tasks.discard)
        return task

    async def _generate_tts_agent_worker_file(self, item):
        self.calls.append(item)
        if self.block_call and len(self.calls) == self.block_call:
            self.started.set()
            await asyncio.to_thread(self.release.wait, 5)
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as output:
            output.setparams((1, 2, 48000, 0, 'NONE', ''))
            output.writeframes(struct.pack('<h', 2000) * (item.ttv_score['duration_ms'] * 48))
        item._tts_agent_render_metadata = {'midi_preroll_ms': 0, 'normalization_gain': 1.0}
        path = self._make_runtime_temp_file('.wav')
        Path(path).write_bytes(raw.getvalue())
        return path


class ProgressiveOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.probe = ProgressiveProbe(self.temporary.name)
        self.environ = patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': self.temporary.name,
            'OSAKA_CD_STREAMING_ENABLED': 'true'})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    async def asyncTearDown(self):
        self.probe.release.set()
        for task in list(self.probe._tts_background_tasks):
            if not task.done():
                task.cancel()
        if self.probe._tts_background_tasks:
            await asyncio.gather(*self.probe._tts_background_tasks, return_exceptions=True)

    async def test_route_rejects_missing_arrangement_and_shutdown(self):
        self.assertTrue(self.probe._cd_streaming_allowed_for(long_item())[0])
        item = long_item()
        item.ttv_arrangement = None
        self.assertFalse(self.probe._cd_streaming_allowed_for(item)[0])
        self.probe._tts_shutting_down = True
        self.assertFalse(self.probe._cd_streaming_allowed_for(long_item())[0])

    async def test_remote_music_route_keeps_complete_verified_file_contract(self):
        self.probe.bot.audio_router = SimpleNamespace(
            should_route_tts_to_music_agent=lambda *_: True,
            should_allow_local_tts_streaming=lambda *_args, **_kwargs: False,
            is_music_active=lambda *_: True)
        self.assertFalse(self.probe._cd_streaming_allowed_for(long_item())[0])

    async def test_voice_owner_lookup_receives_guild_object(self):
        guild = SimpleNamespace(id=1, voice_client=None)
        self.probe.bot.get_guild = lambda guild_id: guild if guild_id == 1 else None
        observed = []
        def accessor(value):
            self.assertIs(value, guild)
            observed.append(value.id)
            return value.voice_client
        self.probe._get_voice_client_for_guild = accessor
        self.assertTrue(self.probe._cd_streaming_allowed_for(long_item())[0])
        self.assertEqual(observed, [1])

    async def test_real_compositor_starts_with_all_simultaneous_voices(self):
        item = long_item()
        # A later phrase with distinct notes must require physical work rather
        # than legitimately reusing the first phrase's identical stem cache.
        for note in item.ttv_arrangement['parts'][0]['notes'][3:]:
            note['midi_note'] = 62
        item.ttv_arrangement = prepare_message_arrangement(item.ttv_arrangement, item.text)
        item.ttv_arrangement_hash = ''
        self.probe.block_call = 3
        handle = await asyncio.wait_for(self.probe._prepare_cd_pcm_stream(
            SimpleNamespace(queue=asyncio.Queue()), item), 5)
        try:
            self.assertTrue(await asyncio.to_thread(self.probe.started.wait, 1))
            first = self.probe.calls[:2]
            self.assertEqual({call.ttv_score['notes'][0]['midi_note'] for call in first}, {60, 64})
            self.assertEqual(first[0].ttv_score['duration_ms'], 3000)
            self.assertFalse(handle.producer_task.done())
            # Both equal-amplitude fixture voices contribute to the stable
            # mixture: normalized part weights sum to the global0.92 headroom.
            pcm = bytearray()
            for _ in range(30):
                pcm.extend(handle.buffer.read())
            samples = struct.unpack('<' + 'h' * (len(pcm) // 2), pcm)
            self.assertIn(1840, samples)
            self.assertNotIn(0, samples[40000:])
        finally:
            self.probe.release.set()
            await self.probe._finalize_cd_pcm_stream(handle, cancel=True)

    async def test_first_audio_returns_before_later_physical_render_finishes(self):
        self.probe.block_call = 2
        with patch('cogs.tts.cd_audio.stream_arrangement', side_effect=controlled_compositor):
            handle = await asyncio.wait_for(self.probe._prepare_cd_pcm_stream(
                SimpleNamespace(queue=asyncio.Queue()), long_item()), 3)
            try:
                self.assertTrue(await asyncio.to_thread(self.probe.started.wait, 1))
                self.assertFalse(handle.producer_task.done())
                self.assertTrue(self.probe._cd_composition_gate.locked())
                self.assertIs(self.probe._cd_stream_for_path(handle.path), handle)
                # It is an identity/lifetime marker, never complete encoded audio.
                self.assertEqual(Path(handle.path).stat().st_size, 0)
                frame = handle.buffer.read()
                self.assertEqual(len(frame), 3840)
                self.assertTrue(any(frame))
            finally:
                self.probe.release.set()
                await self.probe._finalize_cd_pcm_stream(handle, cancel=True)
            self.assertIsNone(self.probe._cd_stream_for_path(handle.path))
            self.assertFalse(Path(handle.path).exists())

    async def test_cancellation_retains_gate_until_native_request_drains(self):
        self.probe.block_call = 2
        with patch('cogs.tts.cd_audio.stream_arrangement', side_effect=controlled_compositor):
            handle = await asyncio.wait_for(self.probe._prepare_cd_pcm_stream(
                SimpleNamespace(queue=asyncio.Queue()), long_item()), 3)
            self.assertTrue(await asyncio.to_thread(self.probe.started.wait, 1))
            finalizer = asyncio.create_task(self.probe._finalize_cd_pcm_stream(handle, cancel=True))
            try:
                await asyncio.sleep(.05)
                self.assertFalse(finalizer.done())
                self.assertTrue(self.probe._cd_composition_gate.locked())
                self.assertTrue(handle.cancelled.is_set())
            finally:
                self.probe.release.set()
                await asyncio.wait_for(finalizer, 3)
            self.assertFalse(self.probe._cd_composition_gate.locked())
            self.assertEqual(self.probe._cd_foreground_compositions, 0)

    async def test_speculative_message_waits_current_physical_producer(self):
        self.probe.block_call = 2
        with patch('cogs.tts.cd_audio.stream_arrangement', side_effect=controlled_compositor):
            current = long_item()
            handle = await asyncio.wait_for(self.probe._prepare_cd_pcm_stream(
                SimpleNamespace(queue=asyncio.Queue()), current), 3)
            self.assertTrue(await asyncio.to_thread(self.probe.started.wait, 1))
            following = long_item()
            following._tts_prefetch = True
            following._tts_foreground_event = asyncio.Event()
            waiter = asyncio.create_task(self.probe._wait_cd_prefetch_turn(following))
            try:
                await asyncio.sleep(.05)
                self.assertFalse(waiter.done())
                self.probe.release.set()
                await asyncio.wait_for(waiter, 3)
            finally:
                self.probe.release.set()
                await self.probe._finalize_cd_pcm_stream(handle, cancel=True)

    async def test_marker_cannot_be_serialized_as_prebuilt_remote_audio(self):
        marker = Path(self.temporary.name) / 'identity.cdpcm'
        marker.touch()
        probe = ContractProbe(self.temporary.name)
        probe.direct_cache = str(marker)
        payload = {}
        with self.assertRaises(RuntimeError):
            await probe._maybe_attach_prebuilt_direct_tts_audio(payload, queued(), generate_if_missing=False)
        self.assertNotIn('audio_b64', payload)
        self.assertNotIn('prebuilt_audio', payload)

    async def test_cancelled_prefetch_waiter_does_not_wait_for_other_native_work(self):
        current = long_item()
        following = long_item()
        following._tts_prefetch = True
        following._cd_stream_path = 'following.cdpcm'
        blocked = asyncio.create_task(asyncio.sleep(60))
        buffer = PCMStreamBuffer(6350)
        own = SimpleNamespace(item=following, buffer=buffer, cancelled=threading.Event(), producer_task=None)
        self.probe._cd_pcm_handles = {
            os.path.abspath('current.cdpcm'): SimpleNamespace(item=current, producer_task=blocked),
            os.path.abspath(following._cd_stream_path): own,
        }
        waiter = asyncio.create_task(self.probe._wait_cd_prefetch_turn(following))
        try:
            await asyncio.sleep(.03)
            self.assertFalse(waiter.done())
            own.cancelled.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiter, .5)
            self.assertFalse(blocked.done(), 'cancelling speculation cannot cancel the current song')
        finally:
            blocked.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await blocked
            buffer.cleanup()
            self.probe._cd_pcm_handles.clear()

    async def test_stream_playback_failure_after_frame_is_never_replayed(self):
        method = audio_method('_play_file_with_recovery')
        handle = SimpleNamespace(buffer=SimpleNamespace(frames_read=1))
        error = RuntimeError('producer failed after audible frame')
        error._tts_first_frame_observed = True
        play = AsyncMock(side_effect=error)
        owner = SimpleNamespace(_cd_stream_for_path=lambda _: handle,
            _edge_stream_handle_for_path=lambda _: None,
            _play_file=play, guild_states={},
            _is_tts_audio_source_error=lambda _: False,
            _is_voice_disconnected_error=lambda _: False,
            _is_music_active_for_guild=lambda _: False,
            _voice_client_is_connected=lambda _: True,
            _tts_local_complete_file_after_stream_failure=AsyncMock())
        with self.assertRaisesRegex(RuntimeError, 'producer failed'):
            await method(owner, SimpleNamespace(id=1), queued(), SimpleNamespace(), 'identity.cdpcm')
        self.assertEqual(play.await_count, 1)
        owner._tts_local_complete_file_after_stream_failure.assert_not_awaited()

    async def test_local_pcm_skips_voice_handoff_before_remote_side_effects(self):
        method = audio_method('_try_worker_voice_direct_tts')
        availability = Mock(side_effect=AssertionError('local PCM must not transfer voice ownership'))
        owner = SimpleNamespace(_cd_streaming_allowed_for=lambda _: (True, 'local_pcm'),
                                _worker_voice_direct_tts_available_for=availability)
        self.assertIsNone(await method(owner, SimpleNamespace(id=1), queued()))
        availability.assert_not_called()

    async def test_pcm_is_resolved_before_legacy_worker_stream_protocol(self):
        method = audio_method('_resolve_audio_path')
        handle = SimpleNamespace(path='identity.cdpcm')
        owner = SimpleNamespace(_snapshot_tts_item=Mock(),
            _cd_streaming_allowed_for=lambda _: (True, 'local_pcm'),
            _try_get_cached_path=lambda *_: None,
            _prepare_cd_pcm_stream=AsyncMock(return_value=handle),
            _get_item_normalized_cache_text=Mock(side_effect=AssertionError('legacy protocol must not gate local PCM')))
        result = await method(owner, SimpleNamespace(), queued(), allow_edge_stream=False)
        self.assertEqual(result, ('identity.cdpcm', True))
        owner._get_item_normalized_cache_text.assert_not_called()

    async def test_pcm_source_primes_without_reading_or_starting_ffmpeg(self):
        source = SimpleNamespace(read=Mock(side_effect=AssertionError('audible clock starts in player')))
        handle = SimpleNamespace()
        owner = SimpleNamespace(_cd_stream_for_path=lambda _: handle,
            _make_discord_tts_source_for_item=lambda *_: (source, 'cd_pcm_ram'))
        method = audio_method('_resolve_and_prime_audio',
            _PreparedTTSPlayback=lambda **values: SimpleNamespace(**values))
        task = asyncio.get_running_loop().create_future()
        task.set_result(('identity.cdpcm', True))
        path, cleanup, prepared = await method(owner, task, queued())
        self.assertEqual(path, 'identity.cdpcm')
        self.assertTrue(cleanup)
        self.assertIs(prepared.source, source)
        self.assertEqual(prepared.source_kind, 'cd_pcm_ram')
        source.read.assert_not_called()

    async def test_discard_uses_physical_stream_finalizer_before_file_leases(self):
        handle = SimpleNamespace()
        owner = SimpleNamespace(_cd_stream_for_path=lambda _: handle,
            _finalize_cd_pcm_stream=AsyncMock(),
            _edge_stream_handle_for_path=Mock(side_effect=AssertionError('CD has its own lifecycle')),
            _audio_leases=Mock(side_effect=AssertionError('marker is not a complete cached file')))
        method = audio_method('_discard_edge_stream_path')
        await method(owner, 'identity.cdpcm')
        owner._finalize_cd_pcm_stream.assert_awaited_once_with(handle, cancel=True)

    async def test_actual_playback_hook_consumes_pcm_and_measures_audible_frame(self):
        buffer = PCMStreamBuffer(40, prebuffer_ms=0, safety_ms=0)
        buffer.append(bytes(3840), generation_ms=0)
        buffer.append(struct.pack('<hh', 1000, -1000) * 960, generation_ms=0)
        buffer.finish()
        self.addCleanup(buffer.cleanup)
        handle = SimpleNamespace(buffer=buffer)
        collected = []
        class Voice:
            guild = SimpleNamespace(id=1)
            def play(self, source, *, after):
                self.source = source
                def consume():
                    error = None
                    try:
                        while frame := source.read():
                            collected.append(frame)
                            time.sleep(.002)
                    except Exception as failure:
                        error = failure
                    finally:
                        after(error)
                self.thread = threading.Thread(target=consume, daemon=True)
                self.thread.start()
        voice = Voice()
        owner = SimpleNamespace(_get_tts_playback_lock=lambda _: asyncio.Lock(),
            _edge_stream_handle_for_path=lambda _: None,
            _cd_stream_for_path=lambda _: handle,
            _wait_until_voice_playable_for_tts=AsyncMock(),
            _make_discord_tts_source_for_item=lambda *_: (buffer.as_audio_source(object), 'cd_pcm_ram'),
            _effective_playback_timeout=lambda *_: 1,
            _record_latency_sample=Mock(), _path_audio_format=lambda _: 'cdpcm',
            _finalize_cd_pcm_stream=AsyncMock())
        method = audio_method('_play_file', _FirstFrameAudioSource=measured_source_class())
        result = await asyncio.wait_for(method(owner, voice, 'identity.cdpcm', item=queued()), 3)
        voice.thread.join(1)
        self.assertFalse(voice.thread.is_alive())
        self.assertEqual(len(collected), 2)
        self.assertEqual(collected[0], bytes(3840))
        self.assertTrue(any(collected[1]))
        self.assertTrue(result['cd_pcm_stream'])
        self.assertEqual(result['audio_format'], 'pcm_s16le')
        self.assertGreater(result['first_audible_at'], result['first_frame_at'])
        self.assertIn('message_to_first_audible_ms', result)
        owner._finalize_cd_pcm_stream.assert_awaited_once_with(handle, cancel=False)


if __name__ == '__main__':
    unittest.main()
