from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/start-phone-worker.sh"


class SupervisorSourceHashTests(unittest.TestCase):
    def _needs_restart(self, running: dict, desired: dict | None, *, pid=1234) -> bool:
        source = SCRIPT.read_text()
        function = re.search(r"(?ms)^runtime_source_changed_for_pid\(\) \{\n.*?^\}", source).group()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status = root / "runtime-status.json"
            status.write_text(json.dumps(running))
            if desired is not None:
                (root / "phone-worker-release.json").write_text(json.dumps(desired))
            shell = "\n".join((
                "PYTHON_BIN=" + shlex.quote(sys.executable),
                "RUNTIME_STATUS_JSON=" + shlex.quote(str(status)),
                "active_release_dir() { printf '%s\\n' " + shlex.quote(str(root)) + "; }",
                function,
                f"runtime_source_changed_for_pid {pid}",
            ))
            result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            return result.returncode == 0

    def test_new_sources_restart_even_when_the_version_is_unchanged(self):
        running = {"pid": 1234, "runtime_kind": "termux", "version": "1.11.19", "source_hash": "a" * 64}
        desired = {"version": "1.11.19", "source_hash": "b" * 64}
        self.assertTrue(self._needs_restart(running, desired))

    def test_current_sources_keep_the_healthy_process(self):
        running = {"pid": 1234, "runtime_kind": "termux", "source_hash": "a" * 64}
        self.assertFalse(self._needs_restart(running, {"source_hash": "a" * 64}))

    def test_recovery_does_not_require_a_local_http_port(self):
        running = {"pid": 1234, "runtime_kind": "termux", "http_port": None, "source_hash": "a" * 64}
        self.assertTrue(self._needs_restart(running, {"source_hash": "b" * 64}))

    def test_unconfirmed_process_or_unknown_identity_is_not_restarted(self):
        base = {"pid": 1234, "runtime_kind": "termux", "source_hash": "a" * 64}
        for running, desired in (
            ({**base, "pid": 1235}, {"source_hash": "b" * 64}),
            ({**base, "runtime_kind": "apk"}, {"source_hash": "b" * 64}),
            ({**base, "source_hash": ""}, {"source_hash": "b" * 64}),
            (base, {"source_hash": "invalid"}),
            (base, None),
        ):
            with self.subTest(running=running, desired=desired):
                self.assertFalse(self._needs_restart(running, desired))
