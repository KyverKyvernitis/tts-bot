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
import fcntl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utility.commands.workers_registry import get_core_workers_registry  # noqa: E402

PHONE_WORKER_FILES: tuple[tuple[str, int], ...] = (
    ("phone_worker.py", 0o755),
    ("music_agent.py", 0o755),
    ("start-phone-worker.sh", 0o755),
    ("start-phone-music-agent.sh", 0o755),
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
LOCK_DIR = ROOT / "data" / "locks"


def _lock_key(value: str) -> str:
    value = str(value or "").strip() or "all"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value)[:120] or "all"


@contextlib.contextmanager
def _process_pending_lock(worker_id: str):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"core-worker-automation-process-pending-{_lock_key(worker_id)}.lock"
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "worker_id": str(worker_id or ""), "started_at": time.time()}, ensure_ascii=False))
            fh.flush()
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            fh.close()


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


def _load_google_services_payload_for_apk_build() -> dict[str, Any]:
    """Envia google-services.json só pelo payload autenticado do job.

    O source ZIP é servido por HTTP para o phone worker; por isso não deve conter
    o google-services.json local. O arquivo continua fora do GitHub e fora do
    ZIP público, mas chega ao worker builder no payload do job.
    """
    candidates: list[Path] = []
    for raw_path in (
        os.getenv("CORE_WORKER_GOOGLE_SERVICES_JSON"),
        os.getenv("CORE_WORKER_FIREBASE_ANDROID_CONFIG"),
        os.getenv("GOOGLE_SERVICES_JSON"),
    ):
        if raw_path:
            candidates.append(Path(str(raw_path)).expanduser())
    candidates.append(ROOT / "android" / "core-worker-app" / "app" / "google-services.json")
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            "google-services.json local não encontrado. Coloque em "
            "android/core-worker-app/app/google-services.json na VPS/build env ou defina CORE_WORKER_GOOGLE_SERVICES_JSON."
        )
    raw = path.read_bytes()
    if len(raw) > 512 * 1024:
        raise ValueError("google-services.json grande demais")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"google-services.json inválido: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("google-services.json inválido: raiz não é objeto JSON")
    project_info = data.get("project_info") if isinstance(data.get("project_info"), dict) else {}
    project_id = str(project_info.get("project_id") or "").strip()
    clients = data.get("client") if isinstance(data.get("client"), list) else []
    matched_client: dict[str, Any] | None = None
    for client in clients:
        if not isinstance(client, dict):
            continue
        info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
        android = info.get("android_client_info") if isinstance(info.get("android_client_info"), dict) else {}
        if str(android.get("package_name") or "").strip() == "dev.core.worker":
            matched_client = client
            break
    if not project_id or matched_client is None:
        raise ValueError("google-services.json precisa conter project_id e client Android package_name dev.core.worker")
    info = matched_client.get("client_info") if isinstance(matched_client.get("client_info"), dict) else {}
    mobile_app_id = str(info.get("mobilesdk_app_id") or "").strip()
    api_keys = matched_client.get("api_key") if isinstance(matched_client.get("api_key"), list) else []
    api_key = ""
    for entry in api_keys:
        if isinstance(entry, dict) and str(entry.get("current_key") or "").strip():
            api_key = str(entry.get("current_key") or "").strip()
            break
    if not mobile_app_id or not api_key:
        raise ValueError("google-services.json precisa conter mobilesdk_app_id e api_key para dev.core.worker")
    sha = hashlib.sha256(raw).hexdigest()
    return {
        "googleServicesJsonB64": base64.b64encode(raw).decode("ascii"),
        "googleServicesSha256": sha,
        "googleServicesPackage": "dev.core.worker",
        "googleServicesProjectId": project_id[:80],
        "googleServicesSource": "local-vps-payload",
    }


