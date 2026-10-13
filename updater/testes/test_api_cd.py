from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from flask import Flask
except ImportError:
    Flask = None


class CDPublishAPITests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        if Flask is not None:
            from updater.api_cd import register_cd_publish_route
            self.app = Flask(__name__)
        else:
            # Run the real handler against a request double when the isolated
            # machine lacks Flask; WSGI integration still needs Flask separately.
            fake_request = types.SimpleNamespace()
            fake_flask = types.ModuleType("flask")
            fake_flask.jsonify = lambda value: value
            fake_flask.request = fake_request
            source = Path(__file__).resolve().parents[1] / "api_cd.py"
            spec = importlib.util.spec_from_file_location("updater.api_cd", source)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"flask": fake_flask}):
                spec.loader.exec_module(module)
            self.module_patch = patch.dict(sys.modules, {"updater.api_cd": module})
            self.module_patch.start()
            register_cd_publish_route = module.register_cd_publish_route
            class FakeApp:
                def post(self, path):
                    def register(handler):
                        self.handler = handler
                        return handler
                    return register
                def test_client(self):
                    app = self
                    class Client:
                        def post(self, path, data, content_type, headers):
                            raw = data.encode() if isinstance(data, str) else data
                            fake_request.mimetype = content_type
                            fake_request.content_length = len(raw)
                            fake_request.headers = headers
                            fake_request.stream = io.BytesIO(raw)
                            payload, status = app.handler()
                            return types.SimpleNamespace(status_code=status, get_json=lambda: payload)
                    return Client()
            self.app = FakeApp()
        def provider(action, payload):
            from pathlib import Path
            self.calls.append((action, payload, Path(payload["zip_path"]).read_bytes()))
            return {"ok": True, "state": "queued", "installed": False, "candidate_id": "test-candidate"}
        register_cd_publish_route(self.app, lambda: provider)
        self.client = self.app.test_client()
        self.env = patch.dict(os.environ, {"OSAKA_CD_PUBLISH_ENABLED": "1", "OSAKA_CD_PUBLISH_TOKEN": "test-token"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        if hasattr(self, "module_patch"):
            self.module_patch.stop()

    def post(self, data=b"ZIP-fixture", **headers):
        return self.client.post("/api/osaka/cd/publish", data=data, content_type="application/zip",
                                headers={"Authorization": "Bearer test-token", **headers})

    def test_requires_opt_in_and_mandatory_secret(self):
        with patch.dict(os.environ, {"OSAKA_CD_PUBLISH_ENABLED": "0"}):
            self.assertEqual(self.post().status_code, 404)
        with patch.dict(os.environ, {"OSAKA_CD_PUBLISH_TOKEN": ""}):
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post(Authorization="Bearer wrong").status_code, 403)
        self.assertEqual(self.calls, [])

    def test_checksum_is_validated_before_enqueue(self):
        self.assertEqual(self.post(**{"X-CD-Package-SHA256": "f" * 64}).status_code, 400)
        self.assertEqual(self.calls, [])
        data = b"fixture"
        result = self.post(data, **{"X-CD-Package-SHA256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(result.status_code, 202)
        self.assertFalse(result.get_json()["installed"])
        self.assertEqual(self.calls[0][0], "publish_cd")
        self.assertEqual(self.calls[0][2], data)

    def test_invalid_content_type_and_oversized_body_are_rejected(self):
        from updater.api_cd import MAX_BYTES
        result = self.client.post("/api/osaka/cd/publish", data="{}", content_type="application/json",
                                  headers={"Authorization": "Bearer test-token"})
        self.assertEqual(result.status_code, 415)
        with patch("updater.api_cd.MAX_BYTES", 4):
            self.assertEqual(self.post(b"12345").status_code, 413)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
