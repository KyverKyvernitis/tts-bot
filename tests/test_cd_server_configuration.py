"""Credential-private server/client pairing and repeatable setup."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cd_server_configuration', ROOT / 'scripts/configure-cd-server.py')
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)


class ConfigurationTests(unittest.TestCase):
    def test_generated_configuration_is_private_and_preserves_pairing_on_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = configuration.configure('https://osaka.example', root / 'config', root / 'data', root / 'client.json')
            first = json.loads((root / 'client.json').read_text())
            self.assertNotIn(first['token'], json.dumps(result))
            self.assertGreaterEqual(len(first['token']), 32)
            for path in (root / 'client.json', root / 'config/server.env'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            configuration.configure('https://osaka.example', root / 'config', root / 'data', root / 'client.json')
            self.assertEqual(first['token'], json.loads((root / 'client.json').read_text())['token'])

    def test_rejects_unencrypted_credentials_redirect_paths_and_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for url in ('http://osaka.example', 'https://secret@osaka.example', 'https://osaka.example?token=x'):
                with self.assertRaises(ValueError):
                    configuration.configure(url, root / 'config', root / 'data')
            (root / 'config').mkdir()
            (root / 'victim').write_text('unchanged')
            (root / 'config/server.env').symlink_to(root / 'victim')
            with self.assertRaises(ValueError):
                configuration.configure('https://osaka.example', root / 'config', root / 'data')
            self.assertEqual((root / 'victim').read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