def _load_apk_signing_payload_for_worker_build() -> dict[str, Any]:
    """Carrega a keystore compatível sem colocá-la no Git/ZIP público.

    A VPS envia a keystore somente pelo payload autenticado do job para o phone
    worker assinar o APK com a mesma chave da versão instalada. Isso evita o
    erro do Android de conflito de pacote por assinatura diferente.
    """
    candidates: list[Path] = []
    for raw_path in (
        os.getenv("CORE_WORKER_APK_COMPAT_KEYSTORE"),
        os.getenv("CORE_WORKER_APK_UPLOAD_KEYSTORE"),
        os.getenv("CORE_WORKER_APK_SIGNING_KEYSTORE"),
        os.getenv("CORE_WORKER_APK_KEYSTORE"),
    ):
        if raw_path:
            candidates.append(Path(str(raw_path)).expanduser())
    candidates.extend([
        Path("/home/ubuntu/secrets/core-worker-upload.keystore"),
        Path.home() / ".android" / "debug.keystore",
    ])
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            "keystore compatível do Core Worker não encontrada. Preserve/copie a chave antiga para "
            "/home/ubuntu/secrets/core-worker-upload.keystore."
        )
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("keystore compatível grande demais")
    alias = (
        os.getenv("CORE_WORKER_APK_COMPAT_KEY_ALIAS")
        or os.getenv("CORE_WORKER_APK_UPLOAD_KEY_ALIAS")
        or os.getenv("CORE_WORKER_APK_KEY_ALIAS")
        or "androiddebugkey"
    ).strip()
    storepass = (
        os.getenv("CORE_WORKER_APK_COMPAT_KEYSTORE_PASSWORD")
        or os.getenv("CORE_WORKER_APK_UPLOAD_KEYSTORE_PASSWORD")
        or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD")
        or "android"
    ).strip()
    keypass = (
        os.getenv("CORE_WORKER_APK_COMPAT_KEY_PASSWORD")
        or os.getenv("CORE_WORKER_APK_UPLOAD_KEY_PASSWORD")
        or os.getenv("CORE_WORKER_APK_KEY_PASSWORD")
        or storepass
        or "android"
    ).strip()
    if not alias or not storepass:
        raise ValueError("alias/senha da keystore compatível ausentes")
    sha = hashlib.sha256(raw).hexdigest()
    return {
        "apkSigningMode": "compat-vps-debug-keystore",
        "apkSigningKeystoreB64": base64.b64encode(raw).decode("ascii"),
        "apkSigningKeystoreSha256": sha,
        "apkSigningKeyAlias": alias,
        "apkSigningStorePassword": storepass,
        "apkSigningKeyPassword": keypass,
        "apkSigningSource": "local-vps-secret",
    }


def _prepare_apk_source_zip() -> dict[str, Any]:
    project = ROOT / "android" / "core-worker-app"
    if not project.is_dir():
        raise FileNotFoundError(str(project))
    release_dir = project / "releases"
    release_dir.mkdir(parents=True, exist_ok=True)
    zip_path = release_dir / "source-core-worker-app.zip"
    excluded_dirs = {"build", ".gradle", "releases", ".idea"}
    excluded_names = {
        ".env",
        "local.properties",
        "private.properties",
        "vps.properties",
        "google-services.json",
        "firebase-service-account.json",
    }
    excluded_suffixes = (".jks", ".keystore", ".p12", ".pem", ".key")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(project.rglob("*")):
            rel = path.relative_to(project)
            if any(part in excluded_dirs for part in rel.parts):
                continue
            if path.is_dir():
                continue
            name = path.name.lower()
            rel_text = rel.as_posix().lower()
            if name in excluded_names or "service-account" in name or rel_text.endswith("/google-services.json"):
                continue
            if any(name.endswith(suffix) for suffix in excluded_suffixes):
                continue
            zf.write(path, (Path("android/core-worker-app") / rel).as_posix())
    raw = zip_path.read_bytes()
    return {
        "path": str(zip_path),
        "filename": zip_path.name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "url": f"{_public_base_url()}/core-worker/app/{zip_path.name}",
        "firebase_config_delivery": "job_payload",
    }


