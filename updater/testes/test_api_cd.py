"""Exercise real ingress handlers and SQLite store, without a live Discord bot."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

from osaka_cd.store import RuntimeStore

try:
    from flask import Flask
except ImportError:
    Flask = None

TOKEN = 'isolated-test-token-' + 'a' * 32


class CDPublishAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cd-api-test-')
        folder = Path(self.temp.name)
        self.store = RuntimeStore(folder / 'runtime', folder / 'legacy')
        self.env = patch.dict(os.environ, {'OSAKA_CD_PUBLISH_ENABLED': '1', 'OSAKA_CD_PUBLISH_TOKEN': TOKEN,
                                         'OSAKA_CD_DATA_ROOT': str(folder / 'runtime')})
        self.env.start()
        if Flask is not None:
            from updater.api_cd import register_cd_publish_route
            self.app = Flask(__name__)
        else:
            request_double = types.SimpleNamespace()
            fake_flask = types.ModuleType('flask')
            fake_flask.jsonify = lambda value: value
            fake_flask.request = request_double
            source = Path(__file__).resolve().parents[1] / 'api_cd.py'
            spec = importlib.util.spec_from_file_location('updater.api_cd', source)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {'flask': fake_flask}):
                spec.loader.exec_module(module)
            self.module_patch = patch.dict(sys.modules, {'updater.api_cd': module})
            self.module_patch.start()
            register_cd_publish_route = module.register_cd_publish_route

            class FakeApp:
                def __init__(self):
                    self.extensions, self.handlers = {}, {}
                def _route(self, method, path):
                    def register(handler):
                        self.handlers[method, path] = handler
                        return handler
                    return register
                def post(self, path):
                    return self._route('POST', path)
                def get(self, path):
                    return self._route('GET', path)
                def test_client(self):
                    app = self
                    class Client:
                        def _call(self, method, path, data=b'', content_type='', headers=None, json_body=None):
                            raw = data.encode() if isinstance(data, str) else data
                            request_double.mimetype = content_type
                            request_double.content_length = len(raw)
                            request_double.headers = headers or {}
                            request_double.stream = io.BytesIO(raw)
                            request_double.get_json = lambda silent=False: json_body
                            route = '/api/osaka/cd/jobs/<job_id>' if path.startswith('/api/osaka/cd/jobs/') else path
                            args = [path.rsplit('/', 1)[-1]] if '<job_id>' in route else []
                            payload, status = app.handlers[method, route](*args)
                            return types.SimpleNamespace(status_code=status, get_json=lambda: payload)
                        def post(self, path, data=b'', content_type='', headers=None, json=None):
                            return self._call('POST', path, data, content_type, headers, json)
                        def get(self, path, headers=None):
                            return self._call('GET', path, headers=headers)
                    return Client()
            self.app = FakeApp()
        self.app.extensions['osaka_cd_store'] = self.store
        # Integration stores are real. Suppress scheduling so each test can
        # explicitly distinguish received/applied/activated without a timing race.
        self.schedule_patch = patch('updater.api_cd._Publisher.kick', lambda self: None)
        self.schedule_patch.start()
        self.provider_calls = []
        register_cd_publish_route(self.app, lambda: self.provider_calls.append('must not call updater'))
        self.client = self.app.test_client()
        score = {'id': 'demo', 'name': 'Demo', 'artist': 'Demo', 'excerpt': 'test', 'source_url': '',
                 'source_sha256': 'a' * 64, 'revision': 1, 'duration_ms': 500,
                 'notes': [{'midi_note': 60, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]}
        source = folder / 'input.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('cogs/tts/cd_song_scores.json', json.dumps([score]))
        self.archive = source.read_bytes()

    def tearDown(self):
        publisher = self.app.extensions.get('osaka_cd_publisher')
        if publisher:
            publisher.pool.shutdown(wait=True)
        self.schedule_patch.stop()
        if hasattr(self, 'module_patch'):
            self.module_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def post(self, data=None, **headers):
        return self.client.post('/api/osaka/cd/publish', data=self.archive if data is None else data,
                                content_type='application/zip', headers={'Authorization': f'Bearer {TOKEN}', **headers})

    def get(self, path, **headers):
        return self.client.get(path, headers={'Authorization': f'Bearer {TOKEN}', **headers})

    def test_opt_in_and_long_secret_required_for_all_routes(self):
        for settings, status in (({'OSAKA_CD_PUBLISH_ENABLED': '0'}, 404),
                                 ({'OSAKA_CD_PUBLISH_TOKEN': ''}, 403),
                                 ({'OSAKA_CD_PUBLISH_TOKEN': 'short'}, 403)):
            with patch.dict(os.environ, settings):
                self.assertEqual(self.post().status_code, status)
                self.assertEqual(self.get('/api/osaka/cd/status').status_code, status)
        self.assertEqual(self.post(Authorization='Bearer wrong').status_code, 403)
        self.assertEqual(self.provider_calls, [])

    def test_checksum_checked_before_durable_receipt(self):
        self.assertEqual(self.post(**{'X-CD-Package-SHA256': 'f' * 64}).status_code, 400)
        self.assertEqual(self.store.outstanding(), [])
        response = self.post(**{'X-CD-Package-SHA256': hashlib.sha256(self.archive).hexdigest()})
        self.assertEqual(response.status_code, 202, response.get_json())
        result = response.get_json()
        self.assertEqual(result['state'], 'received')
        self.assertFalse(result['installed'])
        self.assertEqual(self.store.outstanding(), [result['job_id']])
        self.assertEqual(self.provider_calls, [])

    def test_status_distinguishes_disk_commit_from_bot_activation(self):
        received = self.post().get_json()
        self.store.process(received['job_id'])
        response = self.get(received['status_url'])
        self.assertEqual(response.get_json()['state'], 'applied')
        self.assertFalse(response.get_json()['installed'])
        self.store.acknowledge(response.get_json()['generation'])
        self.assertTrue(self.get(received['status_url']).get_json()['installed'])
        current = self.get('/api/osaka/cd/status').get_json()
        self.assertIn('demo', current['versions'])
        self.assertNotIn('root', current)
        self.assertEqual(self.get('/api/osaka/cd/jobs/not-an-id').status_code, 404)

    def test_invalid_content_type_and_oversized_body(self):
        response = self.client.post('/api/osaka/cd/publish', data='{}', content_type='application/json',
                                    headers={'Authorization': f'Bearer {TOKEN}'})
        self.assertEqual(response.status_code, 415)
        with patch('updater.api_cd.MAX_BYTES', 4):
            self.assertEqual(self.post(b'12345').status_code, 413)
        self.assertEqual(self.store.outstanding(), [])

    def test_invalid_zip_and_cas_headers_rejected(self):
        self.assertEqual(self.post(b'not-a-zip').status_code, 422)
        self.assertEqual(self.post(**{'X-CD-Expected-Versions': '{broken'}).status_code, 400)
        self.assertEqual(self.post(**{'X-CD-Expected-Versions': '{"demo": "invalid"}'}).status_code, 409)
        self.assertEqual(self.store.outstanding(), [])

    def test_expected_version_conflict_reported_durably(self):
        received = self.post(**{'X-CD-Expected-Versions': json.dumps({'demo': 'f' * 64})}).get_json()
        self.store.process(received['job_id'])
        result = self.get(received['status_url']).get_json()
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_code'], 'conflict')
        self.assertFalse(self.store.snapshot()['initialized'])


if __name__ == '__main__':
    unittest.main()
