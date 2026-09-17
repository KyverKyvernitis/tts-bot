"""Builder autocontido do Core Worker APK.

Executa somente jobs allowlist de build/publicação. O primeiro APK é compilado
no Termux sem toolchain gigante nos assets. Depois da instalação, o APK baixa e
valida um toolchain Bionic externo, retém o último slot saudável e executa o
Gradle diretamente no armazenamento privado. A VPS entrega fonte/artefatos e
recebe o APK pronto; nunca executa Gradle, JDK ou Android SDK.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from coreworker.apk_identity import assert_expected_apk_identity, inspect_apk_identity
from coreworker.builder import artifact as _builder_artifact
from coreworker.builder import build as _builder_build
from coreworker.builder import preflight as _builder_preflight
from coreworker.builder import process as _builder_process
from coreworker.builder import private_files as _builder_private_files
from coreworker.builder import source as _builder_source
from coreworker.builder import toolchain as _builder_toolchain

SCHEMA = "core-worker-apk-self-builder-v1"
TOOLCHAIN_SCHEMA_V1 = "core-worker-android-builder-v1"
TOOLCHAIN_SCHEMA_V2 = "core-worker-android-builder-v2"
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_SOURCE_ENTRIES = 16000
MAX_SOURCE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_APK_BYTES = 1024 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 3 * 60 * 60
PRIVATE_ARTIFACT_KEEP = 3
PRIVATE_LOG_KEEP = 8
MIN_BUILD_BATTERY_PERCENT = 25
SOURCE_DOWNLOAD_ATTEMPTS = 3


SourceDownloadTransientError = _builder_source.SourceDownloadTransientError
SourceHashMismatchError = _builder_source.SourceHashMismatchError


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _safe_json_load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _safe_rel(raw: Any, fallback: str = "") -> str:
    value = str(raw or fallback).replace("\\", "/").strip().lstrip("/")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("caminho relativo inválido")
    return "/".join(parts)


def _safe_filename(raw: Any, fallback: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or fallback)).strip("-._")
    return (value or fallback)[:160]


def _same_origin(url: str, server_url: str) -> bool:
    left = urllib.parse.urlsplit(url)
    right = urllib.parse.urlsplit(server_url)
    if left.scheme not in {"http", "https"} or right.scheme not in {"http", "https"}:
        return False
    left_port = left.port or (443 if left.scheme == "https" else 80)
    right_port = right.port or (443 if right.scheme == "https" else 80)
    return left.scheme == right.scheme and (left.hostname or "").lower() == (right.hostname or "").lower() and left_port == right_port


def _resolve_toolchain(toolchain_dir: Path) -> dict[str, Any]:
    return _builder_toolchain.resolve_toolchain(
        toolchain_dir,
        safe_json_load=_safe_json_load,
        safe_rel=_safe_rel,
        toolchain_schema_v1=TOOLCHAIN_SCHEMA_V1,
        toolchain_schema_v2=TOOLCHAIN_SCHEMA_V2,
    )


def _toolchain_fingerprint(tool: dict[str, Any]) -> str:
    return _builder_toolchain.toolchain_fingerprint(tool, sha256_file=_sha256_file)


def _toolchain_environment(
    tool: dict[str, Any],
    *,
    home: Path,
    temp: Path,
    gradle_home: Path,
    clean: bool,
) -> dict[str, str]:
    return _builder_toolchain.toolchain_environment(
        tool,
        home=home,
        temp=temp,
        gradle_home=gradle_home,
        clean=clean,
    )


def _run_smoke_command(name: str, command: list[str], env: dict[str, str], timeout: int) -> dict[str, Any]:
    return _builder_toolchain.run_smoke_command(name, command, env, timeout, short=_short)


def _toolchain_smoke(files: Path, tool: dict[str, Any], *, force: bool) -> dict[str, Any]:
    return _builder_toolchain.toolchain_smoke(
        files,
        tool,
        force=force,
        fingerprint=_toolchain_fingerprint,
        safe_json_load=_safe_json_load,
        atomic_json=_atomic_json,
        environment=_toolchain_environment,
        run_command=_run_smoke_command,
        now_ms=_now_ms,
    )


def preflight(files_dir: str, native_dir: str, run_smoke: bool = False) -> str:
    return _builder_preflight.public_preflight(
        files_dir,
        native_dir,
        run_smoke,
        resolve_toolchain=_resolve_toolchain,
        toolchain_smoke=_toolchain_smoke,
        safe_json_load=_safe_json_load,
        is_inside=_is_inside,
        atomic_json=_atomic_json,
        now_ms=_now_ms,
        schema=SCHEMA,
    )


def _download_source(url: str, target: Path, expected_sha: str, expected_bytes: int, server_url: str) -> dict[str, Any]:
    return _builder_source.download_source(
        url, target, expected_sha, expected_bytes, server_url,
        same_origin=_same_origin, short=_short, max_source_bytes=MAX_SOURCE_BYTES,
        attempts=SOURCE_DOWNLOAD_ATTEMPTS, urlopen=urllib.request.urlopen,
        request_type=urllib.request.Request, sleep=time.sleep, replace=os.replace,
        open_directory=os.open, fsync=os.fsync, close=os.close, getpid=os.getpid,
    )


def _safe_extract_zip(source: Path, target: Path) -> dict[str, Any]:
    return _builder_source.safe_extract_zip(
        source, target, zipfile_type=zipfile.ZipFile, copyfileobj=shutil.copyfileobj,
        is_inside=_is_inside, max_entries=MAX_SOURCE_ENTRIES,
        max_expanded_bytes=MAX_SOURCE_EXPANDED_BYTES,
    )


def _find_project(source_root: Path, project_subdir: str) -> Path:
    return _builder_source.find_project(source_root, project_subdir, safe_rel=_safe_rel)


def _decode_b64(payload: dict[str, Any], names: tuple[str, ...], max_bytes: int, label: str) -> bytes:
    return _builder_private_files.decode_b64(payload, names, max_bytes, label)


def _inject_private_files(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return _builder_private_files.inject_private_files(project, payload)



def _read_meminfo_bytes() -> dict[str, int]:
    return _builder_preflight.read_meminfo_bytes()


def _tree_bytes(root: Path, *, entry_limit: int = 80_000) -> int:
    return _builder_preflight.tree_bytes(root, entry_limit=entry_limit)


def _active_heavy_build_processes() -> list[dict[str, Any]]:
    return _builder_preflight.active_heavy_build_processes(short=_short)


def _effective_gradle_heap_mb(available_bytes: int) -> int:
    return _builder_preflight.effective_gradle_heap_mb(available_bytes)


def _resource_preflight(
    files: Path,
    project: Path | None,
    payload: dict[str, Any],
    tool: dict[str, Any],
) -> dict[str, Any]:
    return _builder_preflight.resource_preflight(
        files,
        project,
        payload,
        tool,
        read_meminfo=_read_meminfo_bytes,
        tree_size=_tree_bytes,
        active_processes=_active_heavy_build_processes,
        heap_mb=_effective_gradle_heap_mb,
        max_source_expanded_bytes=MAX_SOURCE_EXPANDED_BYTES,
        min_build_battery_percent=MIN_BUILD_BATTERY_PERCENT,
    )


_TRANSIENT_FAILURE_RE = re.compile(
    r"outofmemoryerror|java heap space|gc overhead|killed|signal 9|cannot allocate memory|"
    r"no space left on device|enospc|timed? ?out|timeout|connection reset|network is unreachable|"
    r"temporary failure|preflight_blocked|battery_low|temperature_high|thermal_severe|builder_busy|"
    r"network_truncation|source_download_retry_exhausted|source_http_(?:408|425|429|5\d\d)|lease_ownership_lost",
    re.IGNORECASE,
)
_DETERMINISTIC_FAILURE_RE = re.compile(
    r"cannot find symbol|compilation failed|manifest merger failed|resource .* not found|"
    r"aapt2? .*error:|google-services|signing|keystore|package .* does not exist|"
    r"source zip contém caminho inseguro|sha256 do source zip divergente|source_hash_mismatch|"
    r"source_length_contract_mismatch|source_sha256|source_fingerprint|source_http_4\d\d|toolchain .* inválido|"
    r"versionname .* divergente|versioncode .* divergente",
    re.IGNORECASE,
)


def _classify_failure(detail: Any) -> str:
    text = str(detail or "")
    if _TRANSIENT_FAILURE_RE.search(text):
        return "transient"
    if _DETERMINISTIC_FAILURE_RE.search(text):
        return "deterministic"
    return "unknown"


def _validated_source_identifiers(payload: dict[str, Any]) -> tuple[str, int, str]:
    """Exige identidade criptográfica completa antes de consumir a source."""
    expected_sha = str(payload.get("source_sha256") or payload.get("sourceSha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("source_sha256 ausente ou inválido")
    try:
        expected_bytes = int(payload.get("source_bytes") or payload.get("sourceBytes") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("source_bytes inválido") from exc
    if expected_bytes < 0 or expected_bytes > MAX_SOURCE_BYTES:
        raise ValueError("source_bytes fora do limite")
    source_fingerprint = str(payload.get("sourceFingerprint") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
        raise ValueError("source_fingerprint ausente ou inválido")
    return expected_sha, expected_bytes, source_fingerprint


def _proc_start_ticks(pid: int) -> int:
    return _builder_process.proc_start_ticks(pid)



def _process_group_alive(pgid: int) -> bool:
    return _builder_process.process_group_alive(pgid)



def _live_process_group_members(pgid: int) -> list[int]:
    return _builder_process.live_process_group_members(pgid)



def _validated_orphaned_gradle_group(pgid: int, project: Path, work: Path) -> bool:
    return _builder_process.validated_orphaned_gradle_group(pgid, project, work, is_inside=_is_inside)



def _signal_owned_process(process: subprocess.Popen[Any], sig: int) -> None:
    _builder_process.signal_owned_process(process, sig)



def _stop_owned_process(process: subprocess.Popen[Any], *, grace_seconds: float = 10.0) -> int:
    return _builder_process.stop_owned_process(
        process, grace_seconds=grace_seconds,
        group_alive=_process_group_alive, signal_process=_signal_owned_process,
        monotonic=time.monotonic, sleep=time.sleep,
    )



def _payload_cancellation_marker(builder: Path, payload: dict[str, Any]) -> Path | None:
    return _builder_process.payload_cancellation_marker(builder, payload, is_inside=_is_inside)



def _update_active_job_stage(files: Path, job_id: str, stage: str, summary: str) -> bool:
    """Publica o estágio para o lease keeper sem trocar ownership do job."""
    path = files / "apk-agent/active-job.json"
    try:
        active = _safe_json_load(path)
        if not active or str(active.get("job_id") or "") != str(job_id or ""):
            return False
        active["stage"] = str(stage or "running")
        active["summary"] = _short(summary, 240)
        active["updated_at"] = _now_ms()
        _atomic_json(path, active)
        return True
    except Exception:
        return False


def _acquire_build_lock(builder: Path, job_id: str, attempt: int) -> tuple[bool, Path, dict[str, Any]]:
    return _builder_process.acquire_build_lock(
        builder, job_id, attempt, atomic_json=_atomic_json, safe_json_load=_safe_json_load,
        proc_ticks=_proc_start_ticks, group_alive=_process_group_alive, short=_short,
        schema=SCHEMA, wall_time=time.time, rmtree=shutil.rmtree,
    )



def _owner_python_execution_alive(owner: dict[str, Any]) -> bool:
    return _builder_process.owner_python_execution_alive(owner, proc_ticks=_proc_start_ticks)



def _result_outbox_path(files_dir: str, job_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", str(job_id or "").strip())
    return Path(files_dir) / "core-worker-agent/outbox" / ((safe or "invalid-job") + ".json")


def _raise_if_build_cancelled(lock: Path, registry_marker: Path | None = None) -> None:
    _builder_process.raise_if_build_cancelled(
        lock, registry_marker, error_type=SourceDownloadTransientError,
    )



def _safe_gradle_owner(lock: Path, job_id: str, attempt: int = 0) -> tuple[dict[str, Any], int, bool]:
    return _builder_process.safe_gradle_owner(
        lock, job_id, attempt, safe_json_load=_safe_json_load, proc_ticks=_proc_start_ticks,
        group_alive=_process_group_alive, validate_orphaned_group=_validated_orphaned_gradle_group,
        is_inside=_is_inside,
    )



def reconcile_interrupted_build(files_dir: str, job_id: str, attempt: int = 0) -> str:
    return _builder_process.reconcile_interrupted_build(
        files_dir, job_id, attempt, safe_owner=_safe_gradle_owner, atomic_json=_atomic_json,
        safe_json_load=_safe_json_load, process_group_alive_fn=_process_group_alive,
        proc_ticks=_proc_start_ticks, owner_python_alive=_owner_python_execution_alive,
        result_outbox_path=_result_outbox_path, finalize_attempt=finalize_build_attempt,
        wall_time=time.time, monotonic=time.monotonic, sleep=time.sleep,
    )



def finalize_build_attempt(files_dir: str, job_id: str, attempt: int = 0) -> str:
    return _builder_process.finalize_build_attempt(
        files_dir, job_id, attempt, safe_json_load=_safe_json_load, safe_owner=_safe_gradle_owner,
        process_group_alive_fn=_process_group_alive, is_inside=_is_inside,
        cleanup_storage=_cleanup_private_builder_storage, rmtree=shutil.rmtree,
    )



def _hydrate_runtime_assets(project: Path, native_dir: Path, repro_assets: Path) -> dict[str, Any]:
    return _builder_private_files.hydrate_runtime_assets(
        project, native_dir, repro_assets, sha256_file=_sha256_file,
    )


def _tail(path: Path, limit: int = 16000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as fh:
        size = path.stat().st_size
        fh.seek(max(0, size - limit * 2))
        raw = fh.read(limit * 2)
    return raw.decode("utf-8", errors="replace")[-limit:]


def _run_gradle(
    files: Path,
    native: Path,
    project: Path,
    payload: dict[str, Any],
    work: Path,
    log_path: Path,
    resources: dict[str, Any],
    build_lock: Path,
    registry_cancellation: Path | None,
) -> dict[str, Any]:
    del native
    return _builder_process.run_gradle(
        files, project, payload, work, log_path, resources, build_lock, registry_cancellation,
        public_preflight=preflight, toolchain_environment=_toolchain_environment,
        toolchain_fingerprint=_toolchain_fingerprint, safe_json_load=_safe_json_load,
        atomic_json=_atomic_json, proc_ticks=_proc_start_ticks, stop_process=_stop_owned_process,
        tail=_tail, schema=SCHEMA, default_timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        popen=subprocess.Popen, stdout_target=subprocess.STDOUT, wall_time=time.time,
        monotonic=time.monotonic, sleep=time.sleep,
    )



def _validate_apk(
    path: Path,
    *,
    expected_version_name: str = "",
    expected_version_code: int = 0,
) -> dict[str, Any]:
    return _builder_artifact.validate_apk(
        path,
        expected_version_name=expected_version_name,
        expected_version_code=expected_version_code,
        max_apk_bytes=MAX_APK_BYTES,
        inspect_identity=inspect_apk_identity,
        assert_expected_identity=assert_expected_apk_identity,
        sha256_file=_sha256_file,
    )



def _raise_if_publish_cancelled(marker: Path | None, connection: Any = None) -> None:
    _builder_artifact.raise_if_publish_cancelled(
        marker, connection, error_type=SourceDownloadTransientError,
    )



def _multipart_publish(
    apk_path: Path,
    fields: dict[str, Any],
    publish_url: str,
    token: str,
    worker_id: str,
    worker_version: str,
    cancellation_marker: Path | None = None,
) -> dict[str, Any]:
    return _builder_artifact.multipart_publish(
        apk_path, fields, publish_url, token, worker_id, worker_version,
        cancellation_marker=cancellation_marker,
        safe_filename=_safe_filename, short=_short,
        raise_if_cancelled=_raise_if_publish_cancelled,
        https_connection=http.client.HTTPSConnection,
        http_connection=http.client.HTTPConnection,
        wall_time=time.time, getpid=os.getpid,
    )



def _publish_latest(files: Path, payload: dict[str, Any], server_url: str, worker_id: str, token: str, worker_version: str) -> dict[str, Any]:
    return _builder_artifact.publish_latest(
        files, payload, server_url, worker_id, token, worker_version,
        payload_cancellation_marker=_payload_cancellation_marker,
        raise_if_cancelled=_raise_if_publish_cancelled,
        safe_json_load=_safe_json_load, is_inside=_is_inside,
        validate_apk_fn=_validate_apk, same_origin=_same_origin,
        multipart_publish_fn=_multipart_publish,
    )


def _persist_artifact(
    built_apk: Path,
    artifacts: Path,
    filename: str,
    notification_id: str,
    validated: dict[str, Any],
) -> Path:
    return _builder_artifact.persist_artifact(
        built_apk, artifacts, filename, notification_id, validated,
        safe_filename=_safe_filename, sha256_file=_sha256_file,
        copy2=shutil.copy2, replace=os.replace, fsync=os.fsync,
        open_directory=os.open, close=os.close, getpid=os.getpid,
    )



def _cleanup_private_builder_storage(builder: Path, current_apk: Path | None = None) -> dict[str, Any]:
    return _builder_artifact.cleanup_private_builder_storage(
        builder, current_apk,
        safe_json_load=_safe_json_load, is_inside=_is_inside,
        tree_bytes=_tree_bytes, short=_short,
        artifact_keep=PRIVATE_ARTIFACT_KEEP, log_keep=PRIVATE_LOG_KEEP,
        wall_time=time.time, rmtree=shutil.rmtree,
    )



def _build(payload: dict[str, Any], files: Path, cache: Path, native: Path, server_url: str, worker_id: str, token: str, worker_version: str) -> dict[str, Any]:
    return _builder_build.build(
        payload, files, cache, native, server_url, worker_id, token, worker_version,
        public_preflight=preflight,
        validated_source_identifiers=_validated_source_identifiers,
        resource_preflight=_resource_preflight,
        payload_cancellation_marker=_payload_cancellation_marker,
        acquire_build_lock=_acquire_build_lock,
        safe_filename=_safe_filename,
        raise_if_build_cancelled=_raise_if_build_cancelled,
        update_active_job_stage=_update_active_job_stage,
        download_source=_download_source, safe_extract_zip=_safe_extract_zip,
        find_project=_find_project, inject_private_files=_inject_private_files,
        hydrate_runtime_assets=_hydrate_runtime_assets, run_gradle=_run_gradle,
        classify_failure=_classify_failure, validate_apk=_validate_apk,
        persist_artifact=_persist_artifact,
        safe_json_load=_safe_json_load, atomic_json=_atomic_json,
        cleanup_storage=_cleanup_private_builder_storage,
        publish_latest=_publish_latest, transient_error_type=SourceDownloadTransientError,
        schema=SCHEMA, wall_time=time.time, rmtree=shutil.rmtree,
    )



def run(task: str, payload_json: str, files_dir: str, cache_dir: str, native_dir: str, server_url: str, worker_id: str, token: str, worker_version: str) -> str:
    payload = json.loads(payload_json or "{}")
    if not isinstance(payload, dict):
        payload = {}
    files = Path(files_dir)
    cache = Path(cache_dir)
    native = Path(native_dir)
    result: dict[str, Any]
    try:
        if task == "apk_build_debug":
            result = _build(payload, files, cache, native, server_url, worker_id, token, worker_version)
        elif task == "apk_publish_last":
            result = _publish_latest(files, payload, server_url, worker_id, token, worker_version)
        elif task == "apk_builder_status":
            result = json.loads(preflight(files_dir, native_dir))
        else:
            result = {"ok": False, "error": "task de autobuild não permitida", "task": task}
    except Exception as exc:
        detail = f"{type(exc).__name__}: {_short(exc, 800)}"
        category = _classify_failure(detail)
        result = {
            "ok": False,
            "task": task,
            "summary": "falha no autobuilder do APK",
            "error": detail,
            "failure_category": category,
            "retryable": category != "deterministic",
        }
    result.setdefault("task", task)
    result.setdefault("type", task)
    result.setdefault("executedBy", "core-worker-apk-self-builder")
    result.setdefault("schema", SCHEMA)
    result.setdefault("updatedAt", _now_ms())
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
