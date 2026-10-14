"""Publication client checks with real validated local artifacts, no network."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'))
from catalog_import import _export, preview_manifest
from package_validation import score_sha256
import cd_publish


class Response:
    status = 202
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return json.dumps(self.value).encode()


class PublishClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cd-publish-client-')
        self.root = Path(self.temp.name)
        mp3 = self.root / 'sample.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
                        '-codec:a', 'libmp3lame', '-y', str(mp3)], check=True)
        self.song = {'id':'example', 'name':'Example', 'artist':'Artist', 'excerpt':'Test',
                     'source_url':'', 'source_sha256':'a'*64, 'duration_ms':500, 'revision':1,
                     'notes':[{'midi_note':69,'start_ms':0,'duration_ms':500,'velocity':100}]}
        self.song['score_sha256'] = score_sha256(self.song)
        self.zip = self.root / 'example.zip'
        _export(self.song, None, preview_manifest(self.song, mp3.read_bytes()), None, mp3.read_bytes(),
                self.zip, sample_status='ready')
        self.token = 'test-only-secret-that-is-long-enough'

    def tearDown(self): self.temp.cleanup()

    def upload(self, result):
        with patch.object(cd_publish, 'build_opener') as build:
            build.return_value.open.return_value = Response(result)
            report = cd_publish.publish_package(self.zip, 'https://example.test/api/osaka/cd/publish', self.token)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.data, self.zip.read_bytes())
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + self.token)
            self.assertNotIn(self.token, json.dumps(report))
            return report

    def test_queued_is_receipt_and_not_installation(self):
        report = self.upload({'ok':True, 'state':'queued', 'candidate_id':'fixture'})
        self.assertTrue(report['published_to_vps'])
        self.assertFalse(report['installed'])

    def test_unchanged_is_successful_idempotent_receipt(self):
        report = self.upload({'ok':True, 'state':'unchanged', 'candidate_id':None})
        self.assertTrue(report['published_to_vps'])
        self.assertFalse(report['installed'])

    def test_http_credentials_in_url_and_short_token_are_refused(self):
        for url, token in [('http://example.test/', self.token), ('https://user:pw@example.test/', self.token),
                           ('https://example.test/', 'short')]:
            with self.assertRaises(ValueError), patch.object(cd_publish, 'build_opener') as build:
                cd_publish.publish_package(self.zip, url, token)
            build.assert_not_called()

    def test_redirects_do_not_forward_authorization(self):
        self.assertIsNone(cd_publish._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test'))

    def test_failed_durable_job_is_a_valid_status_response_and_hides_server_text(self):
        failed = {'ok': False, 'state': 'failed', 'job_id': 'a'*64,
                  'error_code': 'conflict', 'error': self.token}
        with patch.object(cd_publish, 'build_opener') as build:
            build.return_value.open.return_value = Response(failed)
            report = cd_publish.publication_status('https://example.test/api/osaka/cd/publish', self.token, job_id='a'*64)
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['error'], 'conflict')
        self.assertFalse(report['installed'])
        self.assertNotIn(self.token, json.dumps(report))
        failed['error_code'] = self.token
        self.assertEqual(cd_publish._receipt(failed, 'https://example.test/api/osaka/cd/publish')['error'], 'publication_failed')

    def test_failed_without_durable_job_is_refused(self):
        with patch.object(cd_publish, 'build_opener') as build:
            build.return_value.open.return_value = Response({'ok': False, 'state': 'failed'})
            with self.assertRaisesRegex(cd_publish.PublishError, 'unexpected_response'):
                cd_publish.publication_status('https://example.test/api/osaka/cd/publish', self.token, job_id='a'*64)


if __name__ == '__main__': unittest.main()
