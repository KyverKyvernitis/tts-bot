"""A promoted release must replace a healthy process with the same version."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


BOOTSTRAP = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker_bootstrap.py"


class BootstrapRestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        values = {key: value for key, value in os.environ.items() if not key.startswith("PHONE_WORKER_")}
        values.update({
            "PHONE_WORKER_DIR": str(self.root / "phone-worker"),
            "PHONE_WORKER_RUNTIME_ROOT": str(self.root / "runtime"),
            "PHONE_WORKER_STATE_DIR": str(self.root / "state"),
        })
        self.env = patch.dict(os.environ, values, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        name = "phone_worker_bootstrap_restart_test"
        spec = importlib.util.spec_from_file_location(name, BOOTSTRAP)
        self.bootstrap = importlib.util.module_from_spec(spec)
        modules = patch.dict(sys.modules, {name: self.bootstrap})
        modules.start()
        self.addCleanup(modules.stop)
        spec.loader.exec_module(self.bootstrap)

    def test_default_pid_reads_the_supervisors_file(self):
        installed = self.root / "phone-worker"
        installed.mkdir()
        (installed / "phone-worker.pid").write_text("43210\n")
        state = self.root / "state"
        state.mkdir()
        (state / "phone-worker.pid").write_text("98765\n")

        self.assertEqual(self.bootstrap._pid_path(), installed / "phone-worker.pid")
        self.assertEqual(self.bootstrap._read_pid(), 43210)

    def test_custom_pid_file_is_preserved(self):
        custom = self.root / "custom.pid"
        custom.write_text("32100\n")
        with patch.dict(os.environ, {"PHONE_WORKER_PID_FILE": str(custom)}):
            self.assertEqual(self.bootstrap._pid_path(), custom)
            self.assertEqual(self.bootstrap._read_pid(), 32100)

    def _assert_force_restart(self, active_release):
        installed = self.root / "phone-worker"
        installed.mkdir()
        legacy_script = installed / "start-phone-worker.sh"
        legacy_script.write_text("#!/bin/sh\n")
        expected_script = legacy_script
        if active_release:
            release = self.root / "runtime" / "releases" / ("a" * 64)
            release.mkdir(parents=True)
            expected_script = release / "start-phone-worker.sh"
            expected_script.write_text("#!/bin/sh\n")
            (self.root / "runtime" / "current").symlink_to(release, target_is_directory=True)
        process = Mock()
        with patch.object(self.bootstrap.subprocess, "Popen", return_value=process) as launch, \
                patch.object(self.bootstrap.shutil, "which", return_value="/bin/bash"):
            self.assertIs(self.bootstrap._start_runtime(), process)
        args, kwargs = launch.call_args
        self.assertEqual(args[0], ["/bin/bash", str(expected_script), "--force-restart"])
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["env"]["PHONE_WORKER_RUNTIME_ROOT"], str(self.root / "runtime"))
        if active_release:
            self.assertEqual(kwargs["env"]["PHONE_WORKER_RELEASE_DIR"], str(release))

    def test_promoted_release_forces_restart_using_its_supervisor(self):
        self._assert_force_restart(True)

    def test_legacy_install_also_forces_restart(self):
        self._assert_force_restart(False)


if __name__ == "__main__":
    unittest.main()
