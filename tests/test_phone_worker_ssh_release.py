"""SSH updates retain authenticated origins and a recovery core after rollback."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SSHReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        cls.publisher = load("ssh_release_publisher", ROOT / "scripts/core-worker-automation.py")
        cls.bootstrap = load("ssh_release_bootstrap", PHONE / "phone_worker_bootstrap.py")
        cls.original = load("ssh_release_original_100", ROOT / "tests/fixtures/phone_worker_bootstrap_1_0_0.py")

    def test_one_new_runtime_module_fits_original_bootstrap(self):
        names = [name for name, _ in self.publisher.PHONE_WORKER_FILES]
        self.assertEqual(names.count("phone_worker_runtime/ssh_transport.py"), 1)
        self.assertEqual(len(names) + 1, 64)
        self.assertEqual(self.original.DEFAULT_MAX_MEMBERS, 64)
        self.assertEqual(self.bootstrap.DEFAULT_MAX_MEMBERS, 64)
        self.assertFalse(any("known_hosts" in name or ".env" == Path(name).name or "id_ed25519" in name for name in names))

    def test_publisher_ssh_origin_has_no_overlay_discovery(self):
        with mock.patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "ssh", "CORE_WORKER_SSH_LOCAL_PORT": "19100"}), \
             mock.patch.object(self.publisher.subprocess, "run", side_effect=AssertionError("unexpected external discovery")):
            self.assertEqual(self.publisher._public_base_url(), "http://127.0.0.1:19100")

    def test_manual_ssh_sync_targets_only_the_configured_worker(self):
        with mock.patch.object(sys, "argv", ["core-worker-automation.py", "queue-agent-update", "--worker-id", "phone-test"]), \
             mock.patch.object(self.publisher, "queue_agent_updates", return_value={"ok": True}) as queue, \
             mock.patch.object(self.publisher, "write_status"), \
             mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(self.publisher.main(), 0)
        queue.assert_called_once_with(force=True, only_worker_id="phone-test")

    def test_real_release_zip_is_accepted_by_original_bootstrap(self):
        # Publication remains a real build/hash/ZIP/extraction roundtrip. A
        # missing runtime file is an implementation error, not a skipped case.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.publisher, "AGENT_RELEASE_ROOT", root / "published"), \
                 mock.patch.object(self.publisher, "_public_base_url", return_value="http://127.0.0.1:19000"):
                payload = self.publisher._build_worker_update_payload()
                latest = self.publisher._publish_phone_worker_release(payload)
                validated = self.original._validate_manifest(latest, "http://127.0.0.1:19000")
                archive = root / "published/releases" / (latest["source_hash"] + ".zip")
                self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), latest["sha256"])
                with zipfile.ZipFile(archive) as source:
                    self.assertEqual(len(source.infolist()), 64)
                result = self.original._extract_and_validate(archive, root / "stage", validated)
                self.assertEqual(result["members"], 63)

    def test_ssh_bootstrap_rejects_an_old_overlay_origin_before_fetch(self):
        settings = {"PHONE_WORKER_TRANSPORT": "ssh", "CORE_WORKER_VPS_URL": "http://100.64.0.1:10000", "CORE_WORKER_SSH_LOCAL_PORT": "19000"}
        with mock.patch.object(self.bootstrap, "_migrate_env", return_value={}), \
             mock.patch.object(self.bootstrap, "_load_env", return_value=settings), \
             mock.patch.object(self.bootstrap, "_config_blocker", return_value=""), \
             mock.patch.object(self.bootstrap, "_fetch_latest") as fetch:
            with self.assertRaisesRegex(ValueError, "origem local pareada"):
                self.bootstrap.check_and_apply()
            fetch.assert_not_called()

    def test_ssh_signed_fetch_bypasses_proxy_and_keeps_hmac_verification(self):
        raw = b'{"ready":true}'
        token = "sentinel-shared-worker-token"
        stamp = str(int(time.time()))
        signature = hmac.new(token.encode(), stamp.encode() + b"\n" + raw, hashlib.sha256).hexdigest()
        class Response(io.BytesIO):
            status = 200
            headers = {"X-Core-Worker-Timestamp": stamp, "X-Core-Worker-Signature-Schema": "ts-body-v1", "X-Core-Worker-Signature": "sha256=" + signature}
        handlers = []
        def opener(*values):
            handlers.extend(values)
            result = mock.Mock()
            result.open.return_value = Response(raw)
            return result
        with mock.patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "ssh"}), \
             mock.patch.object(self.bootstrap.urllib.request, "build_opener", side_effect=opener):
            data, _, size, _ = self.bootstrap._signed_request(
                "http://127.0.0.1:19000/core-worker/agent/latest", base_url="http://127.0.0.1:19000",
                token=token, worker_id="phone-test", accept="application/json", timeout=1, max_bytes=1000)
        self.assertEqual(data, raw)
        self.assertEqual(size, len(raw))
        self.assertTrue(any(isinstance(item, self.bootstrap.urllib.request.ProxyHandler) and not item.proxies for item in handlers))
        with self.assertRaisesRegex(ValueError, "origem diferente"):
            self.bootstrap._signed_request("http://100.64.0.1/file", base_url="http://127.0.0.1:19000",
                token=token, worker_id="phone-test", accept="application/json", timeout=1, max_bytes=1000)

    def test_recovery_anchor_survives_old_application_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); staging = root / "stage"; install = root / "installed"
            files = {"phone_worker_runtime/ssh_transport.py": "VALUE='new SSH recovery'\n",
                     "phone_worker_runtime/__init__.py": "", "watch-phone-worker.sh": "#!/bin/bash\ntrue\n",
                     "start-phone-worker.sh": "#!/bin/bash\ntrue\n", "phone_worker_bootstrap.py": "VALUE=1\n",
                     "pair-phone-worker.sh": "#!/bin/bash\ntrue\n"}
            members = []
            for name, text in files.items():
                path = staging / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
                members.append({"path": name, "sha256": hashlib.sha256(text.encode()).hexdigest(), "mode": 0o755 if name.endswith(".sh") else 0o644})
            (staging / "phone-worker-release.json").write_text(json.dumps({"members": members}))
            with mock.patch.object(self.bootstrap, "_install_root", return_value=install):
                self.assertTrue(self.bootstrap._refresh_transport_anchor(staging))
                old = root / "old-release"; old.mkdir()
                (old / "watch-phone-worker.sh").write_text("old incompatible watch")
                (old / "phone-worker-release.json").write_text(json.dumps({"members": []}))
                self.assertFalse(self.bootstrap._refresh_transport_anchor(old))
            self.assertEqual((install / "phone_worker_runtime/ssh_transport.py").read_text(), files["phone_worker_runtime/ssh_transport.py"])
            self.assertEqual((install / "watch-phone-worker.sh").read_text(), files["watch-phone-worker.sh"])

    def test_anchor_tamper_is_rejected_before_any_file_is_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); staging = root / "stage"; staging.mkdir()
            path = staging / "phone_worker_runtime/ssh_transport.py"; path.parent.mkdir(); path.write_text("tampered")
            (staging / "phone-worker-release.json").write_text(json.dumps({"members": [{"path": "phone_worker_runtime/ssh_transport.py", "sha256": "0" * 64}]}))
            install = root / "install"
            with mock.patch.object(self.bootstrap, "_install_root", return_value=install):
                with self.assertRaisesRegex(ValueError, "hash"):
                    self.bootstrap._refresh_transport_anchor(staging)
            self.assertFalse(install.exists())

    def _watch(self, body, *, code="200"):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary); tools = temp / "bin"; tools.mkdir()
            curl = tools / "curl"
            curl.write_text("#!/bin/bash\nwhile (($#)); do if [[ $1 == -o ]]; then out=$2; shift 2; else shift; fi; done\nprintf '%s' " + repr(json.dumps(body)) + " > \"$out\"\nprintf '%s' " + code + "\n")
            curl.chmod(0o755)
            ssh = tools / "ssh"; ssh.write_text("#!/bin/bash\necho unexpected > \"$STATE_DIR/ssh-called\"\nexit 97\n"); ssh.chmod(0o755)
            settings = {**os.environ, "PHONE_WORKER_ENABLED": "true", "PHONE_WORKER_TRANSPORT": "ssh",
                        "PHONE_WORKER_HOST": "127.0.0.1", "PHONE_WORKER_PORT": "18766", "PHONE_WORKER_ID": "phone-test",
                        "PHONE_WORKER_TOKEN": "sentinel-token", "PHONE_WORKER_SSH_USER": "legacy-inbound-user",
                        "ENV_FILE": str(temp / "absent.env"), "REPO_DIR": str(temp), "STATE_DIR": str(temp / "state"),
                        "TMPDIR": str(temp), "PATH": str(tools) + os.pathsep + os.environ.get("PATH", "")}
            result = subprocess.run(["bash", str(ROOT / "scripts/phone-worker-watch.sh")], env=settings, capture_output=True, text=True, timeout=8)
            self.assertFalse((temp / "state/ssh-called").exists())
            return result

    def test_ssh_watch_requires_remote_identity_and_never_attempts_inbound_wake(self):
        for body in ({"ok": False}, {"ok": True, "worker_id": "different-phone"}):
            result = self._watch(body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("resultado: ssh-tunnel-unavailable", result.stdout)
            self.assertNotIn("sentinel-token", result.stdout)
        result = self._watch({"ok": True, "worker_id": "phone-test"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resultado: online", result.stdout)
        self.assertNotIn("open em", result.stdout)


if __name__ == "__main__":
    unittest.main()
