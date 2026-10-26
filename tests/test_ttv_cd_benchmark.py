"""Real adaptation/composition with a fixture renderer; no phone latency claim."""
from __future__ import annotations

import array
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]


def script():
    spec = importlib.util.spec_from_file_location('_cd_benchmark_test', ROOT / 'scripts/benchmark-ttv-cd.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arrangement():
    return {'score_schema': 2, 'duration_ms': 500, 'parts': [
        {'id': identity, 'role': role, 'voice_id': 'kasane-teto', 'lyrics_source': 'vowel',
         'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]}
        for identity, role, pitch in [('lead', 'lead', 60), ('harmony', 'harmony', 64)]]}


class CDLatencyBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.module = script()
        self.temporary = tempfile.TemporaryDirectory(prefix='cd-benchmark-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bank = self.root / 'bank'
        self.bank.mkdir()
        (self.bank / 'source.wav').write_bytes(b'unchanged bank')
        self.production = self.root / 'production-cache'
        self.production.mkdir()
        (self.production / 'keep.esp').write_bytes(b'unchanged analysis')
        self.bridge = self.root / 'esper/bin/resampler.py'
        self.bridge.parent.mkdir(parents=True)
        self.bridge.write_bytes((ROOT / 'deploy/esper-utau/termux/resampler.py').read_bytes())
        self.values = {'PHONE_WORKER_TETO_RESAMPLER_COMMAND': shlex.join([
            sys.executable, str(self.bridge), '--cache-dir=' + str(self.production)]),
            'PHONE_WORKER_ESPER_CACHE_DIR': str(self.production)}
        self.instances, self.calls = [], []
        module, instances, calls = self.module, self.instances, self.calls

        class FakeRenderer:
            def __init__(self, environment, voice_id):
                self.env, self.voice_id = environment, voice_id
                instances.append(self)

            def singing_fingerprint(self, **kwargs):
                # Identity intentionally excludes scheduling-only controls.
                return hashlib.sha256(self.voice_id.encode()).hexdigest()

            def synthesize_performance(self, text, **kwargs):
                cache = Path(self.env['PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR'])
                analysis = Path(self.env['PHONE_WORKER_ESPER_CACHE_DIR'])
                analysis.mkdir(parents=True, exist_ok=True)
                analysis_warm = (analysis / 'source.esp').exists()
                (analysis / 'source.esp').write_bytes(b'temporary analysis')
                cache.mkdir(parents=True, exist_ok=True)
                calls.append({'text': text, 'environment': dict(self.env), 'analysis_warm': analysis_warm})
                score = kwargs['score']
                samples = array.array('h', [700 + score['notes'][0]['midi_note']] * (score['duration_ms'] * 8))
                if sys.byteorder != 'little':
                    samples.byteswap()
                raw = io.BytesIO()
                with wave.open(raw, 'wb') as audio:
                    audio.setparams((1, 2, 8000, 0, 'NONE', ''))
                    audio.writeframes(samples.tobytes())
                data = raw.getvalue()
                return data, {'voice_id': self.voice_id, 'reading_language': kwargs['reading_language'],
                    'pitch_offset_semitones': 0.0, 'speech_rate': 1.0, 'midi_preroll_ms': 0,
                    'normalization_gain': 1.0,
                    'sha256': hashlib.sha256(data).hexdigest(), 'resampler_calls': 1,
                    'render_workers': int(self.env['PHONE_WORKER_TETO_RENDER_THREADS'])}

        self.factory = FakeRenderer

    def perform(self, **changes):
        args = dict(values=self.values, arrangement=arrangement(), bank=self.bank,
                    factory=self.factory, text='a', new_text='o', rounds=2)
        args.update(changes)
        with patch.object(self.module, 'battery_temperature', return_value=None):
            return self.module.benchmark(**args)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for real composition')
    def test_phases_keep_new_text_and_fixed_stem_reuse_separate(self):
        previous_environment = dict(os.environ)
        report = self.perform()
        self.assertTrue(report['ok'], json.dumps(report))
        self.assertFalse(report['physical_utau_requested'])
        self.assertTrue(report['injected_renderer'])
        self.assertTrue(report['temporary_files_removed'])
        phases = report['samples']
        self.assertEqual(len(phases), 13)
        self.assertEqual([r['threads_requested'] for r in phases[1:]], [1] * 3 + [2] * 6 + [1] * 3)
        self.assertEqual(phases[0]['phase'], 'analysis_cold')
        for start in range(1, len(phases), 3):
            new_fragments, repeat, new_text = phases[start:start + 3]
            self.assertEqual(len(new_fragments['physical_parts']), 2)
            self.assertEqual(len(repeat['physical_parts']), 0)
            self.assertTrue(repeat['mix_cache_hit'])
            self.assertTrue(all(stem['cache_hit'] for stem in repeat['stems']))
            self.assertEqual([p['part_id'] for p in new_text['physical_parts']], ['lead'])
            self.assertTrue(next(stem['cache_hit'] for stem in new_text['stems'] if stem['part_id'] == 'harmony'))
            self.assertTrue(repeat['pcm_matches_reference'])
        self.assertFalse(self.calls[0]['analysis_warm'])
        self.assertTrue(all(call['analysis_warm'] for call in self.calls[1:]))
        for instance in self.instances:
            env = instance.env
            analysis = env['PHONE_WORKER_ESPER_CACHE_DIR']
            command = shlex.split(env['PHONE_WORKER_TETO_RESAMPLER_COMMAND'])
            self.assertEqual(command[command.index('--cache-dir') + 1], analysis)
            self.assertNotIn('--cache-dir=' + str(self.production), command)
            self.assertEqual(env['PHONE_WORKER_ESPER_DEADLINE'], '')
            self.assertFalse(Path(analysis).exists())
        self.assertEqual((self.production / 'keep.esp').read_bytes(), b'unchanged analysis')
        self.assertEqual((self.bank / 'source.wav').read_bytes(), b'unchanged bank')
        self.assertEqual(dict(os.environ), previous_environment)

    def test_audio_settings_are_literals_and_do_not_retain_tokens(self):
        env = self.root / 'worker.env'
        env.write_text('TOKEN="do-not-retain"\nPHONE_WORKER_TETO_FLAGS="B0" # audio\n'
                       'PHONE_WORKER_TETO_RESAMPLER_COMMAND=\'literal $(do-not-run)\'\n')
        settings = self.module.read_settings(self.module.quality_helpers(), env)
        self.assertNotIn('TOKEN', settings)
        self.assertEqual(settings['PHONE_WORKER_TETO_FLAGS'], 'B0')
        self.assertEqual(settings['PHONE_WORKER_TETO_RESAMPLER_COMMAND'], 'literal $(do-not-run)')

    def test_thread_bounds_text_and_round_validation(self):
        for args in ({'threads': (4,)}, {'threads': (1, 1)}, {'threads': (True,)},
                     {'rounds': 0}, {'timeout': float('nan')}, {'new_text': 'a'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.perform(**args)
        # Miku can request four workers and receives an independent profile.
        miku_values = {**self.values, 'PHONE_WORKER_MIKU_RESAMPLER_COMMAND':
                       self.values['PHONE_WORKER_TETO_RESAMPLER_COMMAND']}
        env = self.module.isolated_environment(self.module.quality_helpers(), miku_values,
            voice='miku', bank=self.bank, container='voicepeak-arm64', esper_root=None,
            analysis=self.root / 'analysis', fragments=self.root / 'fragments', threads=4, timeout=120)
        self.assertEqual(env['PHONE_WORKER_TETO_ALIAS_SUFFIX'], '_D4')
        self.assertEqual(env['PHONE_WORKER_TETO_RENDER_THREADS'], '4')

    def test_report_cannot_overwrite_configuration_bank_or_cache(self):
        env = self.root / 'worker.env'
        for path in (env, self.bank / 'report.json', self.production / 'report.json', ROOT / 'report.json'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.module.safe_report_path(path, env=env, bank=self.bank, values=self.values)
        self.assertEqual(self.module.safe_report_path(self.root / 'report.json',
            env=env, bank=self.bank, values=self.values), self.root / 'report.json')

    def test_pcm_hash_covers_format_and_samples(self):
        def wav(rate):
            raw = io.BytesIO()
            with wave.open(raw, 'wb') as audio:
                audio.setparams((1, 2, rate, 0, 'NONE', ''))
                audio.writeframes(b'\x01\x00' * 100)
            return raw.getvalue()
        self.assertNotEqual(self.module.pcm_digest(wav(8000)), self.module.pcm_digest(wav(16000)))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for real composition')
    def test_changed_pcm_is_failure_and_stops_comparison(self):
        original = self.factory

        class ChangedRenderer(original):
            def synthesize_performance(self, text, **kwargs):
                raw, meta = super().synthesize_performance(text, **kwargs)
                if self.env['PHONE_WORKER_TETO_RENDER_THREADS'] == '2':
                    raw = raw[:-2] + b'\x01\x00'
                    meta['sha256'] = hashlib.sha256(raw).hexdigest()
                return raw, meta

        report = self.perform(factory=ChangedRenderer, rounds=1)
        self.assertFalse(report['ok'])
        self.assertTrue(report['temporary_files_removed'])
        self.assertFalse(report['physical_utau_completed'])
        self.assertIn('PCM differs', report['samples'][-1]['error'])
        self.assertEqual(report['samples'][-1]['threads_requested'], 2)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for fixture sample conversion')
    def test_progressive_phases_report_availability_without_claiming_playback(self):
        report = self.perform(progressive=True, rounds=1, threads=(1,))
        self.assertTrue(report['ok'], json.dumps(report))
        self.assertEqual(report['output_mode'], 'progressive_pcm')
        self.assertFalse(report['discord_called'])
        self.assertIn('no audio is played', report['first_audio_note'])
        self.assertTrue(report['temporary_files_removed'])
        for record in report['samples']:
            self.assertEqual(record['output_mode'], 'progressive_pcm')
            self.assertTrue(record['pcm_matches_reference'])
            self.assertNotIn('mp3_sha256', record)
            self.assertIsNotNone(record['first_nonzero_pcm_available_ms'])
            self.assertIsNotNone(record['buffer_ready_ms'])
            self.assertLessEqual(record['first_pcm_available_ms'], record['buffer_ready_ms'])
            self.assertLessEqual(record['first_nonzero_pcm_available_ms'], record['elapsed_ms'])
            self.assertLessEqual(record['buffer_ready_ms'], record['elapsed_ms'])
        repeat = report['samples'][2]
        self.assertFalse(repeat['physical_parts'])
        self.assertTrue(all(stem['cache_kind'] == 'memory' for stem in repeat['stems']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for fixture sample conversion')
    def test_progressive_prefix_precedes_later_physical_lead(self):
        selected = {'score_schema': 2, 'duration_ms': 6000, 'parts': [
            {'id': 'lead', 'role': 'lead', 'voice_id': 'kasane-teto', 'lyrics_source': 'message',
             'notes': [{'midi_note': 60 if i < 6 else 62, 'start_ms': i*500,
                        'duration_ms': 500, 'velocity': 100} for i in range(12)]},
            {'id': 'harmony', 'role': 'harmony', 'voice_id': 'kasane-teto', 'lyrics_source': 'vowel',
             'notes': [{'midi_note': 64, 'start_ms': 0, 'duration_ms': 6000, 'velocity': 100}]}]}
        report = self.perform(progressive=True, arrangement=selected,
                              text=' '.join(['a']*12), new_text=' '.join(['o']*12),
                              rounds=1, threads=(1,))
        self.assertTrue(report['ok'], json.dumps(report))
        cold = report['samples'][0]
        leads = [part for part in cold['physical_parts'] if part['role'] == 'lead']
        self.assertGreaterEqual(len(leads), 2)
        self.assertLessEqual(cold['first_nonzero_pcm_available_ms'], leads[1]['started_ms'])
        self.assertLessEqual(cold['buffer_ready_ms'], leads[1]['started_ms'])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for fixture sample conversion')
    def test_progressive_pcm_mismatch_stops_thread_comparison(self):
        original = self.factory

        class ChangedRenderer(original):
            def synthesize_performance(self, text, **kwargs):
                raw, meta = super().synthesize_performance(text, **kwargs)
                if self.env['PHONE_WORKER_TETO_RENDER_THREADS'] == '2':
                    raw = raw[:-2] + b'\x01\x00'
                    meta['sha256'] = hashlib.sha256(raw).hexdigest()
                return raw, meta

        report = self.perform(progressive=True, factory=ChangedRenderer, rounds=1)
        self.assertFalse(report['ok'])
        self.assertIn('PCM differs', report['samples'][-1]['error'])


if __name__ == '__main__':
    unittest.main()
