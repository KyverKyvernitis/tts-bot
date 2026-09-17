from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "android/core-worker-app/app/src/main/python"
SELF_BUILDER = PYTHON_ROOT / "coreworker/apk_self_builder.py"


def _load_self_builder(name: str):
    sys.path.insert(0, str(PYTHON_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(name, SELF_BUILDER)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(PYTHON_ROOT))


def test_fresh_ownerless_lock_is_not_stolen_during_owner_publish_window(tmp_path: Path) -> None:
    module = _load_self_builder("apk_process_fresh_ownerless")
    builder = tmp_path / "apk-self-builder"
    builder.mkdir()
    lock = builder / ".apk-build-active"
    lock.mkdir()

    acquired, returned_lock, detail = module._acquire_build_lock(builder, "job-new", 2)

    assert acquired is False
    assert returned_lock == lock
    assert lock.is_dir()
    assert detail.get("executorLive") is False
    assert not (lock / "owner.json").exists()


def test_stale_ownerless_lock_can_be_reclaimed_after_publish_grace(tmp_path: Path) -> None:
    module = _load_self_builder("apk_process_stale_ownerless")
    builder = tmp_path / "apk-self-builder"
    builder.mkdir()
    lock = builder / ".apk-build-active"
    lock.mkdir()
    stale = time.time() - 120
    os.utime(lock, (stale, stale))

    acquired, returned_lock, owner = module._acquire_build_lock(builder, "job-new", 2)

    assert acquired is True
    assert returned_lock == lock
    assert owner["jobId"] == "job-new"
    assert json.loads((lock / "owner.json").read_text(encoding="utf-8"))["attempt"] == 2


def test_failed_owner_publish_does_not_leave_an_ownerless_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder("apk_process_owner_publish_failure")
    builder = tmp_path / "apk-self-builder"
    builder.mkdir()

    def fail_publish(_path: Path, _value: dict[str, object]) -> None:
        raise OSError("fsync owner failed")

    monkeypatch.setattr(module, "_atomic_json", fail_publish)
    acquired, lock, detail = module._acquire_build_lock(builder, "job-fail", 1)

    assert acquired is False
    assert "fsync owner failed" in detail.get("error", "")
    assert not lock.exists()


@pytest.mark.parametrize("api", ["reconcile", "finalize"])
def test_corrupt_attempt_metadata_is_conservative_in_public_recovery_api(tmp_path: Path, api: str) -> None:
    module = _load_self_builder(f"apk_process_corrupt_attempt_{api}")
    builder = tmp_path / "apk-self-builder"
    lock = builder / ".apk-build-active"
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "jobId": "job-corrupt",
        "attempt": "not-an-int",
        "pid": 0,
        "gradlePid": 0,
        "gradlePgid": 0,
    })

    if api == "reconcile":
        result = json.loads(module.reconcile_interrupted_build(str(tmp_path), "job-corrupt", 1))
        assert result["ok"] is False
        assert result["safeToRequeue"] is False
        assert result["state"] == "owner_identity_unverified"
    else:
        result = json.loads(module.finalize_build_attempt(str(tmp_path), "job-corrupt", 1))
        assert result["ok"] is False
        assert result["released"] is False
        assert result["state"] == "owner_identity_unverified"
    assert lock.is_dir()


def test_corrupt_python_finished_marker_does_not_escape_reconcile(tmp_path: Path) -> None:
    module = _load_self_builder("apk_process_corrupt_python_finished")
    builder = tmp_path / "apk-self-builder"
    lock = builder / ".apk-build-active"
    work = builder / "work/job-corrupt-finished"
    project = work / "source/android/core-worker-app"
    project.mkdir(parents=True)
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "jobId": "job-corrupt-finished",
        "attempt": 1,
        "pid": os.getpid(),
        "processStartTicks": module._proc_start_ticks(os.getpid()),
        "pythonThreadNativeId": 999_999_999,
        "pythonFinishedAt": "not-a-float",
        "work": str(work),
        "project": str(project),
    })

    result = json.loads(module.reconcile_interrupted_build(str(tmp_path), "job-corrupt-finished", 1))

    assert result["ok"] is False
    assert result["safeToRequeue"] is False
    assert result["state"] == "owner_identity_unverified"
    assert lock.is_dir()


