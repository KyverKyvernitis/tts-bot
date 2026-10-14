"""Isolated env/release checks using the unchanged 1.11.41 renderer."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'
sys.path.insert(0, str(KIT))
import worker_config


class WorkerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cd-worker-config-')
        self.root = Path(self.temp.name)
        self.env = self.root / 'worker env'

    def tearDown(self):
        self.temp.cleanup()

    def test_load_real_shell_expansions_without_credentials_or_global_mutation(self):
        self.env.write_text('export PHONE_WORKER_TETO_ENABLED=true\n'
                            'PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR="$HOME/bank with spaces"\n'
                            'PHONE_WORKER_TETO_RESAMPLER_COMMAND=\'resampler "a b"\'\n'
                            'PHONE_WORKER_TOKEN=credential-canary\n'
                            'echo credential-canary\n')
        before = dict(os.environ)
        loaded = worker_config.load_environment(self.env)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(loaded['PHONE_WORKER_TETO_ENABLED'], 'true')
        self.assertEqual(loaded['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'], str(Path.home() / 'bank with spaces'))
        self.assertNotIn('credential-canary', json.dumps(loaded))
        self.assertNotIn('PHONE_WORKER_TOKEN', loaded)

    def test_invalid_env_diagnostic_is_redacted(self):
        self.env.write_text('echo credential-canary >&2\nreturn 1\n')
        with self.assertRaises(worker_config.WorkerConfigurationError) as caught:
            worker_config.load_environment(self.env)
        self.assertNotIn('credential-canary', str(caught.exception))

    def test_config_fingerprint_changes_when_env_changes(self):
        self.env.write_text('PHONE_WORKER_TETO_ENABLED=false\n')
        first = worker_config.configuration_fingerprint(self.env)
        self.env.write_text('PHONE_WORKER_TETO_ENABLED=true\n')
        self.assertNotEqual(first, worker_config.configuration_fingerprint(self.env))

    def test_search_key_is_selected_only_for_metadata(self):
        self.env.write_text('YOUTUBE_API_KEY=metadata-canary\nPHONE_WORKER_TOKEN=worker-secret\n')
        self.assertNotIn('YOUTUBE_API_KEY', worker_config.load_environment(self.env))
        loaded = worker_config.load_environment(self.env, include_search_credentials=True)
        self.assertEqual(loaded['YOUTUBE_API_KEY'], 'metadata-canary')
        self.assertNotIn('PHONE_WORKER_TOKEN', loaded)

    def test_installed_renderer_is_loaded_without_old_worker_shadow_or_mutation(self):
        repo = KIT.parents[1]
        original = repo / 'deploy/termux/phone-worker'
        runtime = self.root / 'runtime'
        release = runtime / 'releases' / 'fixture-1.11.41'
        release.mkdir(parents=True)
        if not (original / 'phone_worker.py').is_file():
            self.skipTest('Existing worker source is unavailable in the standalone kit')
        shutil.copy2(original / 'phone_worker.py', release / 'phone_worker.py')
        shutil.copytree(original / 'teto_renderer', release / 'teto_renderer', ignore=shutil.ignore_patterns('__pycache__'))
        (runtime / 'current').symlink_to(release)
        env = {'PHONE_WORKER_RUNTIME_ROOT': str(runtime), 'PHONE_WORKER_TETO_ENABLED': 'false',
               'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(self.root / 'isolated-cache')}
        source_before = (release / 'phone_worker.py').read_bytes()
        files_before = {str(p.relative_to(release)): p.read_bytes() for p in release.rglob('*') if p.is_file()}
        found = worker_config.find_release(env)
        renderer = worker_config._renderer_type(found)(environment=env, voice_id='kasane-teto')
        self.assertEqual(renderer._getenv('PHONE_WORKER_TETO_ENABLED'), 'false')
        self.assertTrue(callable(renderer.synthesize_performance))
        self.assertEqual((release / 'phone_worker.py').read_bytes(), source_before)
        self.assertEqual(files_before, {str(p.relative_to(release)): p.read_bytes() for p in release.rglob('*') if p.is_file()})
        self.assertFalse((release / 'cache').exists())

    def test_old_worker_version_is_rejected(self):
        runtime = self.root / 'runtime'
        release = runtime / 'releases' / 'old'
        release.mkdir(parents=True)
        (release / 'phone_worker.py').write_text('PHONE_WORKER_VERSION = "1.11.0"\n')
        (runtime / 'current').symlink_to(release)
        with self.assertRaisesRegex(worker_config.WorkerConfigurationError, 'worker_1_11_41_required'):
            worker_config.find_release({'PHONE_WORKER_RUNTIME_ROOT': str(runtime)})

    def test_unavailable_runtime_keeps_old_mp3(self):
        self.env.write_text('PHONE_WORKER_RUNTIME_ROOT=\'' + str(self.root / 'absent') + '\'\n')
        target = self.root / 'keep.mp3'
        target.write_bytes(b'old completed audio')
        with patch.dict(os.environ, {'PHONE_WORKER_ENV': str(self.env)}):
            result = worker_config.render_sample({'duration_ms': 500, 'notes': []}, target)
        self.assertEqual(result['status'], 'pending_worker_release_missing')
        self.assertEqual(target.read_bytes(), b'old completed audio')


if __name__ == '__main__':
    unittest.main()
