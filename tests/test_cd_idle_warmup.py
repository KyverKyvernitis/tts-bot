"""Real CD plans/caches and idle admission, without voicebanks or Discord."""
from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from cogs.tts.arrangement_audio import execution_arrangement
from osaka_cd.lyrics import prepare_message_arrangement
from osaka_cd.render import RenderError, render_arrangement, warm_arrangement_stems
from test_cd_arrangement_audio_contract import Probe, QueueItem, queued, arrangement


def render_fixture(calls):
    def render(**request):
        calls.append(request)
        result = io.BytesIO()
        with wave.open(result, 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', ''))
            output.writeframes(b'\x10\x02' * (request['score']['duration_ms'] * 8))
        return result.getvalue(), {'midi_preroll_ms': 0, 'resampler_calls': 1,
                                   'resample_ms': 10, 'private_command': 'never-record'}
    return render


class FixedStemTests(unittest.TestCase):
    def test_warming_reuses_exact_fixed_parts_of_adapted_new_text(self):
        source = execution_arrangement(arrangement(), 'kasane-teto')
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            renderer = render_fixture(calls)
            warm = warm_arrangement_stems(source, renderer, cache_dir=directory,
                render_profile_fingerprint='bank-v1')
            self.assertEqual([call['role'] for call in calls], ['harmony'])
            self.assertFalse(warm['stems'][0]['cache_hit'])
            adapted = prepare_message_arrangement(source, 'a')
            result = render_arrangement(adapted, 'a', renderer, cache_dir=directory,
                render_profile_fingerprint='bank-v1')
            self.assertEqual([call['role'] for call in calls], ['harmony', 'lead'])
            harmony = next(stem for stem in result['stems'] if stem['part_id'] == 'harmony-1')
            self.assertTrue(harmony['cache_hit'])
            self.assertEqual(harmony['render_metrics'], {})
            lead = next(stem for stem in result['stems'] if stem['part_id'] == 'lead')
            self.assertEqual(lead['render_metrics']['resampler_calls'], 1)
            self.assertNotIn('private_command', lead['render_metrics'])
            self.assertGreater(result['metadata']['timing_ms']['synthesize'], 0)
            repeat = render_arrangement(adapted, 'a', renderer, cache_dir=directory,
                render_profile_fingerprint='bank-v1')
            self.assertTrue(repeat['mix_cache_hit'])
            self.assertEqual(repeat['metadata']['timing_ms']['synthesize'], 0)
            self.assertTrue(all(not stem['render_metrics'] for stem in repeat['stems']))
            self.assertEqual(result['audio'], repeat['audio'])
            # Another bank must not inherit the warmed recording.
            changed = warm_arrangement_stems(source, renderer, cache_dir=directory,
                render_profile_fingerprint='bank-v2')
            self.assertFalse(changed['stems'][0]['cache_hit'])

    def test_missing_identity_cancel_and_limits_never_start_synthesis(self):
        source = execution_arrangement(arrangement(), 'kasane-teto')
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            renderer = render_fixture(calls)
            self.assertEqual(warm_arrangement_stems(source, renderer, cache_dir=directory,
                render_profile_fingerprint='')['stems'], [])
            with self.assertRaisesRegex(RenderError, 'cancelled'):
                warm_arrangement_stems(source, renderer, cache_dir=directory,
                    render_profile_fingerprint='bank', cancelled=lambda: True)
            with self.assertRaises(RenderError):
                warm_arrangement_stems(source, renderer, cache_dir=directory,
                    render_profile_fingerprint='bank', max_stems=0)
            self.assertFalse(calls)


class IdleWarmupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.probe = Probe(self.temporary.name)

    async def test_warmup_avoids_foreground_gate_and_marks_child_priority(self):
        probe = self.probe
        probe._cd_composition_gate = asyncio.Semaphore(0)
        with patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': self.temporary.name}):
            result = await asyncio.wait_for(probe._warm_cd_accompaniment(queued(), 'bank'), 5)
        self.assertEqual(len(result['stems']), 1)
        self.assertEqual(len(probe.calls), 1)
        self.assertTrue(probe.calls[0]._cd_background)
        self.assertNotEqual(probe.calls[0].ttv_score['lyrics_source'], 'message')
        self.assertEqual(list(Path(self.temporary.name).glob('*.wav')), [])

    async def test_foreground_stops_next_background_request(self):
        probe = self.probe
        probe._cd_foreground_compositions = 1
        with patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': self.temporary.name}):
            with self.assertRaisesRegex(RuntimeError, 'foreground_pending'):
                await probe._warm_cd_accompaniment(queued(), 'bank')
        self.assertFalse(probe.calls)

    async def test_dequeued_active_message_is_not_idle(self):
        self.probe.guild_states = {1: SimpleNamespace(active_item=queued(), queue=asyncio.Queue())}
        self.assertFalse(self.probe._cd_warmup_idle())
        self.probe.guild_states[1].active_item = None
        self.assertTrue(self.probe._cd_warmup_idle())

    async def test_switch_to_default_cancels_previous_selection(self):
        from cogs.tts.melodies import DEFAULT_MELODY_ID
        pending = asyncio.create_task(asyncio.sleep(60))
        self.probe._cd_warmup_tasks = {(1, 2): pending}
        self.probe._schedule_cd_accompaniment_warmup(1, 2,
            melody_id=DEFAULT_MELODY_ID, settings={})
        await asyncio.sleep(0)
        self.assertTrue(pending.cancelled())

    async def test_scheduler_requires_capability_and_frees_task_registry(self):
        from cogs.tts import arrangement_audio
        for protocol in (0, 1):
            with self.subTest(protocol=protocol):
                probe = self.probe
                route = probe._tts_agent_route_state()
                route['ttv_background_protocol'] = protocol
                probe._tts_agent_route_state = lambda: route
                probe._warm_cd_accompaniment = AsyncMock()
                clock = iter(range(1, 1000, 6))
                with patch.object(arrangement_audio, 'time', SimpleNamespace(monotonic=lambda: next(clock))), \
                        patch('cogs.tts.melodies.arrangement_for_melody', return_value=arrangement()), \
                        patch.dict(sys.modules, {'cogs.tts.audio': SimpleNamespace(QueueItem=QueueItem)}):
                    task = probe._schedule_cd_accompaniment_warmup(1, 2,
                        melody_id='fixture-cd', settings={'ttv_voice_id': 'kasane-teto'})
                    await asyncio.wait_for(task, 3)
                self.assertEqual(probe._warm_cd_accompaniment.await_count, protocol)
                self.assertEqual(probe._cd_warmup_tasks, {})

    async def test_disabled_scheduler_does_not_create_work(self):
        with patch.dict(os.environ, {'OSAKA_CD_WARMUP_ENABLED': 'false'}):
            self.assertIsNone(self.probe._schedule_cd_accompaniment_warmup(1, 2,
                melody_id='fixture-cd', settings={}))
        self.assertFalse(self.probe.calls)


if __name__ == '__main__':
    unittest.main()
