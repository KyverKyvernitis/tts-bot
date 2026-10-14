"""Explicit paired Tailscale transport, without a phone-side CLI or real network."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/ttv-cd'))
sys.path.insert(0, str(ROOT))
import cd_publish as publication
from osaka_cd import transport as policy

TOKEN = 'isolated-publication-test-secret-with-32-characters'
PRIVATE_URL = 'http://100.101.102.103:10000/api/osaka/cd/publish'
HTTPS_URL = 'https://osaka.example/api/osaka/cd/publish'


class Response:
    def __init__(self, value, status=200):
        self.value = value
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        return json.dumps(self.value).encode('utf-8')


class TailscalePolicyTests(unittest.TestCase):
    def test_private_ipv4_and_ipv6_boundaries_require_explicit_vpn_mode(self):
        for host in ('100.64.0.0', '100.127.255.255', '100.101.102.103',
                     '[fd7a:115c:a1e0::1]', '[fd7a:115c:a1e0:ffff:ffff:ffff:ffff:ffff]'):
            url = f'http://{host}:10000/api/osaka/cd/publish'
            with self.subTest(host=host):
                self.assertEqual(publication.validate_url(url, transport='tailscale'), url)
                with self.assertRaises(publication.PublishError):
                    publication.validate_url(url)

    def test_public_lan_loopback_cgnat_outside_tailnet_and_dns_are_refused(self):
        for host in ('100.63.255.255', '100.128.0.0', '1.1.1.1', '127.0.0.1',
                     '192.168.0.10', '10.0.0.10', '169.254.169.254', 'osaka.example',
                     'osaka.tailnet.ts.net', '[::1]', '[fd7a:115c:a1df::1]',
                     '[fd7a:115c:a1e1::1]', '[::ffff:100.101.102.103]'):
            with self.subTest(host=host), self.assertRaises(publication.PublishError):
                publication.validate_url(f'http://{host}:10000/api/osaka/cd/publish', transport='tailscale')

    def test_unknown_mode_and_malformed_credential_urls_are_refused(self):
        with self.assertRaises(publication.PublishError):
            publication.validate_url(PRIVATE_URL, transport='automatic')
        for url in (
            'http://user:password@100.101.102.103:10000/api/osaka/cd/publish',
            PRIVATE_URL + '?token=not-a-token', PRIVATE_URL + '#fragment',
            PRIVATE_URL + '/other', PRIVATE_URL.replace(':10000/', ':70000/'),
            PRIVATE_URL.replace(':10000/', ':invalid/'),
            PRIVATE_URL.replace(':10000/', ':0/'),
            'http://[fd7a:115c:a1e0::1%25tun0]:10000/api/osaka/cd/publish',
            '\t' + PRIVATE_URL, PRIVATE_URL + '\n',
            PRIVATE_URL.replace('/api/', '/a\tpi/'),
            PRIVATE_URL.replace('/api/', '/api\\/'),
        ):
            with self.subTest(url=repr(url)), self.assertRaises(publication.PublishError):
                publication.validate_url(url, transport='tailscale')

    def test_malformed_json_transport_is_a_stable_local_configuration_error(self):
        for mode in ([], {}, False, 1, None):
            with self.subTest(mode=mode), self.assertRaises(publication.PublishError):
                publication.validate_url(PRIVATE_URL, transport=mode)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            path.write_text(json.dumps({'version': 1, 'publish_url': PRIVATE_URL, 'token': TOKEN, 'transport': []}))
            path.chmod(0o600)
            with self.assertRaises(publication.PublishError):
                publication.load_configuration(path)

    def test_base_url_normalization_uses_the_same_private_policy(self):
        self.assertEqual(policy.normalize_publish_url('http://100.101.102.103:10000', transport='tailscale'), PRIVATE_URL)
        self.assertEqual(policy.normalize_publish_url('https://osaka.example'), HTTPS_URL)
        with self.assertRaises(ValueError):
            policy.normalize_publish_url('http://100.101.102.103:10000')

    def test_https_configuration_remains_compatible_and_private(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            report = publication.save_configuration(HTTPS_URL, TOKEN, path)
            self.assertEqual(publication.load_configuration(path), {'publish_url': HTTPS_URL, 'token': TOKEN})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(TOKEN, json.dumps(report))

    def test_paired_private_configuration_preserves_explicit_mode_without_cli(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            with patch('subprocess.run', side_effect=AssertionError('Android VPN app has no Termux CLI')):
                report = publication.save_configuration(PRIVATE_URL, TOKEN, path, transport='tailscale')
                configuration = publication.load_configuration(path)
            self.assertEqual(configuration, {'publish_url': PRIVATE_URL, 'token': TOKEN, 'transport': 'tailscale'})
            self.assertEqual(json.loads(path.read_text())['transport'], 'tailscale')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(TOKEN, json.dumps(report))

    def test_http_environment_url_requires_explicit_environment_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'absent.json'
            environment = {'OSAKA_CD_PUBLISH_URL': PRIVATE_URL, 'OSAKA_CD_PUBLISH_TOKEN': TOKEN}
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(publication.PublishError):
                    publication.load_configuration(path)
            environment['OSAKA_CD_PUBLISH_TRANSPORT'] = 'tailscale'
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(publication.load_configuration(path)['transport'], 'tailscale')

    def test_invalid_environment_override_cannot_weaken_a_private_pairing(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / 'publish.json'
            publication.save_configuration(PRIVATE_URL, TOKEN, path, transport='tailscale')
            with patch.dict(os.environ, {'OSAKA_CD_PUBLISH_URL': 'http://example.test/api/osaka/cd/publish'}):
                with self.assertRaises(publication.PublishError):
                    publication.load_configuration(path)

    def test_private_transport_bypasses_inherited_proxy_and_keeps_bearer(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://untrusted-proxy.example:3128'}), \
             patch.object(publication, 'build_opener') as build:
            build.return_value.open.return_value = Response({'ok': True, 'versions': {}})
            report = publication.publication_status(PRIVATE_URL, TOKEN, transport='tailscale')
            handlers = build.call_args.args
            proxy_handlers = [handler for handler in handlers if isinstance(handler, ProxyHandler)]
            self.assertEqual(len(proxy_handlers), 1)
            self.assertEqual(proxy_handlers[0].proxies, {})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, PRIVATE_URL.rsplit('/', 1)[0] + '/status')
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + TOKEN)
            self.assertNotIn(TOKEN, json.dumps(report))

    def test_https_preserves_existing_proxy_behavior(self):
        with patch.object(publication, 'build_opener') as build:
            build.return_value.open.return_value = Response({'ok': True, 'versions': {}})
            publication.publication_status(HTTPS_URL, TOKEN)
            self.assertFalse(any(isinstance(handler, ProxyHandler) for handler in build.call_args.args))

    def test_status_cannot_change_origin_scheme_or_endpoint_and_never_sends_token(self):
        for status in (
            'http://100.101.102.104:10000/api/osaka/cd/jobs/fixture',
            'http://100.101.102.103:10001/api/osaka/cd/jobs/fixture',
            'https://100.101.102.103:10000/api/osaka/cd/jobs/fixture',
            '//public.example/api/osaka/cd/jobs/fixture', '/other/endpoint',
            '/api/osaka/cd/jobs/fixture?token=x', '/api/osaka/cd/jobs/fixture#fragment',
            '/api/osaka/cd/jo\tbs/fixture', '\n/api/osaka/cd/jobs/fixture',
        ):
            with self.subTest(status=repr(status)), patch.object(publication, 'build_opener') as build:
                with self.assertRaises(publication.PublishError):
                    publication.publication_status(PRIVATE_URL, TOKEN, status_url=status, transport='tailscale')
                build.assert_not_called()

    def test_private_redirect_cannot_forward_authorization(self):
        self.assertIsNone(publication._NoRedirect().redirect_request(None, None, 302, '', {}, PRIVATE_URL))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'real audio validation requires ffmpeg and ffprobe')
class TailscaleOutboxIntegrationTests(unittest.TestCase):
    def test_real_validated_package_completes_snapshot_upload_poll_without_false_installation(self):
        from catalog_import import _export, preview_manifest
        from package_validation import score_sha256

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mp3 = root / 'sample.mp3'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
                            '-codec:a', 'libmp3lame', '-y', str(mp3)], check=True)
            song = {'id': 'example', 'name': 'Example', 'artist': 'Artist', 'excerpt': 'Test',
                    'source_url': '', 'source_sha256': 'a' * 64, 'duration_ms': 500, 'revision': 1,
                    'notes': [{'midi_note': 69, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]}
            song['score_sha256'] = score_sha256(song)
            archive = root / 'example.zip'
            _export(song, None, preview_manifest(song, mp3.read_bytes()), None, mp3.read_bytes(), archive, sample_status='ready')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            config = {'publish_url': PRIVATE_URL, 'token': TOKEN, 'transport': 'tailscale'}
            box = publication.PublicationOutbox(root, config)
            box.enqueue({'id': 'example', 'zip': str(archive), 'zip_sha256': digest})
            job_id = 'b' * 64
            received = {'ok': True, 'state': 'received', 'job_id': job_id, 'installed': False,
                        'status_url': '/api/osaka/cd/jobs/' + job_id}
            activated = {**received, 'state': 'activated', 'applied': True, 'installed': True, 'generation': 1}
            with patch.object(publication, 'build_opener') as build:
                build.return_value.open.side_effect = [
                    Response({'ok': True, 'versions': {}}), Response(received, status=202), Response(activated)]
                first = box.process(force=True)
                self.assertEqual(len(first), 1)
                self.assertFalse(first[0]['installed'])
                self.assertIn(digest, box.jobs)
                restarted = publication.PublicationOutbox(root, config)
                second = restarted.process(force=True)
                self.assertTrue(second[0]['installed'])
                self.assertEqual(restarted.jobs, {})
                self.assertTrue(restarted.completed[digest]['installed'])
                requests = [call.args[0] for call in build.return_value.open.call_args_list]
                self.assertEqual([request.method for request in requests], ['GET', 'POST', 'GET'])
                self.assertEqual(requests[1].data, archive.read_bytes())
                self.assertEqual(json.loads(requests[1].headers['X-cd-expected-versions']), {'example': None})
                for request in requests:
                    self.assertEqual(request.headers['Authorization'], 'Bearer ' + TOKEN)
                    self.assertTrue(request.full_url.startswith('http://100.101.102.103:10000/api/osaka/cd/'))
                for call in build.call_args_list:
                    self.assertTrue(any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in call.args))
            self.assertNotIn(TOKEN, json.dumps(first + second))
            self.assertNotIn(TOKEN, box.path.read_text())


if __name__ == '__main__':
    unittest.main()