def _load_registry_snapshot() -> dict[str, Any]:
    try:
        timeout = float(os.getenv("CORE_WORKER_AUTOMATION_REGISTRY_LOCK_TIMEOUT_SECONDS", "0.25") or 0.25)
    except Exception:
        timeout = 0.25
    try:
        return get_core_workers_registry().snapshot(lock_timeout_seconds=max(0.0, min(3.0, timeout)))
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
    """Retorna True só para workers ativos/online abaixo da versão alvo.

    Workers antigos offline não devem manter o painel preso em "agent pendente".
    Quando um celular voltar online, o heartbeat/process-pending roda de novo e
    cria o update se a versão real ainda estiver antiga.
    """
    if not str(target_version or "").strip():
        return False
    workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict) or not worker.get("online") or worker.get("enabled") is False:
            continue
        if not _worker_supports(worker, "worker_update", "phone-worker"):
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






def _task_set(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.replace(';', ',').split(',')
    else:
        raw_items = []
    result: set[str] = set()
    for item in raw_items:
        clean = re.sub(r"[^a-z0-9_]+", "_", str(item or "").strip().lower().replace('-', '_')).strip('_')
        if clean:
            result.add(clean)
    return result

def _direct_phone_worker_config() -> dict[str, str]:
    enabled = _env_bool("PHONE_WORKER_ENABLED", True)
    host = str(os.getenv("PHONE_WORKER_HOST") or os.getenv("CORE_WORKER_PHONE_HOST") or "").strip()
    port = str(os.getenv("PHONE_WORKER_PORT") or "8766").strip() or "8766"
    scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
    token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
    return {"enabled": "1" if enabled else "0", "host": host, "port": port, "scheme": scheme, "token": token}


def _direct_phone_worker_request(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    cfg = _direct_phone_worker_config()
    if cfg["enabled"] != "1" or not cfg["host"]:
        return {"ok": False, "skipped": True, "summary": "phone-worker direto não configurado"}
    url = f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}{path}"
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=max(0.8, timeout)) as resp:
            raw = resp.read()
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        return parsed if isinstance(parsed, dict) else {"ok": False, "summary": "resposta direta não é JSON object"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "status": exc.code, "summary": f"HTTP {exc.code}: {_short(body, 160)}"}
    except Exception as exc:
        return {"ok": False, "summary": f"{type(exc).__name__}: {_short(exc, 160)}"}


def _direct_phone_worker_update_if_needed(payload: dict[str, Any], target_version: str, *, force: bool = False) -> dict[str, Any]:
    status = _direct_phone_worker_request("/status", timeout=2.5)
    if not bool(status.get("ok", True)):
        return {"ok": False, "skipped": True, "summary": f"phone-worker direto indisponível: {_short(status.get('summary') or status.get('error'), 160)}"}
    current_version = str(status.get("version") or "")
    supported = _task_set(status.get("supported_tasks"))
    if supported and "worker_update" not in supported:
        return {"ok": False, "skipped": True, "current_version": current_version, "summary": "phone-worker direto não declara worker_update"}
    if not force and current_version and _version_tuple(current_version) >= _version_tuple(target_version):
        return {"ok": True, "skipped": True, "current_version": current_version, "target_version": target_version, "summary": f"phone-worker direto já está em {current_version}"}
    body = dict(payload)
    body["task"] = "worker_update"
    body.setdefault("source", "vps-updater-direct")
    result = _direct_phone_worker_request("/task", payload=body, timeout=45.0)
    result.setdefault("current_version", current_version)
    result.setdefault("target_version", target_version)
    if result.get("ok") is False:
        result.setdefault("summary", "update direto falhou")
    else:
        result.setdefault("summary", f"update direto enviado para {target_version}")
    return result

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
    direct_update = {}
    if not only_worker_id:
        direct_update = _direct_phone_worker_update_if_needed(payload, target_version, force=force)

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
    # Se nenhum worker ativo precisa da versão alvo, limpar a pendência para o
    # painel não ficar preso em "agent X pendente" por causa de registros offline
    # ou duplicatas antigas. Se algum voltar online desatualizado, process-pending
    # recria o job automaticamente no heartbeat/poll.
    if not queued and not errors and not _workers_need_agent_version(_load_registry_snapshot(), target_version):
        pending = _load_pending()
        pending.pop("agent_update", None)
        _save_pending(pending)
        return {"ok": True, "target_version": target_version, "queued": [], "skipped": skipped[:16], "errors": [], "pending": False, "direct_update": direct_update, "message": "todos os agents ativos já estão atualizados"}
    return {"ok": True, "target_version": target_version, "queued": queued, "skipped": skipped[:16], "errors": errors[:10], "pending": True, "direct_update": direct_update}

