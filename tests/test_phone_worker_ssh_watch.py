"""Run the real Bash watchdog with bounded fake transport/start/update tools."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / "deploy/termux/phone-worker"


class SSHWatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.worker = self.home / "phone-worker"
        self.bin = self.home / "bin"
        self.worker.mkdir()
        self.bin.mkdir()
        self.events = self.home / "events"
        self.watch = self.worker / "watch-phone-worker.sh"
        # The production interpreter is a Termux absolute path. Only relocate
        # that interpreter in this sandbox copy; the tested scheduler is intact.
        self.watch.write_text((PHONE / self.watch.name).read_text().replace(
            "/data/data/com.termux/files/usr/bin/bash", "/bin/bash"))
        self.write_executable(self.bin / "termux-wake-lock", "#!/bin/sh\nexit 0\n")
        self.write_executable(self.bin / "python", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        self.write_executable(self.bin / "sleep", "#!/bin/sh\nexec /bin/sleep 0.025\n")
        date_code = """import os,sys,subprocess
from pathlib import Path
if sys.argv[1:]==['+%s']:
 p=Path(os.environ['FAKE_CLOCK']); value=int(p.read_text())+1 if p.exists() else 1000
 p.write_text(str(value)); print(value)
else: subprocess.run(['/bin/date']+sys.argv[1:],check=True)
"""
        self.write_executable(self.bin / "date", "#!" + sys.executable + "\n" + date_code)
        (self.worker / "start-phone-worker.sh").write_text(
            '#!/bin/bash\n'
            'printf "worker:start\\n" >> "$FAKE_EVENTS"\n'
            '/bin/sleep "${FAKE_START_SECONDS:-0.02}"\n'
            'printf "worker:end\\n" >> "$FAKE_EVENTS"\n'
            'exit 0\n')
        (self.worker / "phone_worker_bootstrap.py").write_text(
            "import os,time\n"
            "with open(os.environ['FAKE_EVENTS'],'a') as f: f.write('bootstrap:start\\n')\n"
            "time.sleep(float(os.environ.get('FAKE_BOOTSTRAP_SECONDS','0.02')))\n"
            "with open(os.environ['FAKE_EVENTS'],'a') as f: f.write('bootstrap:end\\n')\n")
        runtime = self.worker / "phone_worker_runtime"
        runtime.mkdir()
        (runtime / "ssh_transport.py").write_text(
            "import json,os,time\nfrom pathlib import Path\n"
            "p=Path(os.environ['FAKE_ENSURE_COUNT'])\n"
            "count=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(count))\n"
            "ready=count>int(os.environ.get('FAKE_NOT_READY_CALLS','0'))\n"
            "with open(os.environ['FAKE_EVENTS'],'a') as f: f.write('ensure:'+str(count)+':'+str(ready)+'\\n')\n"
            "time.sleep(float(os.environ.get('FAKE_ENSURE_SECONDS','0.02')))\n"
            "print(json.dumps({'mode':'ssh','vps_ready':ready,'state':'connected' if ready else 'connecting'}))\n")
        self.env_file = self.home / ".phone-worker.env"
        self.set_mode("ssh")
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + ":" + os.environ.get("PATH", ""),
            "PHONE_WORKER_ENV": str(self.env_file),
            "PHONE_WORKER_DIR": str(self.worker),
            "PHONE_WORKER_RUNTIME_ROOT": str(self.home / "runtime"),
            "PHONE_WORKER_PYTHON": sys.executable,
            "PHONE_WORKER_SSHD_AUTO_START": "false",
            "PHONE_WORKER_WATCH_INTERVAL_SECONDS": "60",
            "PHONE_WORKER_BOOTSTRAP_INTERVAL_SECONDS": "300",
            "PHONE_WORKER_BOOTSTRAP_JITTER_SECONDS": "0",
            "FAKE_EVENTS": str(self.events),
            "FAKE_CLOCK": str(self.home / "clock"),
            "FAKE_ENSURE_COUNT": str(self.home / "ensure-count"),
        }
        self.processes = []
        self.addCleanup(self.stop_all)

    @staticmethod
    def write_executable(path, content):
        path.write_text(content)
        path.chmod(0o700)

    def set_mode(self, mode):
        self.env_file.write_text("PHONE_WORKER_TRANSPORT=" + mode + "\n")

    def start(self, **extra):
        process = subprocess.Popen(["bash", str(self.watch)], env={**self.env, **extra},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        self.processes.append(process)
        return process

    def stop_all(self):
        for process in self.processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)

    def lines(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("watchdog condition was not reached; events=" + repr(self.lines()))

    def ensures(self):
        return [line for line in self.lines() if line.startswith("ensure:")]

    def test_shell_syntax(self):
        for name in ("watch-phone-worker.sh", "start-phone-worker.sh"):
            result = subprocess.run(["bash", "-n", str(PHONE / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_slow_bootstrap_does_not_block_transport_checks(self):
        self.start(FAKE_BOOTSTRAP_SECONDS="3")
        self.wait_for(lambda: "bootstrap:start" in self.lines())
        self.wait_for(lambda: len(self.ensures()) >= 3)
        self.assertNotIn("bootstrap:end", self.lines())
        self.assertEqual(self.lines().count("bootstrap:start"), 1)

    def test_slow_worker_check_does_not_block_transport_or_bootstrap(self):
        self.start(FAKE_START_SECONDS="3")
        self.wait_for(lambda: len(self.ensures()) >= 3 and "bootstrap:start" in self.lines())
        self.assertNotIn("worker:end", self.lines())
        self.assertEqual(self.lines().count("worker:start"), 1)

    def test_bootstrap_waits_for_actual_vps_ready_not_exit_code(self):
        self.start(FAKE_NOT_READY_CALLS="3")
        self.wait_for(lambda: "bootstrap:start" in self.lines())
        lines = self.lines()
        self.assertLess(lines.index("ensure:4:True"), lines.index("bootstrap:start"))
        self.assertNotIn("bootstrap:start", lines[:lines.index("ensure:4:True")])

    def test_missing_transport_does_not_prevent_local_worker_start(self):
        (self.worker / "phone_worker_runtime/ssh_transport.py").unlink()
        self.start()
        self.wait_for(lambda: "worker:end" in self.lines())
        time.sleep(0.15)
        self.assertNotIn("bootstrap:start", self.lines())

    def test_legacy_path_does_not_invoke_transport(self):
        self.set_mode("legacy")
        self.start()
        self.wait_for(lambda: "worker:end" in self.lines())
        self.assertIn("bootstrap:start", self.lines())
        self.assertFalse(self.ensures())

    def test_legacy_watch_notices_activation_from_env(self):
        self.set_mode("legacy")
        self.start()
        self.wait_for(lambda: "worker:end" in self.lines())
        self.set_mode("ssh")
        self.wait_for(lambda: bool(self.ensures()))

    def test_term_exits_watch_and_removes_only_its_lock(self):
        process = self.start(FAKE_BOOTSTRAP_SECONDS="3", FAKE_START_SECONDS="3")
        self.wait_for(lambda: "bootstrap:start" in self.lines())
        process.terminate()
        self.assertEqual(process.wait(timeout=2), 143)
        self.assertFalse((self.worker / ".phone-worker-watch.lock").exists())
        self.assertFalse((self.worker / "phone-worker-watch.pid").exists())

    def test_second_watch_does_not_remove_first_watch_lock(self):
        first = self.start()
        self.wait_for(lambda: bool(self.ensures()))
        second = self.start()
        self.assertEqual(second.wait(timeout=2), 0)
        self.assertIsNone(first.poll())
        owner = (self.worker / ".phone-worker-watch.lock/owner.pid").read_text().strip()
        self.assertEqual(owner, str(first.pid))


if __name__ == "__main__":
    unittest.main()
