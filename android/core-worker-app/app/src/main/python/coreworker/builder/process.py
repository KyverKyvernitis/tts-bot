"""Gradle process ownership, cancellation and durable-result recovery.

The module owns no runtime state. Filesystem/clock/process bindings which the
facade needs to patch in tests are supplied per call.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

OWNER_PUBLISH_GRACE_SECONDS = 30.0


def _int_field(value: Any) -> int | None:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _float_field(value: Any) -> float | None:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def proc_start_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
        rest = raw[raw.rfind(")") + 2 :].split()
        return int(rest[19])
    except Exception:
        return 0


def process_group_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def live_process_group_members(pgid: int) -> list[int]:
    """List non-zombie PGID members without trusting only the leader PID."""
    if pgid <= 0:
        return []
    members: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            rest = raw[raw.rfind(")") + 2 :].split()
            if len(rest) > 2 and rest[0] != "Z" and int(rest[2]) == pgid:
                members.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
            continue
    return members


def validated_orphaned_gradle_group(pgid: int, project: Path, work: Path, *, is_inside) -> bool:
    """Reject a reused PGID after the original Gradle shell leader exited."""
    members = live_process_group_members(pgid)
    if not members:
        return False
    identity_seen = False
    inspected = 0
    for member in members:
        try:
            cmdline = Path(f"/proc/{member}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).lower()
            cwd = Path(f"/proc/{member}/cwd").resolve()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (PermissionError, OSError):
            return False
        inspected += 1
        if not (is_inside(cwd, project) and is_inside(project, work)):
            return False
        if any(token in cmdline for token in (
            "gradle", "java", "aapt2", "kotlinc", "d8", "r8", "/system/bin/sh",
        )):
            identity_seen = True
    return inspected > 0 and identity_seen


def signal_owned_process(process: subprocess.Popen[Any], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(sig)


def stop_owned_process(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = 10.0,
    group_alive=process_group_alive,
    signal_process=signal_owned_process,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> int:
    deadline = monotonic() + max(0.1, float(grace_seconds))
    if process.poll() is None or group_alive(process.pid):
        signal_process(process, signal.SIGTERM)
    while monotonic() < deadline:
        leader_alive = process.poll() is None
        pg_alive = group_alive(process.pid)
        if not leader_alive and not pg_alive:
            break
        sleep(0.1)
    if process.poll() is None or group_alive(process.pid):
        signal_process(process, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    return int(process.poll() if process.poll() is not None else -signal.SIGKILL)


def payload_cancellation_marker(builder: Path, payload: dict[str, Any], *, is_inside) -> Path | None:
    raw = str(payload.get("registryCancellationPath") or "").strip()
    if not raw:
        return None
    marker = Path(raw)
    root = builder / "cancellations"
    if not is_inside(marker, root) or marker.suffix != ".request":
        raise ValueError("registryCancellationPath inválido")
    return marker


def acquire_build_lock(
    builder: Path,
    job_id: str,
    attempt: int,
    *,
    atomic_json,
    safe_json_load,
    proc_ticks,
    group_alive,
    short,
    schema: str,
    wall_time=time.time,
    rmtree=shutil.rmtree,
    owner_publish_grace_seconds: float = OWNER_PUBLISH_GRACE_SECONDS,
) -> tuple[bool, Path, dict[str, Any]]:
    lock = builder / ".apk-build-active"
    owner = lock / "owner.json"
    for _attempt in range(2):
        created_here = False
        try:
            lock.mkdir(parents=False, exist_ok=False)
            created_here = True
            record = {
                "pid": os.getpid(),
                "processStartTicks": proc_ticks(os.getpid()),
                "pythonThreadNativeId": threading.get_native_id(),
                "jobId": str(job_id or ""),
                "attempt": max(1, int(attempt or 1)),
                "startedAt": wall_time(),
                "schema": schema,
            }
            atomic_json(owner, record)
            return True, lock, {**record, "path": str(lock)}
        except FileExistsError:
            owner_exists = owner.is_file()
            data = safe_json_load(owner)
            if not data:
                if owner_exists:
                    return False, lock, {
                        "executorLive": False,
                        "state": "owner_identity_unverified",
                        "path": str(lock),
                    }
                try:
                    age = max(0.0, wall_time() - lock.stat().st_mtime)
                except OSError as exc:
                    return False, lock, {"error": f"{type(exc).__name__}: {short(exc, 300)}", "path": str(lock)}
                if age < max(1.0, float(owner_publish_grace_seconds)):
                    return False, lock, {
                        "executorLive": False,
                        "state": "owner_publish_pending",
                        "lockAgeSeconds": round(age, 3),
                        "path": str(lock),
                    }
                rmtree(lock, ignore_errors=True)
                continue

            pid = _int_field(data.get("pid"))
            started = _float_field(data.get("startedAt"))
            expected_ticks = _int_field(data.get("processStartTicks"))
            gradle_pid = _int_field(data.get("gradlePid"))
            gradle_ticks = _int_field(data.get("gradleStartTicks"))
            gradle_pgid = _int_field(data.get("gradlePgid"))
            if None in {pid, started, expected_ticks, gradle_pid, gradle_ticks, gradle_pgid}:
                return False, lock, {
                    "executorLive": False,
                    "state": "owner_identity_unverified",
                    "path": str(lock),
                }
            alive = pid > 0 and Path(f"/proc/{pid}").exists() and (
                expected_ticks <= 0 or proc_ticks(pid) == expected_ticks
            )
            gradle_alive = gradle_pid > 0 and Path(f"/proc/{gradle_pid}").exists() and (
                gradle_ticks <= 0 or proc_ticks(gradle_pid) == gradle_ticks
            )
            pg_alive = gradle_pgid > 0 and group_alive(gradle_pgid)
            fresh = started > 0 and wall_time() - started < 4 * 60 * 60
            if (alive and fresh) or gradle_alive or pg_alive:
                return False, lock, {
                    "pid": pid,
                    "startedAt": started,
                    "gradlePid": gradle_pid,
                    "gradlePgid": gradle_pgid,
                    "executorLive": bool(gradle_alive or pg_alive),
                    "path": str(lock),
                }
            rmtree(lock, ignore_errors=True)
        except Exception as exc:
            if created_here:
                rmtree(lock, ignore_errors=True)
            return False, lock, {"error": f"{type(exc).__name__}: {short(exc, 300)}", "path": str(lock)}
    return False, lock, {"error": "lock ativo", "path": str(lock)}


def owner_python_execution_alive(owner: dict[str, Any], *, proc_ticks) -> bool:
    """Confirm exact Python thread identity; malformed ownership is never live."""
    finished = _float_field(owner.get("pythonFinishedAt"))
    pid = _int_field(owner.get("pid"))
    expected_ticks = _int_field(owner.get("processStartTicks"))
    tid = _int_field(owner.get("pythonThreadNativeId"))
    if None in {finished, pid, expected_ticks, tid}:
        return False
    if finished > 0.0:
        return False
    if pid <= 0 or not Path(f"/proc/{pid}").exists():
        return False
    if expected_ticks > 0 and proc_ticks(pid) != expected_ticks:
        return False
    if tid <= 0:
        return True
    return Path(f"/proc/{pid}/task/{tid}").exists()


def raise_if_build_cancelled(lock: Path, registry_marker: Path | None, *, error_type) -> None:
    if (lock / "cancel.request").is_file() or (registry_marker is not None and registry_marker.is_file()):
        raise error_type("lease_ownership_lost: build cancelado antes de publicar")


def safe_gradle_owner(
    lock: Path,
    job_id: str,
    attempt: int = 0,
    *,
    safe_json_load,
    proc_ticks,
    group_alive,
    validate_orphaned_group,
    is_inside,
) -> tuple[dict[str, Any], int, bool]:
    owner = safe_json_load(lock / "owner.json")
    if not owner or str(owner.get("jobId") or "") != str(job_id or ""):
        return owner, 0, False
    owner_attempt = _int_field(owner.get("attempt"))
    if owner_attempt is None:
        return owner, 0, False
    if attempt > 0 and owner_attempt not in {0, int(attempt)}:
        return owner, 0, False
    pid = _int_field(owner.get("gradlePid"))
    pgid = _int_field(owner.get("gradlePgid"))
    if pid is None or pgid is None:
        return owner, 0, False
    pid_alive = pid > 0 and Path(f"/proc/{pid}").exists()
    pg_alive = pgid > 0 and group_alive(pgid)
    if not pid_alive and not pg_alive:
        return owner, pid, True
    try:
        project = Path(str(owner.get("project") or "")).resolve()
        work = Path(str(owner.get("work") or "")).resolve()
        builder = lock.parent.resolve()
    except Exception:
        return owner, pid, False
    if not owner.get("project") or not owner.get("work"):
        return owner, pid, False
    if not (is_inside(project, work) and is_inside(work, builder)):
        return owner, pid, False
    if not pid_alive:
        return owner, pid, bool(pg_alive and validate_orphaned_group(pgid, project, work))
    expected_ticks = _int_field(owner.get("gradleStartTicks"))
    if expected_ticks is None or expected_ticks <= 0 or proc_ticks(pid) != expected_ticks:
        return owner, pid, False
    try:
        if pgid > 0 and os.getpgid(pid) != pgid:
            return owner, pid, False
    except OSError:
        return owner, pid, False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").lower()
        cwd = Path(f"/proc/{pid}/cwd").resolve()
    except Exception:
        return owner, pid, False
    identity_ok = "gradle" in cmdline or "java" in cmdline or "/system/bin/sh" in cmdline
    paths_ok = is_inside(cwd, project) and is_inside(project, work)
    return owner, pid, bool(identity_ok and paths_ok)


def reconcile_interrupted_build(
    files_dir: str,
    job_id: str,
    attempt: int = 0,
    *,
    safe_owner,
    atomic_json,
    safe_json_load,
    process_group_alive_fn,
    proc_ticks,
    owner_python_alive,
    result_outbox_path,
    finalize_attempt,
    wall_time=time.time,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> str:
    """Cancel only the Gradle executor proven to belong to the active job."""
    builder = Path(files_dir) / "apk-self-builder"
    lock = builder / ".apk-build-active"
    try:
        numeric_attempt = int(attempt or 0)
    except (TypeError, ValueError, OverflowError):
        numeric_attempt = 0
    owner, pid, safe = safe_owner(lock, str(job_id or ""), numeric_attempt)
    result: dict[str, Any] = {
        "ok": safe,
        "jobId": str(job_id or ""),
        "attempt": numeric_attempt,
        "lockPresent": lock.is_dir(),
        "gradlePid": pid,
        "identityValidated": safe,
        "safeToRequeue": False,
    }
    if not lock.is_dir():
        result.update({"ok": True, "safeToRequeue": True, "state": "no_build_lock"})
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if not safe:
        result["state"] = "owner_identity_unverified"
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    cancel = lock / "cancel.request"
    atomic_json(cancel, {"jobId": str(job_id or ""), "requestedAt": wall_time(), "reason": "service_restart"})
    pgid = _int_field(owner.get("gradlePgid"))
    if pgid is None:
        result.update({"ok": False, "identityValidated": False, "state": "owner_identity_unverified"})
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if pgid > 0 and process_group_alive_fn(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGTERM)
    elif pid > 0 and Path(f"/proc/{pid}").exists():
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)
    if (pid > 0 and Path(f"/proc/{pid}").exists()) or process_group_alive_fn(pgid):
        deadline = monotonic() + 10.0
        while (Path(f"/proc/{pid}").exists() or process_group_alive_fn(pgid)) and monotonic() < deadline:
            sleep(0.25)
        if Path(f"/proc/{pid}").exists() or process_group_alive_fn(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL) if pgid > 0 else os.kill(pid, signal.SIGKILL)
    stopped = (pid <= 0 or not Path(f"/proc/{pid}").exists()) and not process_group_alive_fn(pgid)
    owner_pid = _int_field(owner.get("pid"))
    owner_ticks = _int_field(owner.get("processStartTicks"))
    owner_finished = _float_field(owner.get("pythonFinishedAt"))
    owner_tid = _int_field(owner.get("pythonThreadNativeId"))
    if None in {owner_pid, owner_ticks, owner_finished, owner_tid}:
        result.update({"ok": False, "identityValidated": False, "state": "owner_identity_unverified"})
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    owner_alive = owner_pid > 0 and Path(f"/proc/{owner_pid}").exists() and (
        owner_ticks <= 0 or proc_ticks(owner_pid) == owner_ticks
    )
    if stopped and owner_alive:
        outbox = result_outbox_path(files_dir, job_id)
        deadline = monotonic() + 10.0
        while (
            lock.is_dir()
            and not outbox.is_file()
            and owner_python_alive(safe_json_load(lock / "owner.json"))
            and monotonic() < deadline
        ):
            sleep(0.25)
        current_owner = safe_json_load(lock / "owner.json") if lock.is_dir() else {}
        stopped = not outbox.is_file() and (
            not lock.is_dir() or not owner_python_alive(current_owner)
        )
    resource_recovery: dict[str, Any] = {}
    if stopped:
        resource_recovery = json.loads(finalize_attempt(files_dir, job_id, numeric_attempt))
        stopped = bool(resource_recovery.get("ok") and resource_recovery.get("released"))
    result.update({
        "ok": stopped,
        "safeToRequeue": stopped,
        "state": "executor_stopped" if stopped else "executor_stop_pending",
        "resourceRecovery": resource_recovery,
    })
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def finalize_build_attempt(
    files_dir: str,
    job_id: str,
    attempt: int = 0,
    *,
    safe_json_load,
    safe_owner,
    process_group_alive_fn,
    is_inside,
    cleanup_storage,
    rmtree=shutil.rmtree,
) -> str:
    """Release work/lock only after Java made the result durable."""
    try:
        numeric_attempt = int(attempt or 0)
    except (TypeError, ValueError, OverflowError):
        numeric_attempt = 0
    builder = Path(files_dir) / "apk-self-builder"
    lock = builder / ".apk-build-active"
    if not lock.is_dir():
        return json.dumps({
            "ok": True, "released": True, "state": "no_build_lock",
            "jobId": str(job_id or ""), "attempt": numeric_attempt,
        }, ensure_ascii=False, separators=(",", ":"))
    raw_owner = safe_json_load(lock / "owner.json")
    owner_job = str(raw_owner.get("jobId") or "")
    owner_attempt = _int_field(raw_owner.get("attempt"))
    if owner_attempt is None:
        return json.dumps({
            "ok": False, "released": False, "state": "owner_identity_unverified",
            "jobId": str(job_id or ""), "attempt": numeric_attempt,
        }, ensure_ascii=False, separators=(",", ":"))
    if owner_job and (
        owner_job != str(job_id or "")
        or (numeric_attempt > 0 and owner_attempt not in {0, numeric_attempt})
    ):
        return json.dumps({
            "ok": True,
            "released": True,
            "state": "foreign_build_lock_preserved",
            "jobId": str(job_id or ""),
            "attempt": numeric_attempt,
            "ownerJobId": owner_job,
            "ownerAttempt": owner_attempt,
        }, ensure_ascii=False, separators=(",", ":"))
    owner, pid, safe = safe_owner(lock, str(job_id or ""), numeric_attempt)
    pgid = _int_field(owner.get("gradlePgid"))
    if pgid is None:
        pgid = 0
        safe = False
    executor_alive = (pid > 0 and Path(f"/proc/{pid}").exists()) or process_group_alive_fn(pgid)
    if not safe or executor_alive:
        return json.dumps({
            "ok": False,
            "released": False,
            "state": "executor_still_owned" if executor_alive else "owner_identity_unverified",
            "jobId": str(job_id or ""),
            "attempt": numeric_attempt,
            "gradlePid": pid,
            "gradlePgid": pgid,
        }, ensure_ascii=False, separators=(",", ":"))
    work_raw = str(owner.get("work") or "").strip()
    if work_raw:
        work = Path(work_raw)
        work_root = builder / "work"
        try:
            same_as_root = work.resolve() == work_root.resolve()
        except OSError:
            same_as_root = True
        if same_as_root or not is_inside(work, work_root):
            return json.dumps({
                "ok": False, "released": False, "state": "work_identity_unverified",
                "jobId": str(job_id or ""), "attempt": numeric_attempt,
            }, ensure_ascii=False, separators=(",", ":"))
        rmtree(work, ignore_errors=True)
    rmtree(lock, ignore_errors=True)
    released = not lock.exists()
    cleanup = cleanup_storage(builder) if released else {}
    return json.dumps({
        "ok": released,
        "released": released,
        "state": "build_resources_released" if released else "build_lock_release_failed",
        "jobId": str(job_id or ""),
        "attempt": numeric_attempt,
        "cleanup": cleanup,
    }, ensure_ascii=False, separators=(",", ":"))


def run_gradle(
    files: Path,
    project: Path,
    payload: dict[str, Any],
    work: Path,
    log_path: Path,
    resources: dict[str, Any],
    build_lock: Path,
    registry_cancellation: Path | None,
    *,
    public_preflight,
    toolchain_environment,
    toolchain_fingerprint,
    safe_json_load,
    atomic_json,
    proc_ticks,
    stop_process,
    tail,
    schema: str,
    default_timeout_seconds: int,
    popen=subprocess.Popen,
    stdout_target=subprocess.STDOUT,
    wall_time=time.time,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> dict[str, Any]:
    pre = json.loads(public_preflight(str(files), "", False))
    if not pre.get("ready"):
        raise RuntimeError(pre.get("summary") or "autobuilder não está pronto")
    tool = pre["toolchain"]
    paths = tool["paths"]
    builder = files / "apk-self-builder"
    persistent = builder / "persistent"
    gradle_home = persistent / "gradle-home"
    home = persistent / "home"
    temp = work / "tmp"
    for path in (gradle_home, home, temp):
        path.mkdir(parents=True, exist_ok=True)

    xmx_mb = int(resources.get("xmxMb") or 512)
    metaspace_mb = int(resources.get("maxMetaspaceMb") or 256)
    gradle_props = gradle_home / "gradle.properties"
    gradle_props.write_text("\n".join((
        f"android.aapt2FromMavenOverride={paths['aapt2']}",
        "org.gradle.daemon=false",
        "org.gradle.workers.max=1",
        "org.gradle.parallel=false",
        "org.gradle.vfs.watch=false",
        f"org.gradle.jvmargs=-Xmx{xmx_mb}m -Xms64m -XX:MaxMetaspaceSize={metaspace_mb}m -Dfile.encoding=UTF-8 -Djdk.lang.Process.launchMechanism=FORK",
        "",
    )), encoding="utf-8")

    env = toolchain_environment(tool, home=home, temp=temp, gradle_home=gradle_home, clean=False)
    vps_url = str(payload.get("coreWorkerVpsUrl") or payload.get("core_worker_vps_url") or "").strip()
    vps_label = str(payload.get("coreWorkerVpsLabel") or payload.get("core_worker_vps_label") or "VPS privada").strip()
    env.update({
        "CORE_WORKER_VPS_URL": vps_url,
        "CORE_WORKER_VPS_LABEL": vps_label,
        "CORE_WORKER_REQUIRE_COMPAT_SIGNING": "true",
        "CORE_WORKER_REQUIRE_SELF_BUILDER_TOOLCHAIN": "true",
        "GRADLE_OPTS": f"-Xmx{xmx_mb}m -Xms64m -XX:MaxMetaspaceSize={metaspace_mb}m -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false -Dorg.gradle.vfs.watch=false -Djdk.lang.Process.launchMechanism=FORK",
        "JAVA_TOOL_OPTIONS": "-Djdk.lang.Process.launchMechanism=FORK",
    })
    parent_worker_id = str(payload.get("physicalWorkerId") or payload.get("parentWorkerId") or payload.get("selectedBuilderWorkerId") or "").strip()
    source_fingerprint = str(payload.get("sourceFingerprint") or payload.get("source_sha256") or "").strip()
    command = [
        "/system/bin/sh", paths["gradle"], "assembleDebug",
        "--no-daemon", "--max-workers=1", "--stacktrace", "--console=plain",
        f"-PCORE_WORKER_PARENT_WORKER_ID={parent_worker_id}",
        f"-PCORE_WORKER_SOURCE_FINGERPRINT={source_fingerprint}",
    ]

    timeout = int(payload.get("timeout_seconds") or payload.get("timeoutSeconds") or default_timeout_seconds)
    timeout = max(600, min(4 * 60 * 60, timeout))
    started = wall_time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("===== Core Worker APK self-build =====\n")
        log.write(f"schema={schema}\nstarted_at={int(started)}\nproject={project}\n")
        log.write("runtime=android-private-toolchain-direct\n")
        log.write(f"worker_version={payload.get('requiredAgentVersion') or ''}\n")
        log.write(f"agent_source_hash={payload.get('requiredAgentSourceHash') or ''}\n")
        log.write(f"source_fingerprint={payload.get('sourceFingerprint') or payload.get('source_sha256') or ''}\n")
        log.write(f"apk_target={payload.get('versionName') or ''} code={payload.get('versionCode') or 0}\n")
        log.write("jdk=17 gradle=8.9 agp=8.7.3 compileSdk=34 buildTools=34.0.0 chaquopy=17.0.0\n")
        log.write(f"aapt2={paths['aapt2']}\n")
        log.write(f"xmx_mb={xmx_mb} memory_available_bytes={resources.get('memoryAvailableBytes', 0)} storage_free_bytes={resources.get('storageFreeBytes', 0)}\n")
        log.write(f"toolchain_fingerprint={payload.get('toolchainFingerprint') or toolchain_fingerprint(tool)}\n")
        log.write(f"toolchain_bytes={resources.get('toolchainBytes', 0)} project_bytes={resources.get('projectTreeBytes', 0)}\n")
        log.write("===== Gradle output =====\n")
        log.flush()
        process = popen(
            command,
            cwd=str(project),
            env=env,
            stdout=log,
            stderr=stdout_target,
            start_new_session=True,
        )
        try:
            owner_path = build_lock / "owner.json"
            owner = safe_json_load(owner_path)
            owner.update({
                "gradlePid": process.pid,
                "gradlePgid": process.pid,
                "gradleStartTicks": proc_ticks(process.pid),
                "stage": "gradle_running",
                "project": str(project),
                "work": str(work),
                "log": str(log_path),
                "gradleStartedAt": wall_time(),
            })
            atomic_json(owner_path, owner)
            deadline = monotonic() + timeout
            return_code: int | None = None
            cancelled = False
            while return_code is None:
                return_code = process.poll()
                if return_code is not None:
                    break
                if (build_lock / "cancel.request").is_file() or (
                    registry_cancellation is not None and registry_cancellation.is_file()
                ):
                    cancelled = True
                    return_code = stop_process(process)
                    log.write("\n===== CANCELLED: lease/ownership perdido =====\n")
                    break
                if monotonic() >= deadline:
                    stop_process(process)
                    return_code = 124
                    log.write(f"\n===== TIMEOUT {timeout}s =====\n")
                    break
                sleep(1.0)
            owner = safe_json_load(owner_path)
            owner.update({"gradleExitedAt": wall_time(), "gradleReturnCode": int(return_code or 0)})
            atomic_json(owner_path, owner)
        finally:
            stop_process(process)
    return {
        "returncode": int(return_code),
        "cancelled": cancelled,
        "timeoutSeconds": timeout,
        "durationSeconds": round(wall_time() - started, 3),
        "log": str(log_path),
        "logTail": tail(log_path, 16000),
    }
