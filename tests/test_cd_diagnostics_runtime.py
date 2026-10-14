"""The user-facing diagnostic reports the actual pinned runtime generation."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest

from test_cd_v9_end_to_end import RealMediaFixtures, selected_store

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cd_diagnostics_runtime', ROOT / 'scripts/diagnose-cd-assets.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class DiagnosticTests(RealMediaFixtures):
    def test_diagnostic_uses_generation_manifest_cover_and_real_emoji_bytes(self):
        self.upload('diagnostic-song')
        with selected_store(self.store) as runtime, runtime.pin_snapshot() as selected:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                diagnostics.diagnose('diagnostic-song', selected)
            result = json.loads(output.getvalue())
            self.assertEqual(result['runtime_generation'], selected['generation'])
            self.assertTrue(result['catalog_path'].startswith(selected['root']))
            self.assertTrue(result['local_cover_ready'])
            self.assertTrue(result['emoji_image']['valid'])
            self.assertFalse(result['discord_api_called'])


if __name__ == '__main__':
    unittest.main()
