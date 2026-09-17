"""Build orchestration for the APK self-builder facade.

The module owns no runtime state. Java-facing APIs, bindings and policy constants
remain in ``coreworker.apk_self_builder`` and are supplied per call.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any


def _bind_pre_gradle_owner(
    build_lock: Path,
    *,
    job_id: str,
    attempt: int,
    work: Path,
    log_path: Path,
    safe_json_load,
    atomic_json,
) -> None:
    """Publish private work ownership before any secret can be written there."""
    owner_path = build_lock / "owner.json"
    owner = safe_json_load(owner_path)
    if str(owner.get("jobId") or "") != str(job_id or ""):
        return
    try:
        owner_attempt = int(owner.get("attempt") or 0)
    except (TypeError, ValueError, OverflowError):
        return
    if owner_attempt not in {0, int(attempt)}:
        return
    owner.update({
        "work": str(work),
        "log": str(log_path),
        "gradlePid": int(owner.get("gradlePid") or 0),
        "gradlePgid": int(owner.get("gradlePgid") or 0),
        "gradleStartTicks": int(owner.get("gradleStartTicks") or 0),
        "pythonFinishedAt": 0,
        "stage": "source_preparing",
    })
    atomic_json(owner_path, owner)


def build(
    payload: dict[str, Any],
    files: Path,
    cache: Path,
    native: Path,
    server_url: str,
    worker_id: str,
    token: str,
    worker_version: str,
    *,
    public_preflight,
    validated_source_identifiers,
    resource_preflight,
    payload_cancellation_marker,
    acquire_build_lock,
    safe_filename,
    raise_if_build_cancelled,
    update_active_job_stage,
    download_source,
    safe_extract_zip,
    find_project,
    inject_private_files,
    hydrate_runtime_assets,
    run_gradle,
    classify_failure,
    validate_apk,
    persist_artifact,
    safe_json_load,
    atomic_json,
    cleanup_storage,
    publish_latest,
    transient_error_type,
    schema: str,
    wall_time=time.time,
    rmtree=shutil.rmtree,
) -> dict[str, Any]:
    del cache
    pre = json.loads(public_preflight(str(files), str(native), False))
    if not pre.get("ready"):
        return {
            "ok": False,
            "summary": pre.get("summary"),
            "error": pre.get("summary"),
            "preflight": pre,
            "retryable": True,
        }

    source_url = str(payload.get("source_zip_url") or payload.get("sourceZipUrl") or "").strip()
    if not source_url:
        raise ValueError("source_zip_url ausente")
    expected_sha, expected_bytes, source_fingerprint = validated_source_identifiers(payload)
    version_name = str(payload.get("versionName") or payload.get("version_name") or "").strip()
    version_code = int(payload.get("versionCode") or payload.get("version_code") or 0)
    notification_id = str(payload.get("notificationId") or f"apk-{version_code}-{source_fingerprint[:12]}").strip()

    early_resources = resource_preflight(files, None, payload, pre["toolchain"])
    if not early_resources.get("ok"):
        detail = "preflight_blocked: " + ", ".join(early_resources.get("blockers") or [])
        return {
            "ok": False,
            "summary": detail,
            "error": detail,
            "preflight": pre,
            "resource_preflight": early_resources,
            "failure_category": "transient",
            "retryable": True,
        }

    builder = files / "apk-self-builder"
    work_root = builder / "work"
    artifacts = builder / "artifacts"
    logs = builder / "logs"
    repro_assets = builder / "repro-assets"
    for path in (work_root, artifacts, logs):
        path.mkdir(parents=True, exist_ok=True)
    registry_job_id = str(payload.get("registryJobId") or payload.get("jobId") or notification_id).strip()
    registry_attempt = max(1, int(payload.get("registryAttempt") or 1))
    registry_cancellation = payload_cancellation_marker(builder, payload)
    if registry_cancellation is not None and registry_cancellation.is_file():
        raise transient_error_type("lease_ownership_lost: job cancelado durante o preflight")
    lock_ok, build_lock, lock_info = acquire_build_lock(builder, registry_job_id, registry_attempt)
    if not lock_ok:
        return {
            "ok": False,
            "summary": "preflight_blocked: builder_busy",
            "error": "builder_busy: outro build ainda possui o lock",
            "builder_lock": lock_info,
            "failure_category": "transient",
            "retryable": True,
        }
    job_slug = safe_filename(notification_id or f"build-{int(wall_time())}", "apk-build")
    work = work_root / (job_slug + "-" + hashlib.sha256(f"{wall_time()}".encode()).hexdigest()[:8])
    source_zip = work / "source.zip"
    source_root = work / "source"
    log_path = logs / (job_slug + "-gradle.log")
    started = wall_time()
    try:
        raise_if_build_cancelled(build_lock, registry_cancellation)
        work.mkdir(parents=True, exist_ok=False)
        _bind_pre_gradle_owner(
            build_lock,
            job_id=registry_job_id,
            attempt=registry_attempt,
            work=work,
            log_path=log_path,
            safe_json_load=safe_json_load,
            atomic_json=atomic_json,
        )
        update_active_job_stage(files, registry_job_id, "source_downloading", "baixando source autenticada")
        download = download_source(source_url, source_zip, expected_sha, expected_bytes, server_url)
        raise_if_build_cancelled(build_lock, registry_cancellation)
        update_active_job_stage(files, registry_job_id, "source_preparing", "validando e preparando source")
        extracted = safe_extract_zip(source_zip, source_root)
        project = find_project(source_root, str(payload.get("project_subdir") or "android/core-worker-app"))
        private = inject_private_files(project, payload)
        hydrated = hydrate_runtime_assets(project, native, repro_assets)
        resources = resource_preflight(files, project, payload, pre["toolchain"])
        if not resources.get("ok"):
            detail = "preflight_blocked: " + ", ".join(resources.get("blockers") or [])
            return {
                "ok": False,
                "summary": detail,
                "error": detail,
                "resource_preflight": resources,
                "builder_environment": {"preflight": pre, "hydrated": hydrated, "resources": resources},
                "failure_category": "transient",
                "retryable": True,
            }
        output_dir = project / "app/build/outputs/apk/debug"
        rmtree(output_dir, ignore_errors=True)
        update_active_job_stage(files, registry_job_id, "gradle_running", "compilando APK no toolchain privado")
        gradle = run_gradle(
            files, native, project, payload, work, log_path, resources,
            build_lock, registry_cancellation,
        )
        raise_if_build_cancelled(build_lock, registry_cancellation)
        if gradle["returncode"] != 0:
            detail = (gradle.get("logTail") or "") + "\nGradle retornou código " + str(gradle["returncode"])
            category = classify_failure(detail)
            return {
                "ok": False,
                "summary": "autobuild do APK falhou; consulte gradle_log_tail",
                "error": "Gradle retornou código " + str(gradle["returncode"]),
                "returncode": gradle["returncode"],
                "gradle_log_tail": gradle["logTail"],
                "duration_seconds": round(wall_time() - started, 3),
                "builder_environment": {"preflight": pre, "hydrated": hydrated, "resources": resources},
                "failure_category": "transient" if gradle.get("cancelled") else category,
                "retryable": bool(gradle.get("cancelled")) or category != "deterministic",
            }
        candidates = sorted(
            (project / "app/build/outputs/apk/debug").glob("*.apk"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise FileNotFoundError("Gradle terminou sem gerar app-debug.apk")
        built_apk = candidates[0]
        raise_if_build_cancelled(build_lock, registry_cancellation)
        validated = validate_apk(
            built_apk,
            expected_version_name=version_name,
            expected_version_code=version_code,
        )
        actual_version_name = str(validated["versionName"])
        actual_version_code = int(validated["versionCode"])
        filename = safe_filename(payload.get("filename"), f"CoreWorker-v{actual_version_name}-debug.apk")
        if not filename.lower().endswith(".apk"):
            filename += ".apk"
        artifact_path = persist_artifact(
            built_apk, artifacts, filename, notification_id, validated,
        )
        meta = {
            "schema": schema,
            "filename": artifact_path.name,
            "versionName": actual_version_name,
            "versionCode": actual_version_code,
            "sha256": validated["sha256"],
            "bytes": validated["bytes"],
            "artifact_path": str(artifact_path),
            "sourceFingerprint": source_fingerprint,
            "sourceSha256": download["sha256"],
            "notificationId": notification_id,
            "apkSigningMode": private["signingMode"],
            "apkSigningKeystoreSha256": private["signingKeystoreSha256"],
            "changelog": payload.get("changelog") or ["APK compilado pelo próprio Core Worker APK"],
            "created_at": wall_time(),
            "builderRuntime": "android-private-toolchain-direct",
            "workerVersion": worker_version,
        }
        atomic_json(artifact_path.with_suffix(artifact_path.suffix + ".json"), meta)
        atomic_json(artifacts / "latest-artifact.json", meta)
        raise_if_build_cancelled(build_lock, registry_cancellation)
        storage_cleanup = cleanup_storage(builder, artifact_path)
        result: dict[str, Any] = {
            "ok": True,
            "summary": f"APK {actual_version_name} compilado pelo próprio APK",
            "build_gradle_ok": True,
            "artifact_found": True,
            "apk": {"filename": artifact_path.name, "signed": True, **validated},
            "versionName": actual_version_name,
            "versionCode": actual_version_code,
            "artifact_meta": meta,
            "source": {**download, **extracted},
            "builder_environment": {"preflight": pre, "hydrated": hydrated},
            "storage_cleanup": storage_cleanup,
            "duration_seconds": round(wall_time() - started, 3),
        }
        if bool(payload.get("publish", True)):
            update_active_job_stage(files, registry_job_id, "publishing", "publicando APK validado na VPS")
            owner = safe_json_load(build_lock / "owner.json")
            owner.update({"stage": "publishing", "publishingAt": wall_time(), "artifact": str(artifact_path)})
            atomic_json(build_lock / "owner.json", owner)
            raise_if_build_cancelled(build_lock, registry_cancellation)
            published = publish_latest(files, payload, server_url, worker_id, token, worker_version)
            result["publish"] = published.get("publish", published)
            result["published"] = bool(published.get("ok"))
            if not published.get("ok"):
                result["ok"] = False
                result["summary"] = "APK compilado e persistido, mas a publicação falhou"
                result["error"] = str(
                    (published.get("publish") or {}).get("error")
                    if isinstance(published.get("publish"), dict)
                    else published.get("summary") or ""
                )[:500]
        return result
    finally:
        try:
            owner_path = build_lock / "owner.json"
            owner = safe_json_load(owner_path)
            try:
                owner_attempt = int(owner.get("attempt") or 0)
            except (TypeError, ValueError, OverflowError):
                owner_attempt = -1
            if str(owner.get("jobId") or "") == registry_job_id and owner_attempt in {0, registry_attempt}:
                owner.update({
                    "pythonFinishedAt": wall_time(),
                    "stage": "result_handoff_pending",
                })
                atomic_json(owner_path, owner)
        except Exception:
            pass
        cleanup_storage(builder)
