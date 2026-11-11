"""Exercise the real native launcher's readiness wait without host signals."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "deploy/termux/phone-worker/start-phone-worker.sh"


def function(name):
    source = LAUNCHER.read_text()
    return name + "() {" + source.split(name + "() {", 1)[1].split("\n}", 1)[0] + "\n}\n"


class NativeStartupTests(unittest.TestCase):
    def shell(self, body, *, native="1", wait="3", native_wait=None, functions=None):
        env = dict(os.environ, PYTHON_BIN=sys.executable, START_WAIT=wait, TTS_NATIVE_WORKER=native)
        env.pop("PHONE_WORKER_NATIVE_START_TIMEOUT_SECONDS", None)
        env.pop("CORE_WORKER_ID", None)
        env.pop("CORE_WORKER_WORKER_ID", None)
        if native_wait is not None:
            env["PHONE_WORKER_NATIVE_START_TIMEOUT_SECONDS"] = native_wait
        names = functions or ["startup_timeout_ms", "wait_for_worker_ready"]
        script = "set -u\n" + "".join(function(name) for name in names) + body
        result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def wait(self, extra, *, native="1", wait="3", native_wait=None):
        body = r'''
fake_ms=0
startup_clock_ms() { printf '%s\n' "$fake_ms"; }
sleep() { fake_ms=$((fake_ms + 1000)); }
kill() { [[ "$1" == "-0" && "$2" == "777" ]]; }
pid_is_official_worker() { [[ "$1" == "777" ]]; }
worker_exec_pending() { return 1; }
worker_healthy_for_pid() { (( fake_ms >= 4000 )); }
'''
        body += extra + r'''
if wait_for_worker_ready 777; then rc=0; else rc=$?; fi
printf '%s %s %s\n' "$rc" "$fake_ms" "$STARTUP_WAIT_FAILURE"
'''
        return self.shell(body, native=native, wait=wait, native_wait=native_wait)

    def test_native_worker_ready_after_legacy_three_seconds_is_accepted_early(self):
        # Device evidence: Rust was healthy after 4.06 s, beyond START_WAIT=3.
        self.assertEqual(self.wait(""), "0 4000")

    def test_python_timeout_keeps_the_legacy_budget(self):
        self.assertEqual(self.wait("", native="0"), "1 3000 verification_timeout")

    def test_unhealthy_native_worker_has_a_finite_fifteen_second_budget(self):
        self.assertEqual(self.wait("worker_healthy_for_pid() { return 1; }\n"),
                         "1 15000 verification_timeout")

    def test_longer_configured_waits_are_preserved(self):
        self.assertEqual(self.shell("startup_timeout_ms", wait="24"), "24000")
        self.assertEqual(self.shell("startup_timeout_ms", native_wait="27.5"), "27500")
        self.assertEqual(self.shell("startup_timeout_ms", native_wait="2"), "15000")

    def test_dead_child_stops_before_health_probe_or_sleep(self):
        self.assertEqual(self.wait(r'''
kill() { return 1; }
worker_healthy_for_pid() { printf 'unexpected health\n'; return 0; }
'''), "1 0 child_dead_or_identity_mismatch")

    def test_unrelated_process_cannot_pass_even_when_its_health_is_ready(self):
        self.assertEqual(self.wait(r'''
pid_is_official_worker() { return 1; }
worker_healthy_for_pid() { printf 'unexpected health\n'; return 0; }
'''), "1 0 child_dead_or_identity_mismatch")

    def test_health_returning_after_deadline_is_not_accepted(self):
        self.assertEqual(self.wait(r'''
worker_healthy_for_pid() { fake_ms=16000; return 0; }
'''), "1 16000 verification_timeout")

    def test_owned_shell_handoff_waits_for_python_without_probing_other_process(self):
        self.assertEqual(self.wait(r'''
pid_is_official_worker() { (( fake_ms >= 1000 )); }
worker_exec_pending() { (( fake_ms < 1000 )); }
worker_healthy_for_pid() {
  if (( fake_ms < 1000 )); then printf 'unexpected shell health\n'; fi
  (( fake_ms >= 4000 ))
}
'''), "0 4000")

    def test_owned_shell_that_never_execs_cannot_become_ready(self):
        self.assertEqual(self.wait(r'''
pid_is_official_worker() { return 1; }
worker_exec_pending() { return 0; }
worker_healthy_for_pid() { printf 'unexpected shell health\n'; return 0; }
'''), "1 15000 verification_timeout")

    def test_real_owned_shell_is_recognized_before_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            gate = Path(temporary) / "exec-gate"
            os.mkfifo(gate)
            body = "gate=" + __import__("shlex").quote(str(gate)) + r'''
( read -r ignored < "$gate" ) &
child=$!
if worker_exec_pending "$child"; then result=accepted; else result=rejected; fi
printf 'continue\n' > "$gate"
wait "$child"
printf '%s\n' "$result"
'''
            self.assertEqual(self.shell(body, functions=["worker_exec_pending"]), "accepted")

    def test_actual_readiness_checks_reject_stale_or_other_pid_status(self):
        names = ["startup_timeout_ms", "wait_for_worker_ready", "worker_healthy_for_pid",
                 "runtime_status_for_pid", "strict_health_for_pid", "runtime_http_port"]
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "runtime-status.json"
            for wrong_pid, stale in [(True, False), (False, True)]:
                with self.subTest(wrong_pid=wrong_pid, stale=stale):
                    value = {"pid": 888 if wrong_pid else 777, "updated_at": time.time() - (90 if stale else 0),
                             "status_updated_at": time.time() - (90 if stale else 0), "runtime_kind": "termux",
                             "runtime_mode": "termux", "source": "termux-phone-worker", "control_plane_alive": True,
                             "version": "1.11.52", "source_hash": "a" * 64, "http_port": 8766}
                    status.write_text(json.dumps(value))
                    body = r'''
fake_ms=0
PORT=8766
startup_clock_ms() { printf '%s\n' "$fake_ms"; }
sleep() { fake_ms=$((fake_ms + 1000)); }
kill() { [[ "$1" == "-0" && "$2" == "777" ]]; }
pid_is_official_worker() { [[ "$1" == "777" ]]; }
worker_exec_pending() { return 1; }
health_json_on_port() { cat "$RUNTIME_STATUS_JSON"; }
'''
                    # Quote the path; the JSON remains in the temporary file.
                    body += "RUNTIME_STATUS_JSON=" + shlex.quote(str(status)) + "\n"
                    body += r'''
if wait_for_worker_ready 777; then rc=0; else rc=$?; fi
printf '%s %s %s\n' "$rc" "$fake_ms" "$STARTUP_WAIT_FAILURE"
'''
                    self.assertEqual(self.shell(body, functions=names), "1 15000 verification_timeout")


if __name__ == "__main__":
    unittest.main()
