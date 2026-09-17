from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android/core-worker-app"
PYTHON_ROOT = ANDROID / "app/src/main/python"
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


def _build_payload(**patch):
    payload = {
        "source_zip_url": "https://vps.example/source.zip",
        "source_sha256": "1" * 64,
        "source_bytes": 123,
        "sourceFingerprint": "2" * 64,
        "versionName": "0.8.7",
        "versionCode": 134,
        "notificationId": "notif-134",
        "registryJobId": "job-134",
        "registryAttempt": 1,
        "publish": False,
    }
    payload.update(patch)
    return payload


def _fake_successful_build(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    files = tmp_path / "files"
    native = tmp_path / "native"
    native.mkdir(parents=True)
    monkeypatch.setattr(module, "preflight", lambda *args, **kwargs: json.dumps({"ready": True, "toolchain": {}}))
    monkeypatch.setattr(module, "_resource_preflight", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(module, "_payload_cancellation_marker", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_raise_if_build_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_update_active_job_stage", lambda *args, **kwargs: True)

    def acquire(builder: Path, job_id: str, attempt: int):
        lock = builder / ".apk-build-active"
        lock.mkdir(parents=True, exist_ok=False)
        module._atomic_json(lock / "owner.json", {
            "jobId": job_id,
            "attempt": attempt,
            "pid": os.getpid(),
            "processStartTicks": 0,
            "pythonThreadNativeId": 0,
            "pythonFinishedAt": 0,
            "gradlePid": 0,
            "gradlePgid": 0,
            "gradleStartTicks": 0,
        })
        return True, lock, {}

    monkeypatch.setattr(module, "_acquire_build_lock", acquire)

    def download(url, target: Path, expected_sha, expected_bytes, server_url):
        target.write_bytes(b"source")
        return {"sha256": "1" * 64, "bytes": 123}

    monkeypatch.setattr(module, "_download_source", download)
    monkeypatch.setattr(module, "_safe_extract_zip", lambda source, target: {"entries": 1, "expandedBytes": 6})

    project_holder: dict[str, Path] = {}

    def find_project(source_root: Path, project_subdir: str) -> Path:
        project = source_root / "android/core-worker-app"
        project.mkdir(parents=True)
        project_holder["project"] = project
        return project

    monkeypatch.setattr(module, "_find_project", find_project)
    monkeypatch.setattr(module, "_inject_private_files", lambda *args: {
        "signingMode": "compat-vps-debug-keystore",
        "signingKeystoreSha256": "3" * 64,
    })
    monkeypatch.setattr(module, "_hydrate_runtime_assets", lambda *args: {})

    def run_gradle(files, native, project: Path, payload, work, log_path, resources, build_lock, registry_cancellation):
        apk = project / "app/build/outputs/apk/debug/app-debug.apk"
        apk.parent.mkdir(parents=True, exist_ok=True)
        apk.write_bytes(b"built-apk")
        return {"returncode": 0, "cancelled": False, "logTail": "", "durationSeconds": 1.0}

    monkeypatch.setattr(module, "_run_gradle", run_gradle)
    monkeypatch.setattr(module, "_validate_apk", lambda path, **kwargs: {
        "bytes": 2 * 1024 * 1024,
        "sha256": hashlib.sha256(b"built-apk").hexdigest(),
        "packageName": "dev.core.worker",
        "versionName": "0.8.7",
        "versionCode": 134,
    })
    monkeypatch.setattr(module, "_cleanup_private_builder_storage", lambda *args, **kwargs: {})
    return files


def test_pre_gradle_finished_owner_can_be_finalized_after_durable_handoff(tmp_path: Path) -> None:
    module = _load_self_builder("apk_builder_pre_gradle_finalize")
    files = tmp_path / "files"
    builder = files / "apk-self-builder"
    builder.mkdir(parents=True)
    ok, lock, _ = module._acquire_build_lock(builder, "job-pre-gradle", 1)
    assert ok
    work = builder / "work/attempt-one"
    work.mkdir(parents=True)
    owner = module._safe_json_load(lock / "owner.json")
    owner.update({"work": str(work), "pythonFinishedAt": 10.0, "stage": "result_handoff_pending"})
    module._atomic_json(lock / "owner.json", owner)

    result = json.loads(module.finalize_build_attempt(str(files), "job-pre-gradle", 1))

    assert result["ok"] is True
    assert result["released"] is True
    assert result["state"] == "build_resources_released"
    assert not lock.exists()
    assert not work.exists()



def test_pre_gradle_failure_records_work_for_finalize_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder("apk_builder_pre_gradle_work_cleanup")
    files = tmp_path / "files"
    native = tmp_path / "native"
    native.mkdir(parents=True)
    monkeypatch.setattr(module, "preflight", lambda *args, **kwargs: json.dumps({"ready": True, "toolchain": {}}))
    calls = {"resource": 0}

    def resource(*args, **kwargs):
        calls["resource"] += 1
        return {"ok": True} if calls["resource"] == 1 else {"ok": False, "blockers": ["battery_low"]}

    monkeypatch.setattr(module, "_resource_preflight", resource)
    monkeypatch.setattr(module, "_payload_cancellation_marker", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_raise_if_build_cancelled", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_update_active_job_stage", lambda *args, **kwargs: True)

    def download(url, target: Path, expected_sha, expected_bytes, server_url):
        target.write_bytes(b"source")
        return {"sha256": "1" * 64, "bytes": 123}

    monkeypatch.setattr(module, "_download_source", download)
    monkeypatch.setattr(module, "_safe_extract_zip", lambda source, target: {"entries": 1})

    def find_project(source_root: Path, project_subdir: str) -> Path:
        project = source_root / "android/core-worker-app"
        project.mkdir(parents=True)
        return project

    monkeypatch.setattr(module, "_find_project", find_project)

    def inject(project: Path, payload):
        secret = project / "app/core-worker-signing.properties"
        secret.parent.mkdir(parents=True)
        secret.write_text("secret", encoding="utf-8")
        return {"signingMode": "compat", "signingKeystoreSha256": "3" * 64}

    monkeypatch.setattr(module, "_inject_private_files", inject)
    monkeypatch.setattr(module, "_hydrate_runtime_assets", lambda *args: {})
    monkeypatch.setattr(module, "_cleanup_private_builder_storage", lambda *args, **kwargs: {})

    result = module._build(
        _build_payload(), files, tmp_path / "cache", native,
        "https://vps.example", "worker", "token", "1.0",
    )
    assert result["ok"] is False and "battery_low" in result["error"]
    builder = files / "apk-self-builder"
    workdirs = list((builder / "work").iterdir())
    assert len(workdirs) == 1
    assert (workdirs[0] / "source/android/core-worker-app/app/core-worker-signing.properties").is_file()

    finalized = json.loads(module.finalize_build_attempt(str(files), "job-134", 1))
    assert finalized["released"] is True
    assert list((builder / "work").iterdir()) == []

def test_publish_latest_requires_persisted_artifact_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder("apk_builder_publish_requires_hash")
    files = tmp_path / "files"
    artifacts = files / "apk-self-builder/artifacts"
    artifacts.mkdir(parents=True)
    apk = artifacts / "CoreWorker-v0.8.7-debug.apk"
    apk.write_bytes(b"apk")
    module._atomic_json(artifacts / "latest-artifact.json", {
        "artifact_path": str(apk),
        "versionName": "0.8.7",
        "versionCode": 134,
    })
    monkeypatch.setattr(module, "_validate_apk", lambda path, **kwargs: {
        "bytes": 2 * 1024 * 1024,
        "sha256": "a" * 64,
        "packageName": "dev.core.worker",
        "versionName": "0.8.7",
        "versionCode": 134,
    })
    monkeypatch.setattr(module, "_multipart_publish", lambda *args, **kwargs: {"ok": True})

    with pytest.raises(ValueError, match="sha256"):
        module._publish_latest(files, {}, "https://vps.example", "worker", "token", "1.0")


def test_multipart_publish_closes_connection_when_send_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder("apk_builder_publish_connection_cleanup")
    apk = tmp_path / "artifact.apk"
    apk.write_bytes(b"apk-bytes")

    class Connection:
        def __init__(self):
            self.closed = False
        def putrequest(self, *args): pass
        def putheader(self, *args): pass
        def endheaders(self): pass
        def send(self, data): raise OSError("socket failed")
        def close(self): self.closed = True

    connection = Connection()
    monkeypatch.setattr(module.http.client, "HTTPSConnection", lambda *args, **kwargs: connection)

    with pytest.raises(OSError, match="socket failed"):
        module._multipart_publish(
            apk,
            {"filename": "CoreWorker-debug.apk"},
            "https://vps.example/core-worker/app/publish",
            "token", "worker", "1.0",
        )
    assert connection.closed is True


def test_failed_artifact_copy_never_leaves_partial_final_apk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder("apk_builder_atomic_artifact")
    files = _fake_successful_build(module, tmp_path, monkeypatch)

    def broken_copy(source, target, *args, **kwargs):
        Path(target).write_bytes(b"partial")
        raise OSError("copy interrupted")

    monkeypatch.setattr(module.shutil, "copy2", broken_copy)

    with pytest.raises(OSError, match="copy interrupted"):
        module._build(_build_payload(), files, tmp_path / "cache", tmp_path / "native", "https://vps.example", "worker", "token", "1.0")

    artifacts = files / "apk-self-builder/artifacts"
    assert list(artifacts.glob("*.apk")) == []
    assert list(artifacts.glob("*.tmp*")) == []


def test_collision_suffix_from_notification_cannot_escape_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_self_builder("apk_builder_collision_path")
    files = _fake_successful_build(module, tmp_path, monkeypatch)
    artifacts = files / "apk-self-builder/artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    original = artifacts / "CoreWorker-v0.8.7-debug.apk"
    original.write_bytes(b"different-existing-artifact")

    result = module._build(
        _build_payload(notificationId="x/../../../../outside"),
        files, tmp_path / "cache", tmp_path / "native", "https://vps.example", "worker", "token", "1.0",
    )

    artifact_path = Path(result["artifact_meta"]["artifact_path"])
    assert artifact_path.is_file()
    assert artifact_path.resolve().is_relative_to(artifacts.resolve())
    assert artifact_path.name.endswith(".apk")
    assert original.read_bytes() == b"different-existing-artifact"