_APK_BUILD_PERMANENT_ERROR_RE = re.compile(
    r"(compiledebugjavawithjavac|javac|unclosed string literal|cannot find symbol|"
    r"manifest merger failed|processdebugmainmanifest|aapt|android resource linking failed|"
    r"execution failed for task|cmake error|ninja:|clang|externalnativebuild|"
    r"toolchain nativa incompleta|coreworkerbedrockservice|mainactivity\.java)",
    re.IGNORECASE,
)


def _apk_build_job_matches_source(job: dict[str, Any], version_name: str, source_fingerprint: str) -> bool:
    short_fp = str(source_fingerprint or "")[:12]
    haystacks: list[str] = []
    for key in ("summary", "error"):
        value = job.get(key)
        if value:
            haystacks.append(str(value))
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    for obj in (payload, result):
        for key in ("versionName", "version_name", "versionCode", "version_code", "sourceFingerprint", "source_fingerprint", "sourceSha256", "source_sha256", "notificationId", "notification_id"):
            value = obj.get(key) if isinstance(obj, dict) else None
            if value:
                haystacks.append(str(value))
    joined = " ".join(haystacks)
    has_version = not version_name or version_name in joined
    has_fp = not short_fp or short_fp in joined or str(source_fingerprint or "") in joined
    return bool(has_version and has_fp)


def _apk_build_failure_detail(job: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("summary", "error"):
        value = job.get(key)
        if value:
            pieces.append(str(value))
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    for key in ("summary", "error", "stderr_tail", "stdout_tail", "gradle_log_tail", "tail", "message"):
        value = result.get(key) if isinstance(result, dict) else None
        if value:
            pieces.append(str(value))
    return "\n".join(pieces)


def _apk_build_failure_is_permanent(job: dict[str, Any]) -> bool:
    detail = _apk_build_failure_detail(job)
    return bool(detail and _APK_BUILD_PERMANENT_ERROR_RE.search(detail))


def _recent_failed_apk_build(version_name: str, source_fingerprint: str, *, cooldown_seconds: int | None = None) -> dict[str, Any]:
    cooldown = max(60, int(cooldown_seconds or int(os.getenv("CORE_WORKER_APK_BUILD_FAILURE_COOLDOWN_SECONDS", "1800"))))
    now = time.time()
    try:
        snapshot = _load_registry_snapshot()
        jobs = snapshot.get("jobs") if isinstance(snapshot.get("jobs"), list) else []
    except Exception:
        jobs = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("type") or "") != "apk_build_debug":
            continue
        if str(job.get("status") or "").lower() != "failed":
            continue
        if not _apk_build_job_matches_source(job, version_name, source_fingerprint):
            continue
        updated = float(job.get("updated_at") or job.get("finished_at") or job.get("created_at") or 0)
        if updated and now - updated < cooldown:
            detail = _short(_apk_build_failure_detail(job), 240)
            return {
                "job": job,
                "cooldown_seconds": cooldown,
                "retry_after_seconds": max(0, int(cooldown - (now - updated))),
                "permanent": _apk_build_failure_is_permanent(job),
                "detail": detail,
            }
    return {}


def _pending_apk_build_recently_queued(pending: dict[str, Any], version_code: int, source_fingerprint: str, *, cooldown_seconds: int | None = None) -> dict[str, Any]:
    item = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
    if not item:
        return {}
    cooldown = max(60, int(cooldown_seconds or int(os.getenv("CORE_WORKER_APK_BUILD_QUEUE_COOLDOWN_SECONDS", "600"))))
    last_fp = str(item.get("last_queued_source_fingerprint") or item.get("sourceFingerprint") or "")
    last_code = int(item.get("last_queued_versionCode") or item.get("versionCode") or 0)
    last_at = float(item.get("last_queued_at") or 0)
    if not last_at or last_fp != str(source_fingerprint or "") or last_code != int(version_code or 0):
        return {}
    age = time.time() - last_at
    if age < cooldown:
        return {
            "cooldown_seconds": cooldown,
            "retry_after_seconds": max(0, int(cooldown - age)),
            "last_queued_at": last_at,
        }
    return {}


