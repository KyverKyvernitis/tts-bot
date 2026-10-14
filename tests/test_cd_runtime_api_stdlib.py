"""Route-handler contract tests with a small Flask boundary adapter.

These execute production handlers and publisher scheduling without installing
Flask. They are not HTTP/framework integration; the real Flask cases live in
test_cd_v9_end_to_end.py and explicitly skip when Flask is unavailable.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from osaka_cd import events
from osaka_cd.store import StoreError


class RouteApp:
    def __init__(self, store):
        self.extensions = {"osaka_cd_store": store}
        self.handlers = {}

    def post(self, path):
        return self._register("POST", path)

    def get(self, path):
        return self._register("GET", path)

    def _register(self, method, path):
        def decorate(callback):
            self.handlers[(method, path)] = callback
            return callback
        return decorate


def load_handlers(request):
    adapter = ModuleType("flask")
    adapter.jsonify = lambda value: value
    adapter.request = request
    path = Path(__file__).resolve().parents[1] / "updater/api_cd.py"
    specification = importlib.util.spec_from_file_location("cd_api_boundary_fixture", path)
    module = importlib.util.module_from_spec(specification)
    with patch.dict(sys.modules, {"flask": adapter}):
        specification.loader.exec_module(module)
    return module


class RuntimeApiHandlerContractTests(unittest.TestCase):
    def setUp(self):
        self.token = "isolated-private-handler-test-32-characters"
        self.environment = patch.dict(os.environ, {
            "OSAKA_CD_PUBLISH_ENABLED": "1", "OSAKA_CD_PUBLISH_TOKEN": self.token})
        self.environment.start()
        self.payload = None
        self.request = SimpleNamespace(headers={"Authorization": "Bearer " + self.token},
            mimetype="application/zip", content_length=None, stream=io.BytesIO(),
            get_json=lambda **kwargs: self.payload)
        self.module = load_handlers(self.request)
        self.job = {"ok": True, "state": "received", "job_id": "a" * 64,
                    "installed": False, "published_to_vps": True}
        self.store = SimpleNamespace(outstanding=Mock(return_value=[]),
            next_retry_delay=Mock(return_value=None), process=Mock(),
            receive=Mock(return_value=self.job), job=Mock(return_value=self.job),
            enqueue_rollback=Mock(return_value=self.job), rollback=Mock(side_effect=AssertionError("must queue")),
            snapshot=Mock(return_value={"generation": None, "initialized": False,
                "activated_generation": None, "versions": {}, "root": "/private/fixture"}))
        self.app = RouteApp(self.store)
        self.module.register_cd_publish_route(self.app, Mock(side_effect=AssertionError("code updater must not run")))

    def tearDown(self):
        self.app.extensions["osaka_cd_publisher"].shutdown()
        self.environment.stop()

    def call(self, method, path, *args):
        return self.app.handlers[(method, path)](*args)

    def upload(self, data=b"fixture-archive"):
        self.request.stream = io.BytesIO(data)
        self.request.content_length = len(data)
        self.request.headers["X-CD-Package-SHA256"] = hashlib.sha256(data).hexdigest()
        return self.call("POST", "/api/osaka/cd/publish")

    def test_authentication_covers_all_endpoints_and_disabled_route(self):
        self.request.headers.clear()
        for method, path, arguments in (
            ("POST", "/api/osaka/cd/publish", ()), ("GET", "/api/osaka/cd/status", ()),
            ("GET", "/api/osaka/cd/jobs/<job_id>", ("a" * 64,)),
            ("POST", "/api/osaka/cd/rollback", ())):
            response, status = self.call(method, path, *arguments)
            self.assertEqual(status, 403)
            self.assertNotIn(self.token, str(response))
        with patch.dict(os.environ, {"OSAKA_CD_PUBLISH_ENABLED": "0"}):
            self.assertEqual(self.call("GET", "/api/osaka/cd/status")[1], 404)

    def test_receipt_never_claims_installation_and_forwards_cas(self):
        self.request.headers["X-CD-Expected-Versions"] = '{"demo":null}'
        response, status = self.upload()
        self.assertEqual(status, 202)
        self.assertFalse(response["installed"])
        self.assertEqual(self.store.receive.call_args.args[2], {"demo": None})

    def test_checksum_content_type_and_size_fail_before_storage(self):
        self.request.mimetype = "text/plain"
        self.assertEqual(self.upload()[1], 415)
        self.request.mimetype = "application/zip"
        with patch.object(self.module, "MAX_BYTES", 1):
            self.assertEqual(self.upload()[1], 413)
        self.request.content_length = 3
        self.request.stream = io.BytesIO(b"abc")
        self.request.headers["X-CD-Package-SHA256"] = "f" * 64
        self.assertEqual(self.call("POST", "/api/osaka/cd/publish")[1], 400)
        self.store.receive.assert_not_called()

    def test_sqlite_storage_errors_are_sanitized_503(self):
        self.store.receive.side_effect = sqlite3.OperationalError("private-storage-path")
        response, status = self.upload()
        self.assertEqual(status, 503)
        self.assertNotIn("private-storage-path", str(response))
        self.store.job.side_effect = sqlite3.OperationalError("private-storage-path")
        self.assertEqual(self.call("GET", "/api/osaka/cd/jobs/<job_id>", "a" * 64)[1], 503)
        self.store.snapshot.side_effect = sqlite3.OperationalError("private-storage-path")
        self.assertEqual(self.call("GET", "/api/osaka/cd/status")[1], 503)

    def test_status_uses_strict_pointer_and_hides_filesystem_root(self):
        response, status = self.call("GET", "/api/osaka/cd/status")
        self.assertEqual(status, 200)
        self.store.snapshot.assert_called_once_with(strict=True, advertise_legacy=True)
        self.assertNotIn("root", response)

    def test_rollback_queues_request_without_rendering(self):
        self.payload = {"song_id": "demo", "generation": "g-old", "expected_version": "f" * 64}
        response, status = self.call("POST", "/api/osaka/cd/rollback")
        self.assertEqual(status, 202)
        self.assertFalse(response["installed"])
        self.store.enqueue_rollback.assert_called_once_with("demo", "g-old", "f" * 64)
        self.store.rollback.assert_not_called()

    def test_quota_and_conflict_have_explicit_response_codes(self):
        for code, expected_status in (("storage_quota", 503), ("conflict", 409), ("not_found", 404)):
            self.store.receive.side_effect = StoreError("safe failure", code)
            self.assertEqual(self.upload()[1], expected_status)

    def test_unexpected_worker_failure_waits_before_reentering_queue(self):
        publisher = self.app.extensions["osaka_cd_publisher"]
        self.store.process.side_effect = sqlite3.OperationalError("storage unavailable")
        self.store.outstanding.reset_mock()
        with patch.object(self.module.threading, "Timer") as timer, self.assertLogs(self.module._log, level="ERROR"):
            publisher._run("a" * 64)
            publisher.kick()
            publisher.kick()
            timer.assert_called_once()
            self.assertGreaterEqual(timer.call_args.args[0], 29)
            self.store.outstanding.assert_not_called()
        self.store.process.assert_called_once()


class EventSnapshotTests(unittest.TestCase):
    def test_listener_changes_never_mutate_published_snapshot(self):
        value = {"generation": "g-one", "versions": {"demo": "a" * 64}}
        seen = []
        def changed(snapshot):
            snapshot["versions"]["demo"] = "altered"
        def observed(snapshot):
            seen.append(snapshot["versions"]["demo"])
        stop_change = events.subscribe(changed)
        stop_observe = events.subscribe(observed)
        try:
            events.publish(value)
        finally:
            stop_change()
            stop_observe()
        self.assertEqual(seen, ["a" * 64])
        self.assertEqual(value["versions"]["demo"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
