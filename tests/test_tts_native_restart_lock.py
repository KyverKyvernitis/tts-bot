"""Exercise the launcher's actual lock protocol with concurrent Bash owners."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest


LAUNCHER = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/start-phone-worker.sh"
SOURCE = LAUNCHER.read_text()
LOCK_FUNCTION = "startup_lock_operation() {" + SOURCE.split("startup_lock_operation() {", 1)[1].split("\n}", 1)[0] + "\n}"
ACQUIRE_BLOCK = SOURCE.split("\nif startup_lock_operation acquire; then", 1)[1].split("\ncleanup_start_lock()", 1)[0]


def shell(tmp_path: Path, body: str, *, force: bool = False, wait: str = "2") -> subprocess.Popen:
    env = dict(os.environ, PYTHON_BIN=sys.executable, LOCK_DIR=str(tmp_path / "start.lock"),
               FORCE_RESTART=str(int(force)), PHONE_WORKER_START_LOCK_WAIT_SECONDS=wait,
               TTS_NATIVE_WORKER="1")
    return subprocess.Popen(["bash", "-c", LOCK_FUNCTION + "\n" + body], env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_line(process: subprocess.Popen, expected: str) -> str:
    deadline = time.monotonic() + 5
    output = ""
    while time.monotonic() < deadline:
        if select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
            line = process.stdout.readline()
            output += line
            if expected in line:
                return output
            if not line:
                break
    raise AssertionError(f"Did not receive {expected!r}: {output!r}; exit={process.poll()}")


@pytest.fixture
def holder(tmp_path):
    process = shell(tmp_path, """
startup_lock_operation acquire || exit $?
trap 'startup_lock_operation release' EXIT
printf 'OWNER_READY\n'
while [[ ! -e "$LOCK_DIR.release" ]]; do sleep .02; done
""")
    try:
        wait_line(process, "OWNER_READY")
        yield process
    finally:
        (tmp_path / "start.lock.release").touch()
        process.communicate(timeout=5)


def test_watchdog_busy_is_success_and_preserves_live_owner(tmp_path, holder):
    owner = (tmp_path / "start.lock/owner.pid").read_text()
    process = shell(tmp_path, "\nif startup_lock_operation acquire; then" + ACQUIRE_BLOCK)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "start já em andamento; lock pertence a processo vivo" in stdout
    assert (tmp_path / "start.lock/owner.pid").read_text() == owner
    assert holder.poll() is None


def test_forced_restart_waits_then_runs_with_new_owner(tmp_path, holder):
    old_owner = (tmp_path / "start.lock/owner.pid").read_text()
    process = shell(tmp_path, """
startup_lock_operation acquire || exit $?
trap 'startup_lock_operation release' EXIT
printf 'RESTARTED owner=%s native=%s\n' "$BASHPID" "$TTS_NATIVE_WORKER"
""", force=True)
    try:
        wait_line(process, "aguardando lock de start")
        assert process.poll() is None
        assert (tmp_path / "start.lock/owner.pid").read_text() == old_owner
        (tmp_path / "start.lock.release").touch()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert f"RESTARTED owner={process.pid} native=1" in stdout
        assert not (tmp_path / "start.lock").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


def test_forced_busy_timeout_is_nonzero_and_never_removes_owner(tmp_path, holder):
    owner = (tmp_path / "start.lock/owner.pid").read_text()
    process = shell(tmp_path, "\nif startup_lock_operation acquire; then" + ACQUIRE_BLOCK,
                    force=True, wait=".15")
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 1, stderr
    assert "reinicio solicitado nao foi executado" in stdout
    assert (tmp_path / "start.lock/owner.pid").read_text() == owner
    assert holder.poll() is None


def test_dead_owner_is_recovered_atomically(tmp_path):
    # Use a reaped child PID, rather than guessing a PID outside Android's range.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    directory = tmp_path / "start.lock"
    directory.mkdir()
    (directory / "owner.pid").write_text(str(child.pid) + "\n")
    process = shell(tmp_path, """
startup_lock_operation acquire || exit $?
[[ "$(cat "$LOCK_DIR/owner.pid")" == "$BASHPID" ]] || exit 9
startup_lock_operation release
""", force=True)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "lock orfao confirmado; recuperando" in stdout
    assert not directory.exists()


def test_nonowner_cleanup_preserves_live_lock(tmp_path, holder):
    owner = (tmp_path / "start.lock/owner.pid").read_text()
    process = shell(tmp_path, "startup_lock_operation release")
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert (tmp_path / "start.lock/owner.pid").read_text() == owner


def test_guard_is_released_while_startup_owner_is_alive(tmp_path, holder):
    with (tmp_path / "start.lock.guard").open() as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(guard, fcntl.LOCK_UN)
    assert holder.poll() is None


def test_empty_fresh_legacy_lock_is_preserved(tmp_path):
    directory = tmp_path / "start.lock"
    directory.mkdir()
    process = shell(tmp_path, "startup_lock_operation acquire", force=True, wait=".1")
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 1, stderr
    assert directory.is_dir()
    assert not (directory / "owner.pid").exists()


def test_stale_empty_legacy_lock_is_recovered(tmp_path):
    directory = tmp_path / "start.lock"
    directory.mkdir()
    old = time.time() - 20
    os.utime(directory, (old, old))
    process = shell(tmp_path, "startup_lock_operation acquire && startup_lock_operation release")
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert not directory.exists()
