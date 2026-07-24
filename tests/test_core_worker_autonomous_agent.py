from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/core-worker-app/app/src/main/java/dev/core/worker"


def read(name: str) -> str:
    return (JAVA / name).read_text(encoding="utf-8")


def java_array(source: str, name: str) -> list[str]:
    match = re.search(rf"{re.escape(name)}\s*=\s*new String\[\]\s*\{{(.*?)\}};", source, re.S)
    assert match, f"array {name} not found"
    return re.findall(r'"([a-z0-9_]+)"', match.group(1))


def catalog_jobs() -> list[str]:
    return java_array(read("CoreWorkerJobCatalog.java"), "APK_JOBS")


def direct_tasks() -> list[str]:
    return java_array(read("CoreWorkerJobCatalog.java"), "DIRECT_REGISTRY_TASKS")


def test_activity_only_delegates_job_polling() -> None:
    source = read("MainActivity.java")
    assert "CoreWorkerRuntimeService.requestPoll(this" in source
    assert "/core-worker/app/jobs/fetch" not in source
    assert "/core-worker/app/jobs/result" not in source
    assert "/core-worker/jobs/poll" not in source
    assert "/core-worker/jobs/result" not in source
    assert "executeLightJob(" not in source
    assert "postLightJobResult(" not in source


def test_foreground_service_is_the_single_authenticated_queue_owner() -> None:
    source = read("CoreWorkerRuntimeService.java")
    assert source.count('/core-worker/jobs/poll') == 1
    assert source.count('/core-worker/jobs/result') == 1
    assert '/core-worker/app/jobs/fetch' not in source
    assert '/core-worker/app/jobs/result' not in source
    assert 'Authorization", "Bearer " + token.trim()' in source
    assert "Executors.newSingleThreadExecutor" in source
    assert "persistOutbox(jobId, envelope)" in source
    assert "flushResultOutbox(serverUrl)" in source
    assert 'body.optBoolean("ok", false)' in source
    assert "normalizeStoredEnvelope" in source


def test_internal_catalog_is_unique_and_fully_handled_headlessly() -> None:
    jobs = catalog_jobs()
    executor = read("CoreWorkerJobExecutor.java")
    assert len(jobs) == 44
    assert len(set(jobs)) == len(jobs)
    assert all(f'"{job}"' in executor for job in jobs)
    assert "CoreWorkerJobCatalog.supports(type)" in executor


def test_direct_catalog_is_unique_and_handled_by_direct_executor() -> None:
    tasks = direct_tasks()
    executor = read("CoreWorkerDirectTaskExecutor.java")
    assert len(tasks) >= 30
    assert len(set(tasks)) == len(tasks)
    assert all(f'"{task}"' in executor for task in tasks)
    assert "static boolean supports(String rawTask)" in executor


def test_background_entrypoints_wake_the_agent_without_activity() -> None:
    boot = read("CoreWorkerBootReceiver.java")
    fcm = read("CoreWorkerFirebaseMessagingService.java")
    scheduler = read("CoreWorkerUpdateJobService.java")
    assert "CoreWorkerRuntimeService.requestStart(context" in boot
    assert "CoreWorkerRuntimeService.requestPoll(this, wakeReason)" in fcm
    assert "CoreWorkerRuntimeService.requestPoll(this" in scheduler


def test_explicit_stop_is_persisted_and_respected() -> None:
    service = read("CoreWorkerRuntimeService.java")
    activity = read("MainActivity.java")
    assert '.putBoolean("agent_enabled", false)' in service
    assert 'prefs.contains("agent_enabled")' in service
    assert 'prefs.contains("agent_enabled") && !prefs.getBoolean("agent_enabled", false)' in activity


def test_manifest_declares_required_autonomous_runtime_permissions() -> None:
    manifest = (ROOT / "android/core-worker-app/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert "android.permission.RECEIVE_BOOT_COMPLETED" in manifest
    assert "android.permission.FOREGROUND_SERVICE_DATA_SYNC" in manifest
    assert "android.permission.WAKE_LOCK" in manifest
    assert 'android:name=".CoreWorkerRuntimeService"' in manifest
