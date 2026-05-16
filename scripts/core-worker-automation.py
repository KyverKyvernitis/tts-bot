#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
import contextlib
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utility.commands.workers_registry import get_core_workers_registry  # noqa: E402

PHONE_WORKER_FILES: tuple[tuple[str, int], ...] = (
    ("phone_worker.py", 0o755),
    ("start-phone-worker.sh", 0o755),
    ("watch-phone-worker.sh", 0o755),
    ("pair-phone-worker.sh", 0o755),
    ("bootstrap-phone-worker.sh", 0o755),
    ("install.sh", 0o755),
    ("README.md", 0o644),
    ("phone-worker.env.example", 0o600),
)

PENDING_PATH = ROOT / "data" / "core_worker_automation_pending.json"
STATUS_PATH = ROOT / "data" / "core_worker_automation_status.json"
STATE_PATH = ROOT / "data" / "core_worker_automation_state.json"


def _load_repo_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or ""):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_repo_env()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _changed_files_from_env() -> list[str]:
    raw = os.getenv("CORE_WORKER_CHANGED_FILES") or ""
    items = []
    for line in raw.splitlines():
        clean = line.strip()
        if clean and clean not in items:
            items.append(clean)
    return items


def _has_changed(changed_files: Iterable[str], prefix: str) -> bool:
    return any(str(item).startswith(prefix) for item in changed_files)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(root: Path, *, exclude_dirs: set[str] | None = None) -> str:
    exclude_dirs = set(exclude_dirs or set())
    digest = hashlib.sha256()
    if not root.exists():
        return ""
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts):
            continue
        if not path.is_file():
            continue
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _current_fingerprints() -> dict[str, Any]:
    phone_dir = ROOT / "deploy" / "termux" / "phone-worker"
    android_dir = ROOT / "android" / "core-worker-app"
    version_name, version_code = _read_android_version()
    return {
        "phone_worker_version": _read_phone_worker_version(),
        "phone_worker_hash": _hash_tree(phone_dir),
        "apk_versionName": version_name,
        "apk_versionCode": version_code,
        "apk_source_hash": _hash_tree(android_dir, exclude_dirs={"build", ".gradle", "releases"}),
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _read_phone_worker_version() -> str:
    path = ROOT / "deploy" / "termux" / "phone-worker" / "phone_worker.py"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    m = re.search(r'^PHONE_WORKER_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1) if m else "desconhecida"


def _read_android_version() -> tuple[str, int]:
    path = ROOT / "android" / "core-worker-app" / "app" / "build.gradle"
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    m_name = re.search(r'versionName\s+["\']([^"\']+)["\']', text)
    m_code = re.search(r"versionCode\s+(\d+)", text)
    return (m_name.group(1) if m_name else "0.0.0", int(m_code.group(1)) if m_code else 0)


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) if parts else (0,)


def _public_base_url() -> str:
    explicit = str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("CORE_WORKER_VPS_URL") or os.getenv("VPS_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit and "IP_TAILSCALE_DA_VPS" not in explicit:
        return explicit
    port = str(os.getenv("CORE_WORKER_PUBLIC_PORT") or os.getenv("PORT") or "10000").strip() or "10000"
    host = str(os.getenv("CORE_WORKER_PUBLIC_HOST") or os.getenv("VPS_TAILSCALE_HOST") or "").strip()
    if not host:
        try:
            proc = subprocess.run(["tailscale", "ip", "-4"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False)
            for line in (proc.stdout or "").splitlines():
                candidate = line.strip()
                if re.fullmatch(r"100(?:\.\d{1,3}){3}", candidate):
                    host = candidate
                    break
        except Exception:
            host = ""
    if host:
        return f"http://{host}:{port}"
    return f"http://IP_TAILSCALE_DA_VPS:{port}"


def _build_worker_update_payload(*, scripts_only: bool = False) -> dict[str, Any]:
    src = ROOT / "deploy" / "termux" / "phone-worker"
    targets = PHONE_WORKER_FILES if not scripts_only else tuple(item for item in PHONE_WORKER_FILES if item[0].endswith(".sh"))
    files: list[dict[str, Any]] = []
    for name, mode in targets:
        path = src / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        files.append({
            "target": name,
            "mode": mode,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        })
    if not files:
        raise RuntimeError("nenhum arquivo do phone-worker encontrado")
    return {
        "version": _read_phone_worker_version(),
        "restart": not scripts_only,
        "scripts_only": scripts_only,
        "auto": True,
        "source": "vps-updater",
        "files": files,
    }


def _prepare_apk_source_zip() -> dict[str, Any]:
    project = ROOT / "android" / "core-worker-app"
    if not project.is_dir():
        raise FileNotFoundError(str(project))
    release_dir = project / "releases"
    release_dir.mkdir(parents=True, exist_ok=True)
    zip_path = release_dir / "source-core-worker-app.zip"
    excluded_dirs = {"build", ".gradle", "releases"}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(project.rglob("*")):
            rel = path.relative_to(project)
            if any(part in excluded_dirs for part in rel.parts):
                continue
            if path.is_dir():
                continue
            zf.write(path, (Path("android/core-worker-app") / rel).as_posix())
    raw = zip_path.read_bytes()
    return {
        "path": str(zip_path),
        "filename": zip_path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "url": f"{_public_base_url()}/core-worker/app/{zip_path.name}",
    }


def _load_registry_snapshot() -> dict[str, Any]:
    try:
        return get_core_workers_registry().snapshot()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "workers": [], "jobs": []}


def _load_pending() -> dict[str, Any]:
    if not PENDING_PATH.exists():
        return {}
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending(data: dict[str, Any]) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in (data or {}).items() if v}
    if not clean:
        with contextlib.suppress(Exception):
            PENDING_PATH.unlink()
        return
    tmp = PENDING_PATH.with_suffix(PENDING_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(PENDING_PATH)


def _manifest_version_code() -> int:
    manifest = ROOT / "android" / "core-worker-app" / "releases" / "latest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return int(data.get("versionCode") or 0)
    except Exception:
        return 0


def _manifest_source_sha() -> str:
    manifest = ROOT / "android" / "core-worker-app" / "releases" / "latest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return str(data.get("sourceSha256") or data.get("source_sha256") or "").strip().lower()
    except Exception:
        return ""


def _workers_need_agent_version(snapshot: dict[str, Any], target_version: str) -> bool:
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        current = str(worker.get("version") or "")
        if not current or _version_tuple(current) < _version_tuple(target_version):
            return True
    return False


def _apk_needs_build(version_code: int, source_sha: str) -> bool:
    if _manifest_version_code() < int(version_code or 0):
        return True
    manifest_source = _manifest_source_sha()
    if source_sha and not manifest_source:
        return True
    return bool(source_sha and manifest_source and manifest_source != source_sha)


def _registry_raw() -> dict[str, Any]:
    try:
        path = get_core_workers_registry().path
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _active_job_exists(*, job_type: str, target_worker_id: str = "", summary_contains: str = "") -> bool:
    data = _registry_raw()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    wanted = str(job_type or "").replace("-", "_")
    target_worker_id = str(target_worker_id or "")
    summary_contains = str(summary_contains or "")
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if str(job.get("status") or "queued") not in {"queued", "running"}:
            continue
        if str(job.get("type") or "").replace("-", "_") != wanted:
            continue
        if target_worker_id and str(job.get("target_worker_id") or job.get("worker_id") or "") not in {target_worker_id, ""}:
            continue
        if summary_contains and summary_contains not in str(job.get("summary") or ""):
            continue
        return True
    return False


def _worker_supports(worker: dict[str, Any], task: str, required_capability: str = "phone-worker") -> bool:
    if not worker.get("online"):
        return False
    roles = {str(item) for item in worker.get("roles") or []}
    caps = {str(item) for item in worker.get("capabilities") or []} | roles
    tasks = {str(item).replace("-", "_") for item in worker.get("supported_tasks") or []}
    if required_capability and required_capability not in caps:
        return False
    return not tasks or task in tasks


def _worker_needs_boot_repair(worker: dict[str, Any]) -> bool:
    if not worker.get("online"):
        return False
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    boot = status.get("boot") if isinstance(status.get("boot"), dict) else {}
    if not boot and isinstance(health.get("boot"), dict):
        boot = health.get("boot")
    if boot and boot.get("ok") is False:
        return True
    if not boot and health.get("boot_ok") is False:
        return True
    scripts = status.get("scripts") if isinstance(status.get("scripts"), dict) else {}
    installs = scripts.get("installations") if isinstance(scripts.get("installations"), dict) else {}
    if installs.get("has_active_duplicates"):
        return True
    return False


def queue_boot_repairs(*, only_worker_id: str = "") -> dict[str, Any]:
    registry = get_core_workers_registry()
    snapshot = _load_registry_snapshot()
    workers = [w for w in snapshot.get("workers") or [] if isinstance(w, dict)]
    queued: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id)
        if not worker_id:
            continue
        if only_worker_id and worker_id != only_worker_id:
            continue
        if not _worker_needs_boot_repair(worker):
            skipped.append(f"{name}: boot ok")
            continue
        if not _worker_supports(worker, "boot_repair", "phone-worker"):
            skipped.append(f"{name}: sem suporte/offline")
            continue
        if _active_job_exists(job_type="boot_repair", target_worker_id=worker_id):
            skipped.append(f"{name}: boot_repair já pendente")
            continue
        try:
            result = registry.create_job(
                job_type="boot_repair",
                payload={"auto": True, "source": "vps-updater", "reason": "boot incompleto ou duplicata ativa"},
                created_by_id=0,
                created_by_name="VPS updater",
                target_worker_id=worker_id,
                required_capabilities=["phone-worker"],
                ttl_seconds=900,
                lease_seconds=120,
                max_attempts=2,
                summary="auto-repair boot Core Worker",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            queued.append(f"{name}:{job.get('job_id') or 'job'}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short(exc, 120)}")
    return {"ok": True, "queued": queued, "skipped": skipped[:16], "errors": errors[:10]}


def queue_agent_updates(*, force: bool = False, only_worker_id: str = "") -> dict[str, Any]:
    registry = get_core_workers_registry()
    snapshot = _load_registry_snapshot()
    workers = [w for w in snapshot.get("workers") or [] if isinstance(w, dict)]
    payload = _build_worker_update_payload()
    target_version = str(payload.get("version") or "desconhecida")

    pending = _load_pending()
    pending["agent_update"] = {
        "type": "worker_update",
        "target_version": target_version,
        "payload": payload,
        "created_at": float((pending.get("agent_update") or {}).get("created_at") or time.time()) if isinstance(pending.get("agent_update"), dict) else time.time(),
        "updated_at": time.time(),
        "message": "agent update pendente; será aplicado quando workers compatíveis aparecerem",
    }
    _save_pending(pending)

    queued: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id)
        if not worker_id:
            continue
        if only_worker_id and worker_id != only_worker_id:
            continue
        if not _worker_supports(worker, "worker_update", "phone-worker"):
            skipped.append(f"{name}: incompatível/offline")
            continue
        current_version = str(worker.get("version") or "")
        if not force and current_version and _version_tuple(current_version) >= _version_tuple(target_version):
            skipped.append(f"{name}: já em {current_version}")
            continue
        if _active_job_exists(job_type="worker_update", target_worker_id=worker_id, summary_contains=target_version):
            skipped.append(f"{name}: job já pendente")
            continue
        try:
            result = registry.create_job(
                job_type="worker_update",
                payload=payload,
                created_by_id=0,
                created_by_name="VPS updater",
                target_worker_id=worker_id,
                required_capabilities=["phone-worker"],
                ttl_seconds=1800,
                lease_seconds=240,
                max_attempts=2,
                summary=f"auto-update agent {target_version}",
            )
            job = result.get("job") if isinstance(result, dict) else {}
            queued.append(f"{name}:{job.get('job_id') or 'job'}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {_short(exc, 120)}")
    # Se todos os workers conhecidos já estão na versão alvo, limpar a pendência
    # para o painel não ficar mostrando update eterno. Se houver offline, erro ou
    # incompatível, mantemos para tentar de novo quando o worker aparecer.
    if not only_worker_id and not queued and not errors and workers and skipped and all(": já em " in item for item in skipped):
        pending = _load_pending()
        pending.pop("agent_update", None)
        _save_pending(pending)
        return {"ok": True, "target_version": target_version, "queued": [], "skipped": skipped[:16], "errors": [], "pending": False, "message": "todos os agents conhecidos já estão atualizados"}
    return {"ok": True, "target_version": target_version, "queued": queued, "skipped": skipped[:16], "errors": errors[:10], "pending": True}

def queue_apk_build() -> dict[str, Any]:
    registry = get_core_workers_registry()
    version_name, version_code = _read_android_version()
    source = _prepare_apk_source_zip()
    source_fingerprint = str(_current_fingerprints().get("apk_source_hash") or source["sha256"])
    notification_id = f"apk-{version_code}-{source_fingerprint[:12]}"
    payload = {
        "source_zip_url": source["url"],
        "source_sha256": source["sha256"],
        "sourceFingerprint": source_fingerprint,
        "source_bytes": source["bytes"],
        "project_subdir": "android/core-worker-app",
        "publish": True,
        "versionName": version_name,
        "versionCode": version_code,
        "filename": f"CoreWorker-v{version_name}-debug.apk",
        "notifyUsers": True,
        "notificationRequested": True,
        "notificationId": notification_id,
        "coreWorkerVpsUrl": _public_base_url(),
        "coreWorkerVpsLabel": os.getenv("CORE_WORKER_VPS_LABEL") or "VPS principal",
        "changelog": [
            "APK compilado automaticamente por worker builder",
            "VPS assina e publica o resultado",
            "O app mostra Atualizar no topo quando estiver disponível",
        ],
    }
    pending = _load_pending()
    pending["apk_build"] = {
        "type": "apk_build_debug",
        "versionName": version_name,
        "versionCode": version_code,
        "payload": payload,
        "source": source,
        "created_at": float((pending.get("apk_build") or {}).get("created_at") or time.time()) if isinstance(pending.get("apk_build"), dict) else time.time(),
        "updated_at": time.time(),
        "message": "build do APK pendente; será executado quando um worker apk-builder/turbo estiver online",
    }
    _save_pending(pending)

    if not _apk_needs_build(version_code, source_fingerprint):
        pending.pop("apk_build", None)
        _save_pending(pending)
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "already_published": True, "sourceSha256": source.get("sha256"), "sourceFingerprint": source_fingerprint, "message": "latest.json já está publicado nessa versão/source"}
    if _active_job_exists(job_type="apk_build_debug", summary_contains=version_name):
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "pending": True, "message": "build do APK já está na fila"}
    try:
        result = registry.create_job(
            job_type="apk_build_debug",
            payload=payload,
            created_by_id=0,
            created_by_name="VPS updater",
            required_capabilities=["apk-builder"],
            ttl_seconds=7200,
            lease_seconds=7200,
            max_attempts=1,
            summary=f"build automático APK {version_name}",
        )
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "source": source, "job": result.get("job"), "pending": True}
    except Exception as exc:
        pending = _load_pending()
        item = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
        item.update({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "updated_at": time.time(),
            "message": "build do APK pendente; nenhum worker apk-builder online compatível agora",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item


def process_pending(*, worker_id: str = "") -> dict[str, Any]:
    pending = _load_pending()
    result: dict[str, Any] = {"ok": True, "worker_id": worker_id, "processed_at": time.time()}
    # Mesmo sem pendência explícita, heartbeat/poll de worker online deve reparar
    # boot incompleto automaticamente. Isso evita depender do botão manual.
    result["boot_repair"] = queue_boot_repairs(only_worker_id=worker_id)
    if pending.get("agent_update"):
        result["agent_update"] = queue_agent_updates(force=False, only_worker_id=worker_id)
    if pending.get("apk_build"):
        result["apk_build"] = queue_apk_build()
    write_status({"process_pending": result, "pending": _load_pending(), "finished_at": time.time()})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result

def write_status(status: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def after_update(force_agent: bool = False) -> int:
    changed = _changed_files_from_env()
    snapshot = _load_registry_snapshot()
    current = _current_fingerprints()
    previous = _load_state()
    phone_hash_changed = bool(previous.get("phone_worker_hash") and previous.get("phone_worker_hash") != current.get("phone_worker_hash"))
    apk_hash_changed = bool(previous.get("apk_source_hash") and previous.get("apk_source_hash") != current.get("apk_source_hash"))
    phone_changed = (
        _has_changed(changed, "deploy/termux/phone-worker/")
        or force_agent
        or phone_hash_changed
        or _workers_need_agent_version(snapshot, str(current.get("phone_worker_version") or ""))
    )
    apk_changed = (
        _has_changed(changed, "android/core-worker-app/")
        or apk_hash_changed
        or _apk_needs_build(int(current.get("apk_versionCode") or 0), str(current.get("apk_source_hash") or ""))
    )
    status: dict[str, Any] = {
        "ok": True,
        "changed_files": changed[:80],
        "fingerprints": current,
        "previous_fingerprints_present": bool(previous),
        "phone_worker_hash_changed": phone_hash_changed,
        "apk_source_hash_changed": apk_hash_changed,
        "phone_worker_changed": phone_changed,
        "apk_changed": apk_changed,
        "workers_need_agent": _workers_need_agent_version(snapshot, str(current.get("phone_worker_version") or "")),
        "apk_needs_build": _apk_needs_build(int(current.get("apk_versionCode") or 0), str(current.get("apk_source_hash") or "")),
        "started_at": time.time(),
        "base_url": _public_base_url(),
    }
    if phone_changed and _env_bool("CORE_WORKER_AUTO_AGENT_UPDATE_ENABLED", True):
        status["agent_update"] = queue_agent_updates(force=force_agent)
    elif phone_changed:
        status["agent_update"] = {"ok": True, "skipped": ["desativado por CORE_WORKER_AUTO_AGENT_UPDATE_ENABLED=false"]}

    if apk_changed and _env_bool("CORE_WORKER_AUTO_APK_BUILD_ENABLED", True):
        status["apk_build"] = queue_apk_build()
    elif apk_changed:
        status["apk_build"] = {"ok": True, "skipped": ["desativado por CORE_WORKER_AUTO_APK_BUILD_ENABLED=false"]}

    status["boot_repair"] = queue_boot_repairs()

    status["finished_at"] = time.time()
    write_status(status)
    state = dict(current)
    state["updated_at"] = status["finished_at"]
    _save_state(state)
    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Automação pós-update do Core Worker.")
    sub = parser.add_subparsers(dest="command", required=True)
    after = sub.add_parser("after-update")
    after.add_argument("--force-agent", action="store_true")
    sub.add_parser("queue-agent-update")
    sub.add_parser("queue-apk-build")
    sub.add_parser("queue-boot-repair")
    process = sub.add_parser("process-pending")
    process.add_argument("--worker-id", default="")
    args = parser.parse_args()
    if args.command == "after-update":
        return after_update(force_agent=bool(args.force_agent))
    if args.command == "queue-agent-update":
        result = queue_agent_updates(force=True)
        write_status({"manual": True, "agent_update": result, "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "queue-apk-build":
        result = queue_apk_build()
        write_status({"manual": True, "apk_build": result, "pending": _load_pending(), "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "queue-boot-repair":
        result = queue_boot_repairs()
        write_status({"manual": True, "boot_repair": result, "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "process-pending":
        result = process_pending(worker_id=str(getattr(args, "worker_id", "") or ""))
        return 0 if result.get("ok") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
