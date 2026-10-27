"""Known ESPER launchers adopt the shipped resident bridge without config edits."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer import TetoRenderer
import teto_renderer.renderer as implementation


class BundledEsperRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='esper-bundle-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old = self.root / 'resampler.py'
        self.old_bytes = b'raise RuntimeError("old launcher must never execute")\n'
        self.old.write_bytes(self.old_bytes)
        self.old_hash = hashlib.sha256(self.old_bytes).hexdigest()
        self.bundle = (b'class InProcessEsper:\n'
            b' def __init__(self, **settings): self.settings=settings; self.origin="bundled"; self.closed=False\n'
            b' def close(self):\n'
            b'  self.closed=True\n'
            b'  directory=getattr(self,"_bundled_source_directory",None)\n'
            b'  if directory is not None: directory.cleanup()\n')
        self.digest = hashlib.sha256(self.bundle).hexdigest()
        self.command = [sys.executable, str(self.old), '--implementation-id', self.old_hash]
        self.renderer = TetoRenderer(environment={
            'PHONE_WORKER_TETO_RESAMPLER_COMMAND': ' '.join(self.command),
            'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(self.root / 'fragments')})
        self.patches = [
            patch.object(self.renderer, 'BACKGROUND_BRIDGE_SHA256', self.digest),
            patch.object(self.renderer, 'COMPATIBLE_ESPER_BRIDGES', frozenset({self.old_hash})),
            patch.object(implementation, '_BUNDLED_ESPER_BRIDGE_ZLIB_B64',
                         base64.b64encode(zlib.compress(self.bundle)).decode()),
        ]
        for replacement in self.patches:
            replacement.start()
            self.addCleanup(replacement.stop)

    def tearDown(self):
        saved = self.renderer._esper_client_cache
        if saved and saved[1] is not None:
            saved[1].close()

    def test_compatible_pinned_launcher_adopts_bundle_without_changing_files_or_settings(self):
        before = dict(self.renderer._environment)
        client = self.renderer._esper_inprocess_client(self.command)
        self.assertEqual(client.origin, 'bundled')
        self.assertIs(self.renderer._esper_inprocess_client(self.command), client)
        self.assertTrue(self.renderer.supports_background_cancellation())
        self.assertEqual(self.old.read_bytes(), self.old_bytes)
        self.assertEqual(dict(self.renderer._environment), before)
        runtime = Path(client._bundled_source_directory.name) / 'resampler.py'
        self.assertEqual(runtime.read_bytes(), self.bundle)
        self.assertEqual(runtime.parent.stat().st_mode & 0o777, 0o700)

    def test_explicit_launcher_pin_mismatch_never_executes_the_bundle(self):
        self.assertIsNone(self.renderer._esper_inprocess_client(
            [sys.executable, str(self.old), '--implementation-id', self.digest]))
        self.assertIsNone(self.renderer._esper_client_cache)

    def test_custom_launcher_cannot_silently_enter_the_bundled_engine(self):
        custom = self.root / 'custom.py'
        custom.write_text('print("custom")\n')
        self.assertIsNone(self.renderer._esper_inprocess_client([sys.executable, str(custom)]))

    def test_corrupt_bundle_is_refused_before_any_source_is_executed(self):
        with patch.object(implementation, '_BUNDLED_ESPER_BRIDGE_ZLIB_B64',
                          base64.b64encode(zlib.compress(b'raise AssertionError')).decode()):
            self.assertIsNone(self.renderer._esper_inprocess_client(self.command))

    def test_reconfiguration_closes_the_previous_resident_client(self):
        first = self.renderer._esper_inprocess_client(self.command)
        second = self.renderer._esper_inprocess_client([*self.command, '--cache-dir', str(self.root / 'new-cache')])
        self.assertIsNot(first, second)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)


class ProductionEsperBundleTests(unittest.TestCase):
    def test_embedded_source_and_portable_hook_match_the_verified_source_tree(self):
        embedded = zlib.decompress(base64.b64decode(implementation._BUNDLED_ESPER_BRIDGE_ZLIB_B64,
                                                   validate=True))
        source = (ROOT / 'deploy/esper-utau/termux/resampler.py').read_bytes()
        self.assertEqual(embedded, source)
        self.assertEqual(hashlib.sha256(embedded).hexdigest(), TetoRenderer.BACKGROUND_BRIDGE_SHA256)
        namespace = {'__name__': 'verified_esper_bundle_test', '__file__': str(
            ROOT / 'deploy/esper-utau/termux/resampler.py')}
        exec(compile(embedded, namespace['__file__'], 'exec'), namespace)
        hook = base64.b64decode(namespace['RESIDENT_HOOK_BASE64'], validate=True)
        self.assertTrue(hook.startswith(b'MZ'), 'Portable managed assembly required')
        self.assertLessEqual(len(hook), 65536)
        self.assertEqual(hashlib.sha256(hook).hexdigest(), namespace['RESIDENT_HOOK_SHA256'])


if __name__ == '__main__':
    unittest.main()
