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
