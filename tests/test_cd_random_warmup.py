"""Speculation follows the personal shuffle cycle without consuming a choice."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from cogs.tts import arrangement_audio, random_cd
from cogs.tts.arrangement_audio import execution_arrangement
from osaka_cd.lyrics import prepare_message_arrangement
from osaka_cd.progressive import DecodedPCMCache, stream_arrangement
from osaka_cd.render import warm_arrangement_stems
from test_cd_arrangement_audio_contract import Probe, QueueItem, arrangement, queued


def pcm_renderer(calls):
    def render(**request):
        calls.append(request)
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as output:
            output.setparams((1, 2, 48000, 0, 'NONE', ''))
            output.writeframes(b'\x10\x02' * (request['score']['duration_ms'] * 48))
        return raw.getvalue(), {'midi_preroll_ms': 0, 'normalization_gain': 1.0}
    return render


class RemainingShuffleTests(unittest.TestCase):
    def setUp(self):
        self.pool = tuple(SimpleNamespace(id=identity) for identity in ('one', 'two', 'three'))
        self.cog = SimpleNamespace(_random_cd_cycles=OrderedDict({
            (1, 2): random_cd._RandomCDCycle({'one'}, 'one')}))

    def test_speculation_reads_remaining_without_rng_or_history_mutation(self):
        before = deepcopy(self.cog._random_cd_cycles)
        with patch.object(random_cd, 'available_random_melodies', return_value=self.pool), \
                patch.object(random_cd.random, 'choice', side_effect=AssertionError('not a draw')):
            self.assertEqual([entry.id for entry in random_cd.remaining_random_melodies(
                self.cog, 1, 2)], ['two', 'three'])
            self.assertEqual([entry.id for entry in random_cd.remaining_random_melodies(
                self.cog, 1, 3)], ['one', 'two', 'three'])
        self.assertEqual(self.cog._random_cd_cycles, before)

    def test_exhausted_cycle_warms_all_without_resetting_it(self):
        self.cog._random_cd_cycles[(1, 2)].played.update({'two', 'three'})
        before = deepcopy(self.cog._random_cd_cycles)
        with patch.object(random_cd, 'available_random_melodies', return_value=self.pool):
            self.assertEqual(random_cd.remaining_random_melodies(self.cog, 1, 2), self.pool)
        self.assertEqual(self.cog._random_cd_cycles, before)

    def test_new_draw_schedules_remaining_without_changing_saved_preferences(self):
        self.cog._schedule_cd_random_warmup = Mock()
        preferences = {'ttv_random_cd': True, 'ttv_melody_id': 'one'}
        before = dict(preferences)
        with patch.object(random_cd, 'available_random_melodies', return_value=self.pool), \
                patch.object(random_cd.random, 'choice', side_effect=lambda values: values[0]):
            self.assertEqual(random_cd.choose_synthesis_melody(self.cog, 1, 2, preferences), 'two')
        self.cog._schedule_cd_random_warmup.assert_called_once_with(1, 2, settings=before)
        self.assertEqual(preferences, before)

    def test_optional_schedule_failure_does_not_interrupt_a_draw(self):
        self.cog._schedule_cd_random_warmup = Mock(side_effect=RuntimeError('shutdown'))
        with patch.object(random_cd, 'available_random_melodies', return_value=self.pool):
            self.assertIn(random_cd.choose_synthesis_melody(self.cog, 1, 2,
                {'ttv_random_cd': True}), {'two', 'three'})


class WarmDecodedPCMTests(unittest.TestCase):
    def test_warm_pcm_is_reused_by_real_progressive_new_message(self):
        source = execution_arrangement(arrangement(), 'kasane-teto')
        cache, calls = DecodedPCMCache(), []
        with tempfile.TemporaryDirectory() as directory:
            renderer = pcm_renderer(calls)
            warm = warm_arrangement_stems(source, renderer, cache_dir=directory,
                render_profile_fingerprint='voice-v1', memory_cache=cache,
                first_request_ms=1000, request_ms=8000, first_only=True)
            self.assertEqual(len(warm['stems']), 1)
            self.assertEqual([call['role'] for call in calls], ['harmony'])
            self.assertGreater(cache.size_bytes, 0)
            result = stream_arrangement(prepare_message_arrangement(source, 'a'), 'a',
                renderer, lambda pcm: True, render_profile_fingerprint='voice-v1',
                memory_cache=cache, cache_dir=directory, require_normalization=True,
                first_window_ms=1000, later_window_ms=8000)
            self.assertEqual([call['role'] for call in calls], ['harmony', 'lead'])
            harmony = next(stem for stem in result['stems'] if stem['part_id'] == 'harmony-1')
            self.assertEqual(harmony['cache_kind'], 'memory')
            warm_again = warm_arrangement_stems(source, renderer, cache_dir=directory,
                render_profile_fingerprint='voice-v1', memory_cache=cache,
                first_request_ms=1000, request_ms=8000, first_only=True)
            self.assertTrue(warm_again['stems'][0]['pcm_cache_hit'])
            self.assertEqual(len(calls), 2)
            changed = warm_arrangement_stems(source, renderer, cache_dir=directory,
                render_profile_fingerprint='voice-v2', memory_cache=cache,
                first_request_ms=1000, request_ms=8000, first_only=True)
            self.assertFalse(changed['stems'][0]['cache_hit'])


class RandomWarmupSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.probe = Probe(self.temporary.name)

    async def test_all_remaining_cds_warm_without_rng_or_history_edits(self):
        probe = self.probe
        probe._random_cd_cycles = OrderedDict({(1, 2): random_cd._RandomCDCycle({'one'}, 'one')})
        before = deepcopy(probe._random_cd_cycles)
        pool = tuple(SimpleNamespace(id=identity) for identity in ('one', 'two', 'three'))
        route = probe._tts_agent_route_state()
        route['ttv_background_protocol'] = 1
        probe._tts_agent_route_state = lambda: route
        probe._warm_cd_accompaniment = AsyncMock()
        clock = iter(range(1, 1000, 2))
        with patch.object(arrangement_audio, 'time', SimpleNamespace(monotonic=lambda: next(clock))), \
                patch.object(random_cd, 'available_random_melodies', return_value=pool), \
                patch.object(random_cd.random, 'choice', side_effect=AssertionError('not a draw')), \
                patch('cogs.tts.melodies.arrangement_for_melody', return_value=arrangement()) as load, \
                patch.dict(sys.modules, {'cogs.tts.audio': SimpleNamespace(QueueItem=QueueItem)}):
            task = probe._schedule_cd_random_warmup(1, 2,
                settings={'ttv_random_cd': True, 'ttv_voice_id': 'kasane-teto'})
            await asyncio.wait_for(task, 3)
        self.assertEqual([call.args[0] for call in load.call_args_list], ['two', 'three'])
        self.assertEqual(probe._warm_cd_accompaniment.await_count, 2)
        self.assertEqual(probe._random_cd_cycles, before)
        self.assertEqual(probe._cd_warmup_tasks, {})

    async def test_ram_request_bypasses_legacy_file_with_same_frozen_controls(self):
        probe = self.probe
        raw, metadata = b'WAV bytes already checked by transport', {'normalization_gain': 1.0}
        probe._generate_tts_agent_worker_bytes = AsyncMock(return_value=(raw, metadata))
        probe._generate_tts_agent_worker_file = AsyncMock(side_effect=AssertionError('no tempfile'))
        parent = queued()
        request = {'text': 'a', 'voice_id': 'kasane-teto', 'language': 'pt-BR',
                   'score': {'version': 1, 'duration_ms': 500, 'lyrics_source': 'vowel',
                             'notes': arrangement()['parts'][1]['notes']}}
        result = await probe._request_cd_stem(parent, request, background=True, progressive=True)
        self.assertEqual(result, (raw, metadata))
        child = probe._generate_tts_agent_worker_bytes.await_args.args[0]
        self.assertTrue(child._cd_background)
        self.assertTrue(child._cd_progressive_request)
        self.assertIsNone(child.ttv_arrangement)
        self.assertEqual(child.ttv_score, request['score'])
        self.assertIsNotNone(parent.ttv_arrangement)
        probe._generate_tts_agent_worker_file.assert_not_awaited()

    async def test_streaming_capability_uses_the_same_child_and_callback(self):
        probe = self.probe
        route = probe._tts_agent_route_state()
        route['ttv_pcm_stream_protocol'] = 1
        probe._tts_agent_route_state = lambda: route
        request = {'text': 'a', 'voice_id': 'kasane-teto', 'language': 'pt-BR',
                   'score': {'version': 1, 'duration_ms': 500, 'lyrics_source': 'vowel',
                             'notes': arrangement()['parts'][1]['notes']}}
        callback = Mock()
        stream = AsyncMock(return_value=(b'WAV', {'normalization_gain': 1.0}))
        with patch.dict(sys.modules, {'cogs.tts.cd_worker_stream': SimpleNamespace(request_pcm_stream=stream)}):
            self.assertEqual(await probe._request_cd_stem_stream(queued(), request, on_pcm=callback),
                             (b'WAV', {'normalization_gain': 1.0}))
        self.assertIs(stream.await_args.args[0], probe)
        child = stream.await_args.args[1]
        self.assertIsNone(child.ttv_arrangement)
        self.assertEqual(child.ttv_score, request['score'])
        self.assertTrue(child._cd_progressive_request)
        self.assertIs(stream.await_args.args[2], callback)

    async def test_legacy_stream_capability_keeps_full_stem_fallback(self):
        probe = self.probe
        probe._request_cd_stem = AsyncMock(return_value=(b'WAV', {}))
        parent, request, callback = queued(), {'untouched': True}, Mock()
        self.assertEqual(await probe._request_cd_stem_stream(parent, request, on_pcm=callback),
                         (b'WAV', {}))
        probe._request_cd_stem.assert_awaited_once_with(parent, request, progressive=True)
        callback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