def test_finalize_rejects_work_root_itself_instead_of_deleting_all_attempts(tmp_path: Path) -> None:
    module = _load_self_builder("apk_process_work_root")
    builder = tmp_path / "apk-self-builder"
    work_root = builder / "work"
    other_attempt = work_root / "other-attempt"
    other_attempt.mkdir(parents=True)
    sentinel = other_attempt / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    lock = builder / ".apk-build-active"
    lock.mkdir(parents=True)
    module._atomic_json(lock / "owner.json", {
        "jobId": "job-root",
        "attempt": 1,
        "pid": 0,
        "gradlePid": 0,
        "gradlePgid": 0,
        "work": str(work_root),
        "project": str(work_root / "project"),
    })

    result = json.loads(module.finalize_build_attempt(str(tmp_path), "job-root", 1))

    assert result["ok"] is False
    assert result["released"] is False
    assert result["state"] == "work_identity_unverified"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert lock.is_dir()


def test_gradle_forces_java_tmpdir_to_private_work_directory(tmp_path: Path) -> None:
    process_path = PYTHON_ROOT / "coreworker/builder/process.py"
    spec = importlib.util.spec_from_file_location("apk_process_java_tmpdir", process_path)
    assert spec and spec.loader
    process_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(process_module)

    files = tmp_path / "files"
    project = tmp_path / "project"
    work = tmp_path / "work"
    log_path = tmp_path / "logs/build.log"
    build_lock = files / "apk-self-builder/.apk-build-active"
    build_lock.mkdir(parents=True)
    project.mkdir()
    (build_lock / "owner.json").write_text("{}", encoding="utf-8")

    captured: dict[str, object] = {}

    class Process:
        pid = 4321

        def poll(self):
            return 0

    def fake_popen(command, *, cwd, env, stdout, stderr, start_new_session):
        captured.update({
            "command": command,
            "cwd": cwd,
            "env": dict(env),
            "start_new_session": start_new_session,
        })
        return Process()

    def toolchain_environment(_tool, *, home, temp, gradle_home, clean):
        assert temp.is_dir()
        return {
            "TMPDIR": "/data/data/com.termux/files/usr/tmp",
            "JAVA_TOOL_OPTIONS": "-Djava.io.tmpdir=/data/data/com.termux/files/usr/tmp",
        }

    def safe_json_load(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def atomic_json(path: Path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    result = process_module.run_gradle(
        files,
        project,
        {"timeoutSeconds": 600},
        work,
        log_path,
        {"xmxMb": 256, "maxMetaspaceMb": 128},
        build_lock,
        None,
        public_preflight=lambda *_args: json.dumps({
            "ready": True,
            "toolchain": {
                "paths": {
                    "gradle": "/private/toolchain/gradle",
                    "aapt2": "/private/toolchain/aapt2",
                }
            },
        }),
        toolchain_environment=toolchain_environment,
        toolchain_fingerprint=lambda _tool: "fingerprint",
        safe_json_load=safe_json_load,
        atomic_json=atomic_json,
        proc_ticks=lambda _pid: 9876,
        stop_process=lambda _process: 0,
        tail=lambda path, _limit: path.read_text(encoding="utf-8"),
        schema="test",
        default_timeout_seconds=600,
        popen=fake_popen,
        stdout_target=-2,
        wall_time=lambda: 100.0,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )

    assert result["returncode"] == 0
    temp = work / "tmp"
    java_tmpdir = f"-Djava.io.tmpdir={temp}"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["TMPDIR"] == str(temp)
    assert java_tmpdir in env["JAVA_TOOL_OPTIONS"]
    assert java_tmpdir in env["GRADLE_OPTS"]
    assert "/data/data/com.termux/files/usr/tmp" not in env["JAVA_TOOL_OPTIONS"]

    gradle_props = files / "apk-self-builder/persistent/gradle-home/gradle.properties"
    props = gradle_props.read_text(encoding="utf-8")
    assert java_tmpdir in props
