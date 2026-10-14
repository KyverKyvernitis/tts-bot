"""Credential-private server/client pairing and repeatable setup."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from contextlib import redirect_stdout

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
            self.assertEqual(first['transport'], 'https')

    def test_explicit_vpn_configuration_accepts_only_private_tailscale_addresses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = configuration.configure('http://100.101.102.103:10000', root / 'config',
                                             root / 'data', root / 'client.json', transport='tailscale')
            client = json.loads((root / 'client.json').read_text())
            self.assertEqual(client['publish_url'], 'http://100.101.102.103:10000/api/osaka/cd/publish')
            self.assertEqual(client['transport'], 'tailscale')
            self.assertNotIn(client['token'], json.dumps(result))
            for url in ('http://100.1.2.3:10000', 'http://127.0.0.1:10000',
                        'http://192.168.1.2:10000', 'http://osaka.example:10000'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    configuration.configure(url, root / 'config', root / 'data', transport='tailscale')

    def test_plain_http_requires_explicit_vpn_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                configuration.configure('http://100.101.102.103:10000', root / 'config', root / 'data')

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


class DiscoveryTests(unittest.TestCase):
    def status_result(self, addresses=None, state='Running', online=True):
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'BackendState': state,
            'Self': {'TailscaleIPs': addresses or ['100.101.102.103'], 'Online': online},
            'Peer': {'ignore': {'Token': 'never-export-this'}},
        }))

    def discover(self, service_environment=None, environment=None, **kwargs):
        with mock.patch.object(configuration, '_readonly_command', return_value=self.status_result()), \
                mock.patch.object(configuration, '_service_environment', return_value=service_environment or {}):
            return configuration.discover_endpoint(environment=environment or {}, **kwargs)

    def test_running_service_port_is_used_instead_of_shell_port(self):
        result = self.discover({'PORT': '12345'}, {'PORT': '10001'})
        self.assertEqual(result['publish_url'], 'http://100.101.102.103:12345/api/osaka/cd/publish')
        self.assertEqual(result['port_source'], 'running_service_PORT')
        self.assertFalse(result['reachability_checked'])
        self.assertNotIn('never-export-this', json.dumps(result))

    def test_explicit_port_overrides_service_without_mutating_environment(self):
        original = {'PORT': '12345'}
        result = self.discover(original, port=11000)
        self.assertIn(':11000/', result['publish_url'])
        self.assertEqual(original['PORT'], '12345')
        self.assertEqual(result['port_source'], 'argument')

    def test_existing_worker_url_is_reused_only_for_this_vps(self):
        result = self.discover({'CORE_WORKER_VPS_URL': 'http://100.101.102.103:12345'})
        self.assertIn(':12345/', result['publish_url'])
        self.assertEqual(result['port_source'], 'existing_worker_url')
        result = self.discover({'CORE_WORKER_VPS_URL': 'http://100.64.1.2:9999'})
        self.assertEqual(result['publish_url'], 'http://100.101.102.103:10000/api/osaka/cd/publish')

    def test_running_service_port_takes_precedence_over_stale_worker_url(self):
        result = self.discover({'PORT': '12345', 'CORE_WORKER_VPS_URL': 'http://100.101.102.103:9999'})
        self.assertIn(':12345/', result['publish_url'])

    def test_shell_port_and_repository_default_are_reported(self):
        result = self.discover(environment={'PORT': '11000'})
        self.assertIn(':11000/', result['publish_url'])
        self.assertEqual(result['port_source'], 'environment_PORT')
        result = self.discover()
        self.assertIn(':10000/', result['publish_url'])
        self.assertEqual(result['port_source'], 'repository_default')

    def test_stopped_or_offline_vpn_does_not_fallback_to_unverified_url(self):
        for status in (self.status_result(state='Stopped'), self.status_result(online=False)):
            with self.subTest(status=status), \
                    mock.patch.object(configuration, '_readonly_command', return_value=status), \
                    self.assertRaises(ValueError):
                configuration.discover_endpoint(environment={'CORE_WORKER_VPS_URL': 'http://100.101.102.103:12345'})

    def test_unavailable_cli_timeout_and_invalid_json_have_safe_errors(self):
        for error in (FileNotFoundError('secret-path'), subprocess.TimeoutExpired(['tailscale'], 2)):
            with self.subTest(error=error), \
                    mock.patch.object(configuration, '_readonly_command', side_effect=error), \
                    self.assertRaisesRegex(ValueError, '^tailscale_status_unavailable$'):
                configuration.discover_endpoint(environment={})
        for stdout in ('not json', '[]', '{"BackendState":"Running","Self":{"TailscaleIPs":null}}'):
            with self.subTest(stdout=stdout), \
                    mock.patch.object(configuration, '_readonly_command', return_value=SimpleNamespace(returncode=0, stdout=stdout)), \
                    self.assertRaises(ValueError):
                configuration.discover_endpoint(environment={})

    def test_invalid_port_and_non_tailnet_self_address_are_rejected(self):
        for port in ('0', '65536', '12345secret', '-1'):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, '^invalid_bot_port$'):
                self.discover({'PORT': port})
        with mock.patch.object(configuration, '_readonly_command', return_value=self.status_result(['127.0.0.1'])), \
                self.assertRaisesRegex(ValueError, '^tailscale_address_unavailable$'):
            configuration.discover_endpoint(environment={})

    def test_ipv6_tailnet_address_is_bracketed(self):
        with mock.patch.object(configuration, '_readonly_command',
                               return_value=self.status_result(['fd7a:115c:a1e0::1234'])), \
                mock.patch.object(configuration, '_service_environment', return_value={}):
            result = configuration.discover_endpoint(environment={})
        self.assertEqual(result['publish_url'], 'http://[fd7a:115c:a1e0::1234]:10000/api/osaka/cd/publish')

    def test_service_environment_whitelist_does_not_return_tokens(self):
        raw = b'PORT=12345\x00CORE_WORKER_TOKEN=private-token\x00OSAKA_CD_PUBLISH_TOKEN=another-token\x00CORE_WORKER_VPS_URL=http://100.101.102.103:12345\x00'
        with mock.patch.object(configuration, '_readonly_command', return_value=SimpleNamespace(returncode=0, stdout='123\n')), \
                mock.patch.object(configuration.Path, 'open', return_value=io.BytesIO(raw)):
            result = configuration._service_environment('tts-bot.service')
        self.assertEqual(set(result), {'PORT', 'CORE_WORKER_VPS_URL'})
        self.assertNotIn('private-token', json.dumps(result))
        self.assertNotIn('another-token', json.dumps(result))

    def test_service_pid_unavailable_is_nonfatal(self):
        for stdout in ('0', 'not-pid', '1'):
            with mock.patch.object(configuration, '_readonly_command', return_value=SimpleNamespace(returncode=0, stdout=stdout)):
                self.assertEqual(configuration._service_environment('tts-bot.service'), {})
        with mock.patch.object(configuration, '_readonly_command', side_effect=PermissionError):
            self.assertEqual(configuration._service_environment('tts-bot.service'), {})

    def test_main_automatically_exports_vpn_pairing_without_interactive_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            argv = ['configure-cd-server.py', '--config-dir', str(root / 'config'),
                    '--data-root', str(root / 'data'), '--client-config', str(root / 'client.json')]
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(configuration, '_readonly_command', return_value=self.status_result()), \
                    mock.patch.object(configuration, '_service_environment', return_value={'PORT': '12345'}), \
                    mock.patch('builtins.input', side_effect=AssertionError('must not ask for public address')), \
                    redirect_stdout(output):
                exit_code = configuration.main()
            self.assertEqual(exit_code, 0)
            result = json.loads(output.getvalue())
            client = json.loads((root / 'client.json').read_text())
            self.assertEqual(client['transport'], 'tailscale')
            self.assertNotIn(client['token'], output.getvalue())
            self.assertFalse(result['service_restarted'])
            self.assertFalse(result['discovery']['reachability_checked'])


if __name__ == '__main__':
    unittest.main()
