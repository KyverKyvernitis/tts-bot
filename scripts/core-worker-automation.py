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
    payload = {
        "source_zip_url": source["url"],
        "source_sha256": source["sha256"],
        "source_bytes": source["bytes"],
        "project_subdir": "android/core-worker-app",
        "publish": True,
        "versionName": version_name,
        "versionCode": version_code,
        "filename": f"CoreWorker-v{version_name}-debug.apk",
        "notifyUsers": True,
        "notificationRequested": True,
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

    if _manifest_version_code() >= int(version_code or 0):
        pending.pop("apk_build", None)
        _save_pending(pending)
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "already_published": True, "message": "latest.json já está publicado nessa versão"}
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
    phone_changed = _has_changed(changed, "deploy/termux/phone-worker/")
    apk_changed = _has_changed(changed, "android/core-worker-app/")
    status: dict[str, Any] = {
        "ok": True,
        "changed_files": changed[:80],
        "phone_worker_changed": phone_changed,
        "apk_changed": apk_changed,
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

    status["finished_at"] = time.time()
    write_status(status)
    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Automação pós-update do Core Worker.")
    sub = parser.add_subparsers(dest="command", required=True)
    after = sub.add_parser("after-update")
    after.add_argument("--force-agent", action="store_true")
    sub.add_parser("queue-agent-update")
    sub.add_parser("queue-apk-build")
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
    if args.command == "process-pending":
        result = process_pending(worker_id=str(getattr(args, "worker_id", "") or ""))
        return 0 if result.get("ok") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
