"""The resident bridge fits the existing automatic phone update protocol."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LowLatencyReleaseTests(unittest.TestCase):
    def test_real_bundle_fits_and_validates_with_the_existing_bootstrap(self):
        publisher = load('ttv_low_latency_release_publisher', ROOT / 'scripts/core-worker-automation.py')
        bootstrap = load('ttv_low_latency_bootstrap',
                         ROOT / 'deploy/termux/phone-worker/phone_worker_bootstrap.py')
        payload = publisher._build_worker_update_payload()
        self.assertLessEqual(len(payload['files']) + 1, 64)
        self.assertFalse(any('resident' in item['target'] or 'esper_bridge' in item['target']
                             for item in payload['files']))
        with tempfile.TemporaryDirectory(prefix='ttv-release-') as directory:
            temporary = Path(directory)
            with patch.object(publisher, 'AGENT_RELEASE_ROOT', temporary / 'publish'), \
                 patch.object(publisher, '_public_base_url', return_value='http://127.0.0.1:10000'):
                outer = publisher._publish_phone_worker_release(payload)
            archive = temporary / 'publish/releases' / (outer['source_hash'] + '.zip')
            self.assertLessEqual(archive.stat().st_size, 8 * 1024 * 1024)
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), outer['sha256'])
            staging = temporary / 'staging'
            metadata = bootstrap._extract_and_validate(archive, staging, outer)
            self.assertEqual(metadata['members'], len(payload['files']))
            source = ROOT / 'deploy/termux/phone-worker/teto_renderer/renderer.py'
            self.assertEqual((staging / 'teto_renderer/renderer.py').read_bytes(), source.read_bytes())
            with zipfile.ZipFile(archive) as exported:
                self.assertEqual(len(exported.namelist()), 64)
                self.assertIsNone(exported.testzip())


if __name__ == '__main__':
    unittest.main()