def queue_apk_build(*, manual: bool = False) -> dict[str, Any]:
    registry = get_core_workers_registry()
    version_name, version_code = _read_android_version()
    source = _prepare_apk_source_zip()
    source_fingerprint = str(_current_fingerprints().get("apk_source_hash") or source["sha256"])
    notification_id = f"apk-{version_code}-{source_fingerprint[:12]}"
    try:
        firebase_config = _load_google_services_payload_for_apk_build()
        signing_config = _load_apk_signing_payload_for_worker_build()
    except Exception as exc:
        pending = _load_pending()
        message = "arquivo local necessário ausente/inválido; build do APK não foi enfileirado"
        if "google-services" in str(exc).lower():
            message = "google-services.json local ausente/inválido; build do APK não foi enfileirado"
        elif "keystore" in str(exc).lower() or "assinatura" in str(exc).lower():
            message = "keystore compatível ausente/inválida; build do APK não foi enfileirado"
        pending["apk_build"] = {
            "ok": False,
            "pending": False,
            "versionName": version_name,
            "versionCode": version_code,
            "source": source,
            "error": f"{type(exc).__name__}: {_short(exc, 200)}",
            "updated_at": time.time(),
            "message": message,
        }
        _save_pending(pending)
        return pending["apk_build"]
    payload = {
        "source_zip_url": source["url"],
        "source_sha256": source["sha256"],
        "sourceFingerprint": source_fingerprint,
        "source_bytes": source["bytes"],
        "firebase_config_delivery": source.get("firebase_config_delivery") or "job_payload",
        **firebase_config,
        **signing_config,
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
            "Phone worker assina com chave compatível; VPS só publica o resultado",
            "O app mostra Atualizar no topo quando estiver disponível",
        ],
    }
    pending = _load_pending()
    previous_apk_pending = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
    pending_item = {
        "type": "apk_build_debug",
        "versionName": version_name,
        "versionCode": version_code,
        "payload_redacted": True,
        "firebase_config_delivery": "job_payload",
        "apk_signing_delivery": "job_payload",
        "source": source,
        "created_at": float(previous_apk_pending.get("created_at") or time.time()) if isinstance(previous_apk_pending, dict) else time.time(),
        "updated_at": time.time(),
        "message": "build do APK pendente; será executado quando um worker apk-builder/turbo estiver online",
    }
    if isinstance(previous_apk_pending, dict):
        for key in ("last_queued_at", "last_queued_versionCode", "last_queued_source_fingerprint", "last_job_id"):
            if key in previous_apk_pending:
                pending_item[key] = previous_apk_pending[key]
    pending["apk_build"] = pending_item
    _save_pending(pending)

    if not _apk_needs_build(version_code, source_fingerprint):
        pending.pop("apk_build", None)
        _save_pending(pending)
        return {"ok": True, "versionName": version_name, "versionCode": version_code, "already_published": True, "sourceSha256": source.get("sha256"), "sourceFingerprint": source_fingerprint, "message": "latest.json já está publicado nessa versão/source"}
    recent_queue = {} if manual else _pending_apk_build_recently_queued(pending, version_code, source_fingerprint)
    if recent_queue:
        item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
        item.update({
            "ok": True,
            "pending": True,
            "blocked_by_recent_queue": True,
            "retry_after_seconds": recent_queue.get("retry_after_seconds"),
            "updated_at": time.time(),
            "message": "build do APK já foi enfileirado recentemente; aguardando resultado/cooldown para evitar loop",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item
    failed_recent = {} if manual else _recent_failed_apk_build(version_name, source_fingerprint)
    if failed_recent:
        item = dict(pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {})
        item.update({
            "ok": False,
            "pending": False,
            "blocked_by_recent_failure": True,
            "last_failed_job_id": (failed_recent.get("job") or {}).get("job_id"),
            "retry_after_seconds": failed_recent.get("retry_after_seconds"),
            "updated_at": time.time(),
            "permanent_failure": bool(failed_recent.get("permanent")),
            "last_failure_detail": failed_recent.get("detail"),
            "message": "build do APK falhou recentemente; retry automático bloqueado para evitar loop; use retry manual após corrigir o erro",
        })
        pending["apk_build"] = item
        _save_pending(pending)
        return item
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
            summary=f"build automático APK {version_name} {source_fingerprint[:12]}",
        )
        pending = _load_pending()
        item = pending.get("apk_build") if isinstance(pending.get("apk_build"), dict) else {}
        item.update({
            "ok": True,
            "pending": True,
            "last_queued_at": time.time(),
            "last_queued_versionCode": version_code,
            "last_queued_source_fingerprint": source_fingerprint,
            "last_job_id": (result.get("job") or {}).get("job_id") if isinstance(result.get("job"), dict) else None,
            "message": "build do APK enfileirado; aguardando resultado do worker builder",
        })
        pending["apk_build"] = item
        _save_pending(pending)
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


def _automation_time_budget_seconds() -> float:
    try:
        return max(5.0, min(180.0, float(os.getenv("CORE_WORKER_AUTOMATION_TIME_BUDGET_SECONDS", "45") or 45)))
    except Exception:
        return 45.0


def _budget_exceeded(started: float) -> bool:
    return (time.monotonic() - started) >= _automation_time_budget_seconds()


def process_pending(*, worker_id: str = "") -> dict[str, Any]:
    started = time.monotonic()
    pending = _load_pending()
    result: dict[str, Any] = {"ok": True, "worker_id": worker_id, "processed_at": time.time()}
    snapshot = _load_registry_snapshot()
    current = _current_fingerprints()
    target_agent = str(current.get("phone_worker_version") or "")
    apk_version_code = int(current.get("apk_versionCode") or 0)
    apk_source_hash = str(current.get("apk_source_hash") or "")

    if _env_bool("CORE_WORKER_AUTO_BOOT_REPAIR_ON_PENDING", False) and not _budget_exceeded(started):
        result["boot_repair"] = queue_boot_repairs(only_worker_id=worker_id)
    else:
        result["boot_repair"] = {"ok": True, "skipped": "disabled_by_default"}

    agent_needed = False if _budget_exceeded(started) else _workers_need_agent_version(snapshot, target_agent)
    if not _budget_exceeded(started) and (pending.get("agent_update") or agent_needed):
        result["agent_update"] = queue_agent_updates(force=False, only_worker_id=worker_id)
        if agent_needed:
            result["agent_update_detected"] = {"target_version": target_agent, "reason": "worker abaixo da versão esperada"}
    elif pending.get("agent_update") or agent_needed:
        result["agent_update"] = {"ok": True, "skipped": "time_budget_exceeded"}

    apk_needed = False if _budget_exceeded(started) else _apk_needs_build(apk_version_code, apk_source_hash)
    if not _budget_exceeded(started) and (pending.get("apk_build") or apk_needed):
        result["apk_build"] = queue_apk_build()
        if apk_needed:
            result["apk_build_detected"] = {"versionCode": apk_version_code, "reason": "latest.json ausente/antigo ou source divergente"}
    elif pending.get("apk_build") or apk_needed:
        result["apk_build"] = {"ok": True, "skipped": "time_budget_exceeded"}

    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
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
        result = queue_apk_build(manual=True)
        write_status({"manual": True, "apk_build": result, "pending": _load_pending(), "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "queue-boot-repair":
        result = queue_boot_repairs()
        write_status({"manual": True, "boot_repair": result, "finished_at": time.time()})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    if args.command == "process-pending":
        worker_id = str(getattr(args, "worker_id", "") or "")
        with _process_pending_lock(worker_id) as acquired:
            if not acquired:
                result = {"ok": True, "skipped": "already_running", "worker_id": worker_id, "processed_at": time.time()}
                write_status({"process_pending": result, "finished_at": time.time()})
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                return 0
            result = process_pending(worker_id=worker_id)
            return 0 if result.get("ok") else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
