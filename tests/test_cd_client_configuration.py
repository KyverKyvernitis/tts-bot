"""Reuse the worker's literal address, while keeping publication credentials separate."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/ttv-cd'))
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('cd_client_configuration', ROOT / 'deploy/ttv-cd/configure-cd.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

WORKER_SECRET = 'existing-worker-secret-not-a-publication-token'
PUBLISH_SECRET = 'separate-publication-token-with-minimum-32-characters'
PRIVATE_URL = 'http://100.101.102.103:10000/api/osaka/cd/publish'


def printed_json(text):
    """A detected-address notice can precede the final formatted result."""
    values = []
    decoder = json.JSONDecoder()
    remaining = text.strip()
    while remaining:
        value, end = decoder.raw_decode(remaining)
        values.append(value)
        remaining = remaining[end:].strip()
    return values


class WorkerEndpointDiscoveryTests(unittest.TestCase):
    def test_phone_worker_env_quoted_export_comments_and_unrelated_tokens(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'worker.env'
            path.write_text(f'CORE_WORKER_TOKEN={WORKER_SECRET}\n'
                            'export CORE_WORKER_VPS_URL="http://100.101.102.103:10000" # already paired\n'
                            'UNRELATED=value\n')
            with patch.dict(os.environ, {'PHONE_WORKER_ENV': str(path)}, clear=True), \
                 patch('subprocess.run', side_effect=AssertionError('no CLI on Android')):
                report = client.discover_worker_endpoint()
            self.assertEqual(report['publish_url'], PRIVATE_URL)
            self.assertEqual(report['transport'], 'tailscale')
            self.assertEqual(report['vps_host'], '100.101.102.103')
            self.assertNotIn(WORKER_SECRET, json.dumps(report))

    def test_explicit_file_and_worker_base_url_fallback_are_supported(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'worker.env'
            path.write_text("CORE_WORKER_BASE_URL='http://100.101.102.103:10000/'\n")
            self.assertEqual(client.discover_worker_endpoint(path)['publish_url'], PRIVATE_URL)

    def test_environment_address_wins_without_reading_worker_file(self):
        with patch.dict(os.environ, {'CORE_WORKER_VPS_URL': 'http://100.101.102.103:10000'}, clear=True), \
             patch.object(Path, 'read_text', side_effect=AssertionError('worker file unnecessary')):
            self.assertEqual(client.discover_worker_endpoint()['publish_url'], PRIVATE_URL)

    def test_shell_commands_are_never_evaluated_and_later_literal_is_used(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'worker.env'
            path.write_text('CORE_WORKER_VPS_URL=$(touch marker)\n'
                            'CORE_WORKER_VPS_URL=`echo http://100.101.102.103:10000`\n'
                            'CORE_WORKER_VPS_URL="http://100.101.102.103:10000"; echo bad\n'
                            'export CORE_WORKER_VPS_URL="http://100.101.102.103:10000"\n')
            with patch('subprocess.run', side_effect=AssertionError('never evaluate a shell')):
                self.assertEqual(client.discover_worker_endpoint(path)['publish_url'], PRIVATE_URL)

    def test_invalid_private_or_credential_address_is_not_discovered(self):
        for url in ('http://public.example:10000', 'http://127.0.0.1:10000',
                    'http://100.10.20.30:10000', 'http://secret@100.101.102.103:10000',
                    'http://100.101.102.103:10000?token=secret'):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temp:
                with patch.dict(os.environ, {'PHONE_WORKER_ENV': str(Path(temp) / 'absent'),
                                             'CORE_WORKER_VPS_URL': url}, clear=True):
                    with self.assertRaises(client.PublishError):
                        client.discover_worker_endpoint()

    def test_existing_https_pairing_keeps_https_policy(self):
        with patch.dict(os.environ, {'CORE_WORKER_VPS_URL': 'https://osaka.example'}, clear=True):
            report = client.discover_worker_endpoint()
        self.assertEqual(report['publish_url'], 'https://osaka.example/api/osaka/cd/publish')
        self.assertEqual(report['transport'], 'https')


class ClientConfigurationCommandTests(unittest.TestCase):
    def run_main(self, arguments):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['configure-cd.py', *arguments]), contextlib.redirect_stdout(output):
            status = client.main()
        return status, printed_json(output.getvalue()), output.getvalue()

    def test_discover_command_prints_only_endpoint_and_mode(self):
        with patch.dict(os.environ, {'CORE_WORKER_VPS_URL': 'http://100.101.102.103:10000',
                                     'CORE_WORKER_TOKEN': WORKER_SECRET}, clear=True), \
             patch.object(client.getpass, 'getpass', side_effect=AssertionError('discovery needs no token')):
            status, values, output = self.run_main(['--discover'])
        self.assertEqual(status, 0)
        self.assertEqual(values[-1]['transport'], 'tailscale')
        self.assertNotIn(WORKER_SECRET, output)

    def test_show_and_check_pass_vpn_mode_and_never_print_tokens(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            client.save_configuration(PRIVATE_URL, PUBLISH_SECRET, path, transport='tailscale')
            with patch.object(client, 'publication_status', side_effect=AssertionError('show is offline')):
                status, values, output = self.run_main(['--config', str(path), '--show'])
            self.assertEqual(status, 0)
            self.assertEqual(values[-1]['transport'], 'tailscale')
            self.assertNotIn(PUBLISH_SECRET, output)
            with patch.object(client, 'publication_status', return_value={'ok': True, 'generation': 2, 'versions': {'example': 'a' * 64}}) as verify:
                status, values, output = self.run_main(['--config', str(path), '--check'])
            self.assertEqual(status, 0)
            self.assertTrue(values[-1]['server_available'])
            self.assertEqual(values[-1]['songs'], 1)
            verify.assert_called_once_with(PRIVATE_URL, PUBLISH_SECRET, transport='tailscale')
            self.assertNotIn(PUBLISH_SECRET, output)

    def test_manual_http_url_requires_explicit_mode_before_reading_a_token(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            with patch.object(client.getpass, 'getpass', side_effect=AssertionError('validate destination first')):
                status, values, output = self.run_main(['--url', 'http://100.101.102.103:10000', '--config', str(path)])
            self.assertEqual(status, 1)
            self.assertFalse(values[-1]['ok'])
            self.assertFalse(path.exists())

    def test_manual_http_url_with_explicit_mode_saves_private_config(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            with patch.object(client.getpass, 'getpass', return_value=PUBLISH_SECRET):
                status, values, output = self.run_main(['--url', 'http://100.101.102.103:10000',
                                                       '--transport', 'tailscale', '--config', str(path)])
            self.assertEqual(status, 0)
            self.assertEqual(client.load_configuration(path)['transport'], 'tailscale')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(PUBLISH_SECRET, output)

    def test_auto_existing_vpn_pairing_uses_separate_hidden_publication_token(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'publish.json'
            environment = {'CORE_WORKER_VPS_URL': 'http://100.101.102.103:10000', 'CORE_WORKER_TOKEN': WORKER_SECRET}
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(client.getpass, 'getpass', return_value=PUBLISH_SECRET) as prompt, \
                 patch('builtins.input', side_effect=AssertionError('existing address avoids public URL prompt')):
                status, values, output = self.run_main(['--config', str(path)])
                configuration = client.load_configuration(path)
            self.assertEqual(status, 0)
            self.assertEqual(configuration, {'publish_url': PRIVATE_URL, 'transport': 'tailscale', 'token': PUBLISH_SECRET})
            prompt.assert_called_once()
            self.assertNotIn(WORKER_SECRET, output)
            self.assertNotIn(PUBLISH_SECRET, output)
            self.assertNotIn(WORKER_SECRET, path.read_text())


if __name__ == '__main__':
    unittest.main()
