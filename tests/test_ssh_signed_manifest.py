"""SSH update manifests retain authenticated same-origin bootstrap semantics."""
import ast
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = {"_safe_agent_release_hash", "_core_worker_response_hmac", "_signed_agent_response_headers",
             "_read_ssh_manifest_registry", "_ssh_manifest_origin", "_agent_manifest_for_transport",
             "core_worker_agent_latest"}


def load_routes():
    tree = ast.parse((ROOT / "webserver.py").read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            node.decorator_list = []
            selected.append(node)
    namespace = {"Path": Path, "json": json, "os": os, "stat": stat, "re": re,
                 "urlsplit": urlsplit, "hmac": hmac, "hashlib": hashlib,
                 "time": SimpleNamespace(time=lambda: 1234567890)}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "webserver.py", "exec"), namespace)
    return namespace


class Response:
    def __init__(self, body, status, mimetype):
        self.data = body
        self.status_code = status
        self.mimetype = mimetype
        self.headers = {}


class SignedManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ssh-manifest-test-", dir=ROOT.parent)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.manifest = {"schema": "core-phone-worker-release-v2", "version": "1.11.42",
                         "source_hash": "a" * 64, "sha256": "b" * 64, "bytes": 12345,
                         "members": [{"path": "phone_worker.py", "sha256": "c" * 64}],
                         "min_bootstrap_version": "1.0.0",
                         "url": "http://100.103.240.118:10000/core-worker/agent/releases/" + "a" * 64 + ".zip"}
        self.raw = (json.dumps(self.manifest, indent=2) + "\n").encode()
        (self.folder / "latest.json").write_bytes(self.raw)
        self.ns = load_routes()
        self.request = SimpleNamespace(remote_addr="127.0.0.1", scheme="http", host="127.0.0.1:19000",
                                       args={"worker_id": "phone-one"}, headers={})
        self.ns.update(request=self.request, jsonify=lambda value: value,
                       app=SimpleNamespace(response_class=Response, logger=Mock()),
                       _authenticate_core_worker_agent_request=lambda worker: (True, None, "worker-existing-token"),
                       _core_worker_agent_root=lambda: self.folder)
        self.registry = {"marker": "Managed by Osaka Phone SSH v1.", "schema": 1,
                         "workers": {"phone-one": {"state": "enrolled", "client_public_key": "public-only",
                                                   "local_port": 19000, "bootstrap_origin": "http://127.0.0.1:19000"}}}
        self.real_reader = self.ns["_read_ssh_manifest_registry"]
        self.reader = Mock(return_value=self.registry)
        self.ns["_read_ssh_manifest_registry"] = self.reader

    def route(self):
        return self.ns["core_worker_agent_latest"]()

    def test_private_origin_is_rebased_without_changing_archive_identity_and_signed_afterward(self):
        response = self.route()
        data = json.loads(response.data)
        self.assertEqual(data["url"], "http://127.0.0.1:19000/core-worker/agent/releases/" + "a" * 64 + ".zip")
        self.assertEqual({k: v for k, v in data.items() if k != "url"},
                         {k: v for k, v in self.manifest.items() if k != "url"})
        stamp = response.headers["X-Core-Worker-Timestamp"]
        expected = hmac.new(b"worker-existing-token", stamp.encode() + b"\n" + response.data, hashlib.sha256).hexdigest()
        self.assertEqual(response.headers["X-Core-Worker-Signature"], "sha256=" + expected)
        stale = hmac.new(b"worker-existing-token", stamp.encode() + b"\n" + self.raw, hashlib.sha256).hexdigest()
        self.assertNotEqual(stale, expected)
        self.assertEqual((self.folder / "latest.json").read_bytes(), self.raw)

    def test_old_bootstrap_same_origin_check_accepts_ssh_manifest_without_weakening_validation(self):
        source = ROOT / "tests/fixtures/phone_worker_bootstrap_1_0_0.py"
        spec = importlib.util.spec_from_file_location("old_bootstrap_signed_ssh_test", source)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        response = self.route()
        checked = bootstrap._validate_manifest(json.loads(response.data), "http://127.0.0.1:19000")
        self.assertEqual(checked["source_hash"], self.manifest["source_hash"])
        with self.assertRaises(ValueError):
            bootstrap._validate_manifest(json.loads(response.data), "http://100.103.240.118:10000")

    def test_public_client_cannot_request_loopback_origin_by_forging_host(self):
        self.request.remote_addr = "198.51.100.23"
        self.assertEqual(self.route().data, self.raw)
        self.reader.assert_not_called()

    def test_foreign_host_forwarded_header_or_wrong_scheme_cannot_choose_origin(self):
        for host, scheme in (("attacker.example", "http"), ("127.0.0.1:19001", "http"),
                             ("127.0.0.1:19000", "https")):
            with self.subTest(host=host, scheme=scheme):
                self.request.host, self.request.scheme = host, scheme
                self.request.headers = {"X-Forwarded-Host": "127.0.0.1:19000"}
                self.assertEqual(self.route().data, self.raw)

    def test_other_worker_and_unenrolled_key_keep_original_manifest(self):
        self.request.args["worker_id"] = "phone-two"
        self.assertEqual(self.route().data, self.raw)
        self.request.args["worker_id"] = "phone-one"
        self.registry["workers"]["phone-one"]["state"] = "prepared"
        self.assertEqual(self.route().data, self.raw)

    def test_custom_registered_port_is_used_only_with_matching_host(self):
        row = self.registry["workers"]["phone-one"]
        row.update(local_port=19008, bootstrap_origin="http://127.0.0.1:19008")
        self.request.host = "127.0.0.1:19008"
        self.assertTrue(json.loads(self.route().data)["url"].startswith("http://127.0.0.1:19008/"))

    def test_invalid_trust_registry_fails_closed_without_signing_untrusted_origin(self):
        self.registry["workers"]["phone-one"]["bootstrap_origin"] = "https://attacker.example"
        body, status = self.route()
        self.assertEqual(status, 500)
        self.assertNotIn("attacker", json.dumps(body))
        self.assertNotIn("token", json.dumps(body))

    def test_authentication_failure_happens_before_private_registry_or_manifest_access(self):
        self.ns["_authenticate_core_worker_agent_request"] = lambda worker: (False, ({"ok": False}, 403), "")
        self.assertEqual(self.route(), ({"ok": False}, 403))
        self.reader.assert_not_called()

    def test_published_release_alias_is_also_rebased(self):
        self.manifest["release_url"] = self.manifest["url"]
        (self.folder / "latest.json").write_text(json.dumps(self.manifest))
        response = self.route()
        data = json.loads(response.data)
        self.assertEqual(data["url"], data["release_url"])

    def test_symlink_or_writable_registry_is_never_trusted(self):
        path = self.folder / "registry.json"
        path.write_text(json.dumps(self.registry))
        link = self.folder / "link.json"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            self.real_reader(link, owner_uid=os.getuid())
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            self.real_reader(path, owner_uid=os.getuid())

    def test_missing_registry_is_legacy_compatible(self):
        self.assertEqual(self.real_reader(self.folder / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
