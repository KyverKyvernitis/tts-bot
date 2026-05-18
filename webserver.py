from flask import Flask, jsonify, abort, send_file, request
from waitress import serve
import os
import json
import hashlib
import re
import contextlib
import threading
import time
import uuid
import shutil
import subprocess
import tempfile
import zipfile
import urllib.error
import urllib.request
from pathlib import Path

app = Flask(__name__)

_health_provider = None
_tts_audio_lock = threading.RLock()
_core_worker_notification_lock = threading.RLock()
_core_worker_fcm_tokens_lock = threading.RLock()
_core_worker_app_heartbeat_lock = threading.RLock()
_core_worker_app_jobs_lock = threading.RLock()
_tts_audio_files: dict[str, tuple[str, float]] = {}


def _core_worker_apk_dir() -> str:
    """Diretório local usado para publicar atualizações privadas do Core Worker APK.

    O caminho pode ser definido por CORE_WORKER_APK_DIR. Por padrão fica dentro do
    repositório para publicar o APK recebido do phone worker builder.
    """
    base = os.getenv("CORE_WORKER_APK_DIR")
    if not base:
        base = os.path.join(os.getcwd(), "android", "core-worker-app", "releases")
    return os.path.abspath(base)


def _safe_core_worker_apk_file(filename: str) -> str | None:
    filename = str(filename or "").strip().replace("\\", "/")
    if not filename or filename.startswith("/") or ".." in filename.split("/"):
        return None
    lowered = filename.lower()
    if not lowered.endswith((".apk", ".json", ".txt", ".zip")):
        return None
    base = _core_worker_apk_dir()
    full = os.path.abspath(os.path.join(base, filename))
    if full != base and not full.startswith(base + os.sep):
        return None
    return full




def _safe_release_filename(value: str, *, default: str = "CoreWorker.apk") -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or default).replace("\\", "/").split("/")[-1]).strip("-._")
    if not filename:
        filename = default
    return filename[:120]




def _external_core_worker_url(path: str) -> str:
    path = str(path or "").strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    try:
        return request.url_root.rstrip("/") + path
    except Exception:
        base = str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("CORE_WORKER_VPS_URL") or "").strip().rstrip("/")
        return (base + path) if base else path


def _core_worker_apk_url(filename: str) -> str:
    return f"/core-worker/app/{_safe_release_filename(filename, default='CoreWorker.apk')}"


def _json_field(value: str, fallback):
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return fallback


def _repo_data_dir() -> str:
    base = os.getenv("CORE_WORKER_DATA_DIR") or os.path.join(os.getcwd(), "data")
    path = os.path.abspath(os.path.expanduser(base))
    os.makedirs(path, exist_ok=True)
    return path


def _safe_short_text(value, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def _atomic_write_json(path: str, data: dict, *, mode: int = 0o600) -> None:
    """Grava JSON de forma atômica e segura, com tmp único por chamada.

    O Patch 51 usava sempre o mesmo ``.tmp``. Quando o APK reportava vários
    eventos ao mesmo tempo, uma requisição podia renomear o tmp da outra e o
    endpoint /core-worker/app/notification caía com FileNotFoundError.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            with contextlib.suppress(Exception):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        with contextlib.suppress(Exception):
            os.chmod(path, mode)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _core_worker_notification_log_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_notifications.json")


def _notification_event_id(*, version_name: str, version_code: int, sha256: str) -> str:
    seed = f"{version_name}:{version_code}:{sha256}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"apk-{int(version_code or 0)}-{digest}"


def _append_core_worker_notification_event(event: dict) -> dict:
    path = _core_worker_notification_log_path()
    now = int(time.time())
    state = _safe_short_text(event.get("state") or event.get("event"), 48)
    delivered = bool(event.get("delivered", False)) or state in {"displayed", "background_displayed", "fcm_received", "fcm_displayed", "duplicate", "background_duplicate", "already_displayed", "download_started", "download_verified", "install_intent_opened", "local_agent_seen", "local_agent_unpaired", "app_opened"}
    clean = {
        "receivedAt": now,
        "notificationId": _safe_short_text(event.get("notificationId"), 96),
        "state": state,
        "delivered": delivered,
        "versionName": _safe_short_text(event.get("versionName"), 48),
        "versionCode": int(event.get("versionCode") or 0),
        "appVersion": _safe_short_text(event.get("appVersion"), 48),
        "appVersionCode": int(event.get("appVersionCode") or 0),
        "workerId": _safe_short_text(event.get("workerId"), 80),
        "installId": _safe_short_text(event.get("installId"), 80),
        "permission": _safe_short_text(event.get("permission"), 40),
        "detail": _safe_short_text(event.get("detail"), 180),
        "remoteAddr": _safe_short_text(getattr(request, "remote_addr", "") or "", 64),
    }
    with _core_worker_notification_lock:
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        events = data.get("events") if isinstance(data.get("events"), list) else []
        events.append(clean)
        events = events[-120:]
        latest_by_id = data.get("latestById") if isinstance(data.get("latestById"), dict) else {}
        nid = clean.get("notificationId") or "unknown"
        latest_by_id[nid] = clean
        data = {"ok": True, "updatedAt": now, "events": events, "latestById": latest_by_id}
        _atomic_write_json(path, data, mode=0o600)
    return clean

def _latest_core_worker_notification_summary(notification_id: str) -> dict:
    path = _core_worker_notification_log_path()
    try:
        data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
    except Exception:
        data = {}
    latest = data.get("latestById") if isinstance(data, dict) and isinstance(data.get("latestById"), dict) else {}
    record = latest.get(str(notification_id or "")) if isinstance(latest, dict) else None
    if isinstance(record, dict):
        state = str(record.get("state") or "")
        delivered = bool(record.get("delivered", False)) or state in {"displayed", "background_displayed", "fcm_received", "fcm_displayed", "duplicate", "background_duplicate", "already_displayed", "download_started", "download_verified", "install_intent_opened", "local_agent_seen", "local_agent_unpaired", "app_opened"}
        return {
            "lastState": record.get("state"),
            "lastDelivered": delivered,
            "lastReceivedAt": record.get("receivedAt"),
            "lastWorkerId": record.get("workerId"),
            "lastAppVersion": record.get("appVersion"),
            "lastDetail": record.get("detail"),
        }
    return {"lastState": "pending", "lastDelivered": False}


def _core_worker_fcm_tokens_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_fcm_tokens.json")


def _fcm_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8", errors="ignore")).hexdigest()


def _fcm_error_is_unregistered(detail: str) -> bool:
    text = str(detail or "").lower()
    return "unregistered" in text or ("http 404" in text and "requested entity was not found" in text)


def _fcm_unregistered_public_error() -> str:
    return "token FCM expirado/invalidado; aguardando novo token do APK"


def _load_core_worker_fcm_tokens() -> dict:
    path = _core_worker_fcm_tokens_path()
    try:
        data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    data["tokens"] = tokens
    data.setdefault("ok", True)
    return data


def _save_core_worker_fcm_tokens(data: dict) -> None:
    path = _core_worker_fcm_tokens_path()
    with _core_worker_fcm_tokens_lock:
        _atomic_write_json(path, data, mode=0o600)

def _register_core_worker_fcm_token(payload: dict) -> dict:
    token = str(payload.get("fcmToken") or payload.get("fcm_token") or payload.get("token") or "").strip()
    if len(token) < 20:
        raise ValueError("fcmToken ausente ou curto demais")
    now = int(time.time())
    token_hash = _fcm_token_hash(token)
    with _core_worker_fcm_tokens_lock:
        data = _load_core_worker_fcm_tokens()
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        record = tokens.get(token_hash) if isinstance(tokens.get(token_hash), dict) else {}
        was_unregistered = str(record.get("lastErrorCode") or "").upper() == "UNREGISTERED" or bool(record.get("invalidatedAt"))
        common = {
            "token": token,
            "tokenHash": token_hash,
            "workerId": _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80),
            "installId": _safe_short_text(payload.get("installId") or payload.get("install_id"), 80),
            "deviceName": _safe_short_text(payload.get("deviceName") or payload.get("device_name"), 80),
            "platform": _safe_short_text(payload.get("platform") or "android", 32),
            "source": _safe_short_text(payload.get("source") or "core-worker-apk", 48),
            "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48),
            "appVersionCode": int(payload.get("appVersionCode") or payload.get("app_version_code") or 0),
            "permission": _safe_short_text(payload.get("permission"), 40),
            "lastReason": _safe_short_text(payload.get("reason"), 64),
            "lastRemoteAddr": _safe_short_text(request.remote_addr or "", 64),
            "lastSeenAt": now,
        }
        record.update(common)
        record.setdefault("registeredAt", now)
        if was_unregistered:
            # A documentação FCM recomenda remover tokens UNREGISTERED/404 e obter um novo token no cliente.
            record.update({
                "active": False,
                "refreshRequired": True,
                "lastPushStatus": "unregistered",
                "lastErrorCode": "UNREGISTERED",
                "lastError": _fcm_unregistered_public_error(),
                "lastRejectedRegistrationAt": now,
            })
        else:
            record.update({
                "active": True,
                "refreshRequired": False,
                "lastErrorCode": "",
                "lastError": "",
            })
        tokens[token_hash] = record
        data["tokens"] = tokens
        data["updatedAt"] = now
        _atomic_write_json(_core_worker_fcm_tokens_path(), data, mode=0o600)
        public = {k: v for k, v in record.items() if k != "token"}
        if was_unregistered:
            public["refreshRequired"] = True
            public["refreshReason"] = _fcm_unregistered_public_error()
        return public


def _active_core_worker_fcm_records(*, max_age_days: int = 45) -> list[dict]:
    data = _load_core_worker_fcm_tokens()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    cutoff = int(time.time()) - max(1, int(max_age_days)) * 86400
    records: list[dict] = []
    for record in tokens.values():
        if not isinstance(record, dict) or not record.get("active"):
            continue
        token = str(record.get("token") or "").strip()
        if len(token) < 20:
            continue
        try:
            seen = int(record.get("lastSeenAt") or record.get("registeredAt") or 0)
        except Exception:
            seen = 0
        if seen and seen < cutoff:
            continue
        records.append(record)
    return records


def _core_worker_fcm_public_summary(worker_id: str = "") -> dict:
    worker_id = str(worker_id or "").strip()
    data = _load_core_worker_fcm_tokens()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    records = _active_core_worker_fcm_records(max_age_days=120)
    if worker_id:
        records = [r for r in records if str(r.get("workerId") or "") == worker_id]
    invalidated = []
    for item in tokens.values():
        if not isinstance(item, dict):
            continue
        if worker_id and str(item.get("workerId") or "") != worker_id:
            continue
        if str(item.get("lastErrorCode") or "").upper() == "UNREGISTERED" or item.get("invalidatedAt"):
            invalidated.append(item)
    last = None
    for record in records:
        if last is None or int(record.get("lastSeenAt") or 0) > int(last.get("lastSeenAt") or 0):
            last = record
    if last is None and invalidated:
        for record in invalidated:
            if last is None or int(record.get("lastSeenAt") or record.get("invalidatedAt") or 0) > int(last.get("lastSeenAt") or last.get("invalidatedAt") or 0):
                last = record
    return {
        "active": len(records),
        "needsRefresh": bool(not records and invalidated),
        "invalidated": len(invalidated),
        "lastSeenAt": int((last or {}).get("lastSeenAt") or 0),
        "lastPushAt": int((last or {}).get("lastPushAt") or 0),
        "lastPushStatus": _safe_short_text((last or {}).get("lastPushStatus"), 40),
        "lastError": _safe_short_text((last or {}).get("lastError"), 120),
        "lastErrorCode": _safe_short_text((last or {}).get("lastErrorCode"), 40),
        "lastAppVersion": _safe_short_text((last or {}).get("appVersion"), 48),
        "permission": _safe_short_text((last or {}).get("permission"), 40),
    }


def _core_worker_app_heartbeats_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_heartbeats.json")


def _append_core_worker_app_heartbeat(payload: dict) -> dict:
    now = int(time.time())
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    if not install_id and not worker_id:
        raise ValueError("installId ou workerId ausente")
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
    battery = payload.get("battery") if isinstance(payload.get("battery"), dict) else status.get("battery") if isinstance(status.get("battery"), dict) else {}
    network = payload.get("network") if isinstance(payload.get("network"), dict) else status.get("network") if isinstance(status.get("network"), dict) else {}
    update = payload.get("update") if isinstance(payload.get("update"), dict) else status.get("update") if isinstance(status.get("update"), dict) else {}
    app_status = payload.get("app_status") if isinstance(payload.get("app_status"), dict) else status.get("app_status") if isinstance(status.get("app_status"), dict) else {}
    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else status.get("storage") if isinstance(status.get("storage"), dict) else {}
    permissions = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else status.get("permissions") if isinstance(status.get("permissions"), dict) else {}
    record = {
        "receivedAt": now,
        "installId": install_id,
        "workerId": worker_id,
        "deviceName": _safe_short_text(payload.get("deviceName") or payload.get("device_name"), 80),
        "source": _safe_short_text(payload.get("source") or "core-worker-apk-internal-runtime", 64),
        "state": _safe_short_text(payload.get("state") or "internal_heartbeat", 48),
        "reason": _safe_short_text(payload.get("reason"), 64),
        "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48),
        "appVersionCode": int(payload.get("appVersionCode") or payload.get("app_version_code") or 0),
        "profile": _safe_short_text(payload.get("profile") or payload.get("profileLabel") or payload.get("profile_label"), 48),
        "runtimeMode": _safe_short_text(payload.get("runtime_mode") or runtime.get("mode") or "hybrid", 40),
        "internalRuntime": _safe_short_text(payload.get("internal_runtime") or runtime.get("internal_runtime") or "apk-heartbeat", 48),
        "internalRuntimeState": _safe_short_text(payload.get("internal_runtime_state") or runtime.get("internal_runtime_state"), 120),
        "termuxWorkerOnline": bool(payload.get("termuxWorkerOnline") or payload.get("localAgentOnline") or status.get("local_agent_online")),
        "jobsRuntime": _safe_short_text(payload.get("jobsRuntime") or runtime.get("jobs_runtime") or "termux", 40),
        "internalJobsQueue": _safe_short_text(runtime.get("internal_jobs_queue") or status.get("internal_jobs_queue"), 120),
        "internalJobsRunning": int(runtime.get("internal_jobs_running") or status.get("internal_jobs_running") or 0),
        "internalJobsPending": int(runtime.get("internal_jobs_pending") or status.get("internal_jobs_pending") or 0),
        "fcmState": _safe_short_text(status.get("fcm_state") or payload.get("fcm_state"), 80),
        "batteryPercent": int(battery.get("percent") or battery.get("percentage") or -1) if isinstance(battery, dict) else -1,
        "batteryTemperatureC": float(battery.get("temperature_c") or -1) if isinstance(battery, dict) else -1,
        "batteryCharging": bool(battery.get("charging")) if isinstance(battery, dict) else False,
        "networkType": _safe_short_text(network.get("type") if isinstance(network, dict) else "", 32),
        "networkVpn": bool(network.get("vpn")) if isinstance(network, dict) else False,
        "vpsPingMs": int(network.get("vps_ping_ms") or -1) if isinstance(network, dict) else -1,
        "updateState": _safe_short_text(update.get("state") if isinstance(update, dict) else "", 80),
        "updateAvailable": bool(update.get("available")) if isinstance(update, dict) else False,
        "lastAppError": _safe_short_text(app_status.get("last_error") if isinstance(app_status, dict) else "", 160),
        "ready": bool(app_status.get("ready")) if isinstance(app_status, dict) else False,
        "diagnosticsSummary": _safe_short_text(runtime.get("diagnostics_summary") or status.get("diagnostics_summary"), 160),
        "storageSummary": _safe_short_text(runtime.get("storage_summary") or (storage.get("summary") if isinstance(storage, dict) else ""), 120),
        "bridgeSummary": _safe_short_text(runtime.get("bridge_summary"), 120),
        "notificationPermission": _safe_short_text((permissions.get("notifications") if isinstance(permissions, dict) else ""), 32),
        "battery": battery if isinstance(battery, dict) else {},
        "network": network if isinstance(network, dict) else {},
        "update": update if isinstance(update, dict) else {},
        "appStatus": app_status if isinstance(app_status, dict) else {},
        "storage": storage if isinstance(storage, dict) else {},
        "permissions": permissions if isinstance(permissions, dict) else {},
        "remoteAddr": _safe_short_text(request.remote_addr or "", 64),
    }
    path = _core_worker_app_heartbeats_path()
    key = install_id or worker_id or "unknown"
    with _core_worker_app_heartbeat_lock:
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        latest_by_install = data.get("latestByInstallId") if isinstance(data.get("latestByInstallId"), dict) else {}
        latest_by_worker = data.get("latestByWorkerId") if isinstance(data.get("latestByWorkerId"), dict) else {}
        events = data.get("events") if isinstance(data.get("events"), list) else []
        latest_by_install[key] = record
        if worker_id:
            latest_by_worker[worker_id] = record
        events.append(record)
        events = events[-160:]
        data = {"ok": True, "updatedAt": now, "latestByInstallId": latest_by_install, "latestByWorkerId": latest_by_worker, "events": events}
        _atomic_write_json(path, data, mode=0o600)
    return record


def _core_worker_app_runtime_public_summary(worker_id: str = "", install_id: str = "") -> dict:
    path = _core_worker_app_heartbeats_path()
    try:
        data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    record = None
    if worker_id and isinstance(data.get("latestByWorkerId"), dict):
        record = data.get("latestByWorkerId", {}).get(str(worker_id))
    if record is None and install_id and isinstance(data.get("latestByInstallId"), dict):
        record = data.get("latestByInstallId", {}).get(str(install_id))
    if not isinstance(record, dict):
        return {"online": False, "lastSeenAt": 0, "state": "unknown"}
    seen = int(record.get("receivedAt") or 0)
    online = bool(seen and time.time() - seen <= 180)
    return {
        "online": online,
        "lastSeenAt": seen,
        "state": _safe_short_text(record.get("state"), 48),
        "appVersion": _safe_short_text(record.get("appVersion"), 48),
        "runtimeMode": _safe_short_text(record.get("runtimeMode"), 40),
        "internalRuntime": _safe_short_text(record.get("internalRuntime"), 48),
        "internalRuntimeState": _safe_short_text(record.get("internalRuntimeState"), 120),
        "termuxWorkerOnline": bool(record.get("termuxWorkerOnline")),
        "jobsRuntime": _safe_short_text(record.get("jobsRuntime"), 40),
        "fcmState": _safe_short_text(record.get("fcmState"), 80),
        "batteryPercent": int(record.get("batteryPercent") or -1),
        "batteryTemperatureC": float(record.get("batteryTemperatureC") or -1),
        "batteryCharging": bool(record.get("batteryCharging")),
        "networkType": _safe_short_text(record.get("networkType"), 32),
        "networkVpn": bool(record.get("networkVpn")),
        "vpsPingMs": int(record.get("vpsPingMs") or -1),
        "updateState": _safe_short_text(record.get("updateState"), 80),
        "updateAvailable": bool(record.get("updateAvailable")),
        "lastAppError": _safe_short_text(record.get("lastAppError"), 160),
        "ready": bool(record.get("ready")),
        "diagnosticsSummary": _safe_short_text(record.get("diagnosticsSummary"), 160),
        "storageSummary": _safe_short_text(record.get("storageSummary"), 120),
        "bridgeSummary": _safe_short_text(record.get("bridgeSummary"), 120),
        "notificationPermission": _safe_short_text(record.get("notificationPermission"), 32),
        "internalJobsQueue": _safe_short_text(record.get("internalJobsQueue"), 120),
        "internalJobsRunning": int(record.get("internalJobsRunning") or 0),
        "internalJobsPending": int(record.get("internalJobsPending") or 0),
        "lightJobs": _core_worker_app_jobs_public_summary(worker_id=worker_id, install_id=install_id),
    }


def _core_worker_app_jobs_path() -> str:
    return os.path.join(_repo_data_dir(), "core_worker_app_jobs.json")


def _core_worker_app_jobs_key(payload: dict) -> str:
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    return install_id or worker_id or "unknown"


CORE_WORKER_APP_JOB_ALIASES = {
    # Nomes antigos aceitos para não quebrar APKs/painéis entre patches.
    "apk_clear_app_cache": "apk_cache_cleanup",
    "apk_cleanup_runtime_cache": "apk_cache_cleanup",
    "apk_report_logs": "apk_upload_app_logs",
    "apk_status_refresh": "apk_sync_runtime_state",
}

CORE_WORKER_APP_AUTO_JOB_TYPES = {
    "apk_ping",
    "apk_diagnostic",
    "apk_check_update",
    "apk_upload_app_logs",
    "apk_runtime_diagnostic",
    "apk_worker_bridge_status",
    "apk_storage_diagnostic",
    "apk_collect_status_bundle",
    "apk_device_diagnostic",
    "apk_network_diagnostic",
    "apk_push_diagnostic",
    "apk_update_diagnostic",
    "apk_job_history",
    "apk_cache_cleanup",
}

CORE_WORKER_APP_MANUAL_JOB_TYPES = {
    "apk_download_small",
    "apk_verify_file",
    "apk_upload_report",
    "apk_test_vps_connection",
    "apk_sync_profile",
    "apk_sync_runtime_state",
}

CORE_WORKER_APP_SAFE_JOB_TYPES = (
    CORE_WORKER_APP_AUTO_JOB_TYPES
    | CORE_WORKER_APP_MANUAL_JOB_TYPES
    | set(CORE_WORKER_APP_JOB_ALIASES.keys())
)

CORE_WORKER_APP_JOB_LABELS = {
    "apk_ping": "ping interno",
    "apk_diagnostic": "diagnóstico geral",
    "apk_check_update": "checagem de atualização",
    "apk_upload_app_logs": "logs internos",
    "apk_runtime_diagnostic": "diagnóstico do runtime",
    "apk_worker_bridge_status": "ponte APK/Termux",
    "apk_storage_diagnostic": "armazenamento",
    "apk_collect_status_bundle": "pacote completo",
    "apk_device_diagnostic": "aparelho",
    "apk_network_diagnostic": "rede",
    "apk_push_diagnostic": "push",
    "apk_update_diagnostic": "update",
    "apk_job_history": "histórico",
    "apk_cache_cleanup": "limpeza de cache",
    "apk_download_small": "download pequeno",
    "apk_verify_file": "verificar arquivo",
    "apk_upload_report": "enviar relatório",
    "apk_test_vps_connection": "teste de conexão VPS",
    "apk_sync_profile": "sincronizar perfil",
    "apk_sync_runtime_state": "sincronizar runtime",
}

def _core_worker_app_normalize_job_type(job_type: object) -> str:
    raw = _safe_short_text(job_type, 48)
    return CORE_WORKER_APP_JOB_ALIASES.get(raw, raw)


def _core_worker_app_job_class(job_type: object) -> str:
    normalized = _core_worker_app_normalize_job_type(job_type)
    if normalized in CORE_WORKER_APP_AUTO_JOB_TYPES:
        return "automatic"
    if normalized in CORE_WORKER_APP_MANUAL_JOB_TYPES:
        return "manual"
    return "unknown"


CORE_WORKER_APP_JOB_MAX_DELIVER = 6
CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS = 45
CORE_WORKER_APP_JOB_DEFAULT_MAX_RETRIES = 1
CORE_WORKER_APP_JOB_RESULT_LIMIT = 300


def _core_worker_app_safe_job_payload(job: dict) -> dict:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = _core_worker_app_normalize_job_type(job.get("type"))
    clean: dict = {}
    if job_type == "apk_download_small":
        raw_url = _safe_short_text(payload.get("url") or payload.get("path") or "/core-worker/app/latest.json", 220)
        # O APK também valida host/protocolo. Aqui só removemos entradas obviamente perigosas.
        if raw_url.startswith("/") or raw_url.startswith("http://") or raw_url.startswith("https://"):
            clean["url"] = raw_url
        try:
            max_bytes = int(payload.get("maxBytes") or payload.get("max_bytes") or 262144)
        except Exception:
            max_bytes = 262144
        clean["maxBytes"] = max(1024, min(max_bytes, 262144))
        expected_sha = _safe_short_text(payload.get("sha256") or payload.get("expectedSha256"), 80).lower()
        if re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            clean["sha256"] = expected_sha
    elif job_type == "apk_verify_file":
        filename = _safe_short_text(payload.get("file") or payload.get("cacheFile") or payload.get("name"), 80)
        if filename and not filename.startswith("/") and ".." not in filename.replace("\\", "/").split("/"):
            clean["file"] = filename
        expected_sha = _safe_short_text(payload.get("sha256") or payload.get("expectedSha256"), 80).lower()
        if re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            clean["sha256"] = expected_sha
    elif job_type == "apk_sync_profile":
        profile = _safe_short_text(payload.get("profile"), 40).lower()
        if profile in {"leve", "midia", "media", "normal", "completo", "builder", "turbo", "bedrock"}:
            clean["profile"] = profile
    elif job_type in {"apk_upload_report", "apk_upload_app_logs", "apk_job_history", "apk_sync_runtime_state", "apk_cache_cleanup", "apk_device_diagnostic", "apk_network_diagnostic", "apk_push_diagnostic", "apk_update_diagnostic", "apk_runtime_diagnostic", "apk_storage_diagnostic", "apk_worker_bridge_status", "apk_collect_status_bundle"}:
        detail = _safe_short_text(payload.get("detail") or payload.get("reason"), 80)
        if detail:
            clean["detail"] = detail
    return clean


def _core_worker_app_job_timeout(job: dict) -> int:
    try:
        raw = int(job.get("timeoutSec") or job.get("timeout_seconds") or CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS)
    except Exception:
        raw = CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS
    return max(10, min(raw, 180))


def _core_worker_app_job_max_retries(job: dict) -> int:
    try:
        raw = int(job.get("maxRetries") or job.get("max_retries") or CORE_WORKER_APP_JOB_DEFAULT_MAX_RETRIES)
    except Exception:
        raw = CORE_WORKER_APP_JOB_DEFAULT_MAX_RETRIES
    return max(0, min(raw, 3))


def _core_worker_app_job_matches(job: dict, install_id: str, worker_id: str) -> bool:
    target_install = str(job.get("installId") or job.get("install_id") or "").strip()
    target_worker = str(job.get("workerId") or job.get("worker_id") or "").strip()
    return (target_install and target_install == install_id) or (target_worker and target_worker == worker_id) or (not target_install and not target_worker)


def _core_worker_app_make_timeout_record(job: dict, now: int) -> dict:
    return {
        "receivedAt": now,
        "jobId": _safe_short_text(job.get("id"), 64),
        "type": _safe_short_text(job.get("type"), 48),
        "installId": _safe_short_text(job.get("installId") or job.get("install_id"), 80),
        "workerId": _safe_short_text(job.get("workerId") or job.get("worker_id"), 80),
        "appVersion": "",
        "appVersionCode": 0,
        "ok": False,
        "message": "job interno expirou antes do APK reportar resultado",
        "error": "timeout",
        "result": {"ok": False, "error": "timeout", "attempt": int(job.get("attempt") or 0)},
    }


def _core_worker_app_public_job(job: dict, now: int) -> dict:
    out = {
        "id": _safe_short_text(job.get("id"), 64),
        "type": _safe_short_text(job.get("type"), 48),
        "reason": _safe_short_text(job.get("reason"), 80),
        "issuedAt": int(job.get("issuedAt") or now),
        "title": _safe_short_text(job.get("title"), 80),
    }
    payload = _core_worker_app_safe_job_payload(job)
    if payload:
        out["payload"] = payload
    out["timeoutSec"] = _core_worker_app_job_timeout(job)
    out["attempt"] = int(job.get("attempt") or 0)
    return out


def _core_worker_app_job_catalog() -> dict:
    return {
        "automatic": sorted(CORE_WORKER_APP_AUTO_JOB_TYPES),
        "manual": sorted(CORE_WORKER_APP_MANUAL_JOB_TYPES),
        "aliases": dict(sorted(CORE_WORKER_APP_JOB_ALIASES.items())),
        "labels": {k: CORE_WORKER_APP_JOB_LABELS.get(k, k) for k in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES | CORE_WORKER_APP_MANUAL_JOB_TYPES)},
    }


def _core_worker_app_jobs_build_summaries(data: dict, now: int | None = None) -> dict:
    now = int(now or time.time())
    if not isinstance(data, dict):
        return {}
    results = data.get("results") if isinstance(data.get("results"), list) else []
    pending = data.get("pending") if isinstance(data.get("pending"), list) else []
    running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
    keys: set[str] = set()
    for item in results:
        if isinstance(item, dict):
            key = str(item.get("installId") or item.get("workerId") or "unknown")
            keys.add(key)
    for item in pending:
        if isinstance(item, dict):
            keys.add(str(item.get("installId") or item.get("workerId") or "unknown"))
    for item in running.values():
        if isinstance(item, dict):
            keys.add(str(item.get("installId") or item.get("workerId") or "unknown"))
    summaries: dict[str, dict] = {}
    for key in keys or {"unknown"}:
        latest_by_type: dict[str, dict] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get("installId") or item.get("workerId") or "unknown")
            if item_key != key:
                continue
            typ = _core_worker_app_normalize_job_type(item.get("type"))
            prev = latest_by_type.get(typ)
            if prev is None or int(item.get("receivedAt") or 0) >= int(prev.get("receivedAt") or 0):
                latest_by_type[typ] = item
        auto_ok = 0
        auto_failed = 0
        auto_missing: list[str] = []
        failed_types: list[str] = []
        latest_public: dict[str, dict] = {}
        for typ in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES):
            rec = latest_by_type.get(typ)
            if not isinstance(rec, dict):
                auto_missing.append(typ)
                continue
            ok = bool(rec.get("ok"))
            if ok:
                auto_ok += 1
            else:
                auto_failed += 1
                failed_types.append(typ)
            latest_public[typ] = {
                "ok": ok,
                "message": _safe_short_text(rec.get("message") or rec.get("error"), 120),
                "receivedAt": int(rec.get("receivedAt") or 0),
            }
        pending_for_key = [j for j in pending if isinstance(j, dict) and str(j.get("installId") or j.get("workerId") or "unknown") == key]
        running_for_key = [j for j in running.values() if isinstance(j, dict) and str(j.get("installId") or j.get("workerId") or "unknown") == key]
        status = "ok"
        if auto_failed:
            status = "attention"
        elif auto_missing:
            status = "warming_up"
        summaries[key] = {
            "status": status,
            "autoTotal": len(CORE_WORKER_APP_AUTO_JOB_TYPES),
            "autoOk": auto_ok,
            "autoFailed": auto_failed,
            "autoMissing": auto_missing[:40],
            "manualTotal": len(CORE_WORKER_APP_MANUAL_JOB_TYPES),
            "manualTypes": sorted(CORE_WORKER_APP_MANUAL_JOB_TYPES),
            "failedTypes": failed_types[:20],
            "pending": len(pending_for_key),
            "running": len(running_for_key),
            "latestByType": latest_public,
            "updatedAt": now,
        }
    return summaries


def _core_worker_app_queue_internal_jobs_for_worker(worker_id: str = "", install_id: str = "", *, kinds: list[str] | None = None, reason: str = "manual-runtime-test") -> dict:
    now = int(time.time())
    worker_id = _safe_short_text(worker_id, 80)
    install_id = _safe_short_text(install_id, 80)
    key = install_id or worker_id or "unknown"
    job_types = [_core_worker_app_normalize_job_type(k) for k in (kinds or sorted(CORE_WORKER_APP_AUTO_JOB_TYPES))]
    job_types = [k for k in dict.fromkeys(job_types) if k in CORE_WORKER_APP_AUTO_JOB_TYPES]
    path = _core_worker_app_jobs_path()
    with _core_worker_app_jobs_lock:
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
        existing_types = set()
        for item in pending:
            if isinstance(item, dict) and _core_worker_app_job_matches(item, install_id, worker_id):
                existing_types.add(_core_worker_app_normalize_job_type(item.get("type")))
        for item in running.values():
            if isinstance(item, dict) and _core_worker_app_job_matches(item, install_id, worker_id):
                existing_types.add(_core_worker_app_normalize_job_type(item.get("type")))
        created = []
        for typ in job_types:
            if typ in existing_types:
                continue
            job_id = f"manual-{typ.replace('_', '-')}-{key[:16]}-{now}-{len(created)}"
            job = {
                "id": job_id,
                "type": typ,
                "jobClass": "automatic",
                "reason": reason,
                "issuedAt": now,
                "title": CORE_WORKER_APP_JOB_LABELS.get(typ, typ),
                "status": "pending",
                "timeoutSec": CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS,
                "maxRetries": 1,
                "installId": install_id,
                "workerId": worker_id,
            }
            pending.append(job)
            created.append(job)
        data["pending"] = pending[-160:]
        data["runningByJobId"] = running
        data["jobCatalog"] = _core_worker_app_job_catalog()
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
        data["updatedAt"] = now
        data["ok"] = True
        _atomic_write_json(path, data, mode=0o600)
    return {"ok": True, "created": len(created), "requested": len(job_types), "workerId": worker_id, "installId": install_id, "types": [j.get("type") for j in created]}


def _core_worker_app_jobs_fetch(payload: dict) -> dict:
    now = int(time.time())
    if not isinstance(payload, dict):
        payload = {}
    key = _core_worker_app_jobs_key(payload)
    install_id = _safe_short_text(payload.get("installId") or payload.get("install_id"), 80)
    worker_id = _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80)
    supported = payload.get("supportedJobs") if isinstance(payload.get("supportedJobs"), list) else []
    supported_set = {
        _core_worker_app_normalize_job_type(item)
        for item in supported
        if str(item or "").strip()
    } & set(CORE_WORKER_APP_SAFE_JOB_TYPES)
    if not supported_set:
        supported_set = set(CORE_WORKER_APP_SAFE_JOB_TYPES)
    path = _core_worker_app_jobs_path()
    with _core_worker_app_jobs_lock:
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        results = data.get("results") if isinstance(data.get("results"), list) else []
        running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
        history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
        stats = data.get("statsByInstallId") if isinstance(data.get("statsByInstallId"), dict) else {}
        last_fetch = data.get("lastFetchByInstallId") if isinstance(data.get("lastFetchByInstallId"), dict) else {}
        auto_ping = data.get("lastAutoPingByInstallId") if isinstance(data.get("lastAutoPingByInstallId"), dict) else {}

        # Recoloca jobs expirados na fila, com retry limitado. Se estourar, registra timeout claro.
        refreshed_running: dict = {}
        for job_id, job in list(running.items()):
            if not isinstance(job, dict):
                continue
            deadline = int(job.get("deadlineAt") or 0)
            if deadline and deadline < now:
                attempts = int(job.get("attempt") or 0)
                if attempts <= _core_worker_app_job_max_retries(job):
                    job["status"] = "pending"
                    job["requeuedAt"] = now
                    pending.append(job)
                else:
                    results.append(_core_worker_app_make_timeout_record(job, now))
                continue
            refreshed_running[str(job_id)] = job
        running = refreshed_running

        deliver: list[dict] = []
        remaining: list[dict] = []
        delivered_keys: set[str] = set()
        for job in pending:
            if not isinstance(job, dict):
                continue
            job_type = _core_worker_app_normalize_job_type(job.get("type"))
            if job_type not in CORE_WORKER_APP_SAFE_JOB_TYPES or job_type not in supported_set:
                remaining.append(job)
                continue
            if not _core_worker_app_job_matches(job, install_id, worker_id):
                remaining.append(job)
                continue
            job_id = _safe_short_text(job.get("id") or f"job-{uuid.uuid4().hex[:12]}", 64)
            if job_id in running or job_id in delivered_keys:
                remaining.append(job)
                continue
            if len(deliver) >= CORE_WORKER_APP_JOB_MAX_DELIVER:
                remaining.append(job)
                continue
            out = dict(job)
            out["id"] = job_id
            out["type"] = job_type
            out["jobClass"] = _core_worker_app_job_class(job_type)
            out["issuedAt"] = int(out.get("issuedAt") or now)
            out["status"] = "running"
            out["claimedAt"] = now
            out["deadlineAt"] = now + _core_worker_app_job_timeout(out)
            out["attempt"] = int(out.get("attempt") or 0) + 1
            if install_id and not out.get("installId"):
                out["installId"] = install_id
            if worker_id and not out.get("workerId"):
                out["workerId"] = worker_id
            running[job_id] = out
            deliver.append(out)
            delivered_keys.add(job_id)

        auto_intervals = {
            "apk_ping": int(os.getenv("CORE_WORKER_APP_AUTO_PING_INTERVAL_SECONDS", "900") or "900"),
            "apk_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_DIAGNOSTIC_INTERVAL_SECONDS", "1800") or "1800"),
            "apk_check_update": int(os.getenv("CORE_WORKER_APP_AUTO_UPDATE_CHECK_INTERVAL_SECONDS", "2700") or "2700"),
            "apk_upload_app_logs": int(os.getenv("CORE_WORKER_APP_AUTO_REPORT_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_runtime_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_RUNTIME_DIAGNOSTIC_INTERVAL_SECONDS", "2100") or "2100"),
            "apk_worker_bridge_status": int(os.getenv("CORE_WORKER_APP_AUTO_BRIDGE_INTERVAL_SECONDS", "2100") or "2100"),
            "apk_storage_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_STORAGE_DIAGNOSTIC_INTERVAL_SECONDS", "5400") or "5400"),
            "apk_collect_status_bundle": int(os.getenv("CORE_WORKER_APP_AUTO_STATUS_BUNDLE_INTERVAL_SECONDS", "7200") or "7200"),
            "apk_device_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_DEVICE_DIAGNOSTIC_INTERVAL_SECONDS", "2400") or "2400"),
            "apk_network_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_NETWORK_DIAGNOSTIC_INTERVAL_SECONDS", "1800") or "1800"),
            "apk_push_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_PUSH_DIAGNOSTIC_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_update_diagnostic": int(os.getenv("CORE_WORKER_APP_AUTO_UPDATE_DIAGNOSTIC_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_job_history": int(os.getenv("CORE_WORKER_APP_AUTO_JOB_HISTORY_INTERVAL_SECONDS", "3600") or "3600"),
            "apk_cache_cleanup": int(os.getenv("CORE_WORKER_APP_AUTO_CACHE_CLEANUP_INTERVAL_SECONDS", "21600") or "21600"),
        }

        def _maybe_auto_job(kind: str, interval: int, title: str, reason: str) -> None:
            kind = _core_worker_app_normalize_job_type(kind)
            if len(deliver) >= CORE_WORKER_APP_JOB_MAX_DELIVER or interval <= 0 or kind not in supported_set:
                return
            # Não cria outro job igual se já existe rodando/pendente para esta instalação/worker.
            for existing in list(running.values()) + remaining + deliver:
                if not isinstance(existing, dict):
                    continue
                if _core_worker_app_normalize_job_type(existing.get("type")) == kind and _core_worker_app_job_matches(existing, install_id, worker_id):
                    return
            auto_key = f"{key}:{kind}"
            try:
                last = int(auto_ping.get(auto_key) or 0)
            except Exception:
                last = 0
            if now - last >= interval:
                job_id = f"auto-{kind.replace('_', '-')}-{key[:16]}-{now}"
                job = {"id": job_id, "type": kind, "jobClass": "automatic", "reason": reason, "issuedAt": now, "title": title, "status": "running", "claimedAt": now, "deadlineAt": now + CORE_WORKER_APP_JOB_DEFAULT_TIMEOUT_SECONDS, "attempt": 1, "installId": install_id, "workerId": worker_id}
                running[job_id] = job
                deliver.append(job)
                auto_ping[auto_key] = now

        for kind in sorted(CORE_WORKER_APP_AUTO_JOB_TYPES):
            interval = int(auto_intervals.get(kind) or 0)
            title = CORE_WORKER_APP_JOB_LABELS.get(kind, kind)
            _maybe_auto_job(kind, interval, title, "auto-" + kind.replace("apk_", "").replace("_", "-"))

        queue_stats = stats.get(key) if isinstance(stats.get(key), dict) else {}
        queue_stats["lastFetchAt"] = now
        queue_stats["jobsReturned"] = len(deliver)
        queue_stats["pending"] = len(remaining)
        queue_stats["running"] = len(running)
        stats[key] = queue_stats
        last_fetch[key] = {"at": now, "installId": install_id, "workerId": worker_id, "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48), "jobsReturned": len(deliver), "pending": len(remaining), "running": len(running)}
        data["pending"] = remaining[-120:]
        data["runningByJobId"] = running
        data["results"] = results[-CORE_WORKER_APP_JOB_RESULT_LIMIT:]
        data["historyByInstallId"] = history
        data["statsByInstallId"] = stats
        data["lastFetchByInstallId"] = last_fetch
        data["lastAutoPingByInstallId"] = auto_ping
        data["updatedAt"] = now
        data["ok"] = True
        data["jobCatalog"] = _core_worker_app_job_catalog()
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
        _atomic_write_json(path, data, mode=0o600)
    public_jobs = [_core_worker_app_public_job(job, now) for job in deliver]
    return {"ok": True, "jobs": public_jobs, "count": len(public_jobs), "jobsRuntime": "apk-safe-internal-queue", "queue": {"pending": len(remaining), "running": len(running)}, "supported": sorted(CORE_WORKER_APP_SAFE_JOB_TYPES), "catalog": _core_worker_app_job_catalog()}


def _core_worker_app_jobs_result(payload: dict) -> dict:
    now = int(time.time())
    if not isinstance(payload, dict):
        payload = {}
    job_id = _safe_short_text(payload.get("jobId") or payload.get("job_id"), 64)
    job_type = _core_worker_app_normalize_job_type(payload.get("type"))
    if not job_id:
        raise ValueError("jobId ausente")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    record = {"receivedAt": now, "jobId": job_id, "type": job_type, "jobClass": _core_worker_app_job_class(job_type), "installId": _safe_short_text(payload.get("installId") or payload.get("install_id"), 80), "workerId": _safe_short_text(payload.get("workerId") or payload.get("worker_id"), 80), "appVersion": _safe_short_text(payload.get("appVersion") or payload.get("app_version"), 48), "appVersionCode": int(payload.get("appVersionCode") or payload.get("app_version_code") or 0), "ok": bool(result.get("ok")), "message": _safe_short_text(result.get("message") or result.get("summary"), 160), "error": _safe_short_text(result.get("error"), 160), "result": result}
    path = _core_worker_app_jobs_path()
    with _core_worker_app_jobs_lock:
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        results = data.get("results") if isinstance(data.get("results"), list) else []
        latest = data.get("latestResultByInstallId") if isinstance(data.get("latestResultByInstallId"), dict) else {}
        running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
        history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
        stats = data.get("statsByInstallId") if isinstance(data.get("statsByInstallId"), dict) else {}
        key = record["installId"] or record["workerId"] or "unknown"
        running.pop(job_id, None)
        results.append(record)
        hist = history.get(key) if isinstance(history.get(key), list) else []
        hist.append({"at": now, "jobId": job_id, "type": job_type, "ok": bool(record.get("ok")), "message": _safe_short_text(record.get("message") or record.get("error"), 120)})
        history[key] = hist[-24:]
        queue_stats = stats.get(key) if isinstance(stats.get(key), dict) else {}
        queue_stats["lastResultAt"] = now
        queue_stats["lastType"] = job_type
        queue_stats["lastOk"] = bool(record.get("ok"))
        queue_stats["lastMessage"] = _safe_short_text(record.get("message") or record.get("error"), 120)
        if bool(record.get("ok")):
            queue_stats["okCount"] = int(queue_stats.get("okCount") or 0) + 1
        else:
            queue_stats["failCount"] = int(queue_stats.get("failCount") or 0) + 1
        stats[key] = queue_stats
        data["results"] = results[-CORE_WORKER_APP_JOB_RESULT_LIMIT:]
        latest[key] = record
        data["latestResultByInstallId"] = latest
        data["runningByJobId"] = running
        data["historyByInstallId"] = history
        data["statsByInstallId"] = stats
        data["updatedAt"] = now
        data["ok"] = True
        data["jobCatalog"] = _core_worker_app_job_catalog()
        data["summaryByInstallId"] = _core_worker_app_jobs_build_summaries(data, now)
        _atomic_write_json(path, data, mode=0o600)
    return record


def _core_worker_app_jobs_public_summary(worker_id: str = "", install_id: str = "") -> dict:
    path = _core_worker_app_jobs_path()
    try:
        data = json.loads(open(path, "r", encoding="utf-8").read()) if os.path.isfile(path) else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    latest = data.get("latestResultByInstallId") if isinstance(data.get("latestResultByInstallId"), dict) else {}
    stats = data.get("statsByInstallId") if isinstance(data.get("statsByInstallId"), dict) else {}
    history = data.get("historyByInstallId") if isinstance(data.get("historyByInstallId"), dict) else {}
    record = None
    if install_id:
        record = latest.get(install_id)
    if not isinstance(record, dict) and isinstance(data.get("results"), list):
        for item in reversed(data.get("results") or []):
            if not isinstance(item, dict):
                continue
            if worker_id and str(item.get("workerId") or "") == worker_id:
                record = item
                break
    pending = data.get("pending") if isinstance(data.get("pending"), list) else []
    running = data.get("runningByJobId") if isinstance(data.get("runningByJobId"), dict) else {}
    pending_count = len(pending)
    running_count = len(running)
    key = install_id or worker_id or "unknown"
    stat = stats.get(key) if isinstance(stats.get(key), dict) else {}
    hist = history.get(key) if isinstance(history.get(key), list) else []
    return {"pending": pending_count, "running": running_count, "lastResultAt": int((record or {}).get("receivedAt") or 0), "lastType": _safe_short_text((record or {}).get("type"), 48), "lastOk": bool((record or {}).get("ok")) if isinstance(record, dict) else False, "lastMessage": _safe_short_text((record or {}).get("message") or (record or {}).get("error"), 120), "okCount": int((stat or {}).get("okCount") or 0), "failCount": int((stat or {}).get("failCount") or 0), "history": hist[-5:]}


def _firebase_service_account_path() -> str:
    return _expand_path(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "/home/ubuntu/secrets/firebase-service-account.json")


def _firebase_access_token_and_project() -> tuple[str, str]:
    path = _firebase_service_account_path()
    if not os.path.isfile(path):
        raise RuntimeError(f"service account Firebase ausente em {path}")
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except Exception as exc:
        raise RuntimeError(f"dependência google-auth indisponível: {type(exc).__name__}") from exc
    scopes = ["https://www.googleapis.com/auth/firebase.messaging"]
    credentials = service_account.Credentials.from_service_account_file(path, scopes=scopes)
    credentials.refresh(GoogleAuthRequest())
    project_id = getattr(credentials, "project_id", "") or ""
    if not project_id:
        with open(path, "r", encoding="utf-8") as fh:
            project_id = str((json.load(fh) or {}).get("project_id") or "")
    if not project_id:
        raise RuntimeError("project_id ausente na service account Firebase")
    return str(credentials.token or ""), project_id


def _fcm_send_v1(token: str, data_payload: dict) -> tuple[bool, str]:
    access_token, project_id = _firebase_access_token_and_project()
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    clean_data = {str(k): str(v) for k, v in (data_payload or {}).items() if v is not None}
    body = json.dumps({
        "message": {
            "token": token,
            "android": {"priority": "HIGH"},
            "data": clean_data,
        }
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            response_body = resp.read(8192).decode("utf-8", errors="replace")
            return True, response_body[:500]
    except urllib.error.HTTPError as exc:
        response_body = exc.read(8192).decode("utf-8", errors="replace") if exc.fp else ""
        return False, f"HTTP {exc.code}: {response_body[:500]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"


def _send_core_worker_apk_update_pushes(manifest: dict, *, reason: str = "apk_published") -> dict:
    if not isinstance(manifest, dict):
        return {"ok": False, "error": "manifest inválido"}
    notification_id = _safe_short_text(manifest.get("notificationId") or _notification_event_id(version_name=str(manifest.get("versionName") or ""), version_code=int(manifest.get("versionCode") or 0), sha256=str(manifest.get("sha256") or "")), 96)
    version_name = _safe_short_text(manifest.get("versionName"), 48)
    version_code = int(manifest.get("versionCode") or 0)
    data_payload = {
        "type": "apk_update_available",
        "source": "core-worker-vps",
        "reason": reason,
        "notificationId": notification_id,
        "versionName": version_name,
        "versionCode": str(version_code),
        "apkUrl": str(manifest.get("apkUrl") or ""),
        "downloadUrl": str(manifest.get("downloadUrl") or manifest.get("directApkUrl") or ""),
        "sha256": str(manifest.get("sha256") or ""),
    }
    records = _active_core_worker_fcm_records()
    sent = 0
    failed = 0
    now = int(time.time())
    store = _load_core_worker_fcm_tokens()
    tokens = store.get("tokens") if isinstance(store.get("tokens"), dict) else {}
    for record in records:
        token = str(record.get("token") or "").strip()
        token_hash = str(record.get("tokenHash") or _fcm_token_hash(token))
        ok, detail = _fcm_send_v1(token, data_payload)
        current = tokens.get(token_hash) if isinstance(tokens.get(token_hash), dict) else record
        unregistered = (not ok) and _fcm_error_is_unregistered(detail)
        current["lastPushAt"] = now
        current["lastNotificationId"] = notification_id
        if ok:
            current["lastPushStatus"] = "sent"
            current["lastError"] = ""
            current["lastErrorCode"] = ""
            current["refreshRequired"] = False
            current["active"] = True
        elif unregistered:
            current["lastPushStatus"] = "unregistered"
            current["lastErrorCode"] = "UNREGISTERED"
            current["lastError"] = _fcm_unregistered_public_error()
            current["refreshRequired"] = True
            current["active"] = False
            current["invalidatedAt"] = now
        else:
            current["lastPushStatus"] = "failed"
            current["lastError"] = _safe_short_text(detail, 180)
        tokens[token_hash] = current
        event_detail = "push FCM enviado pela VPS" if ok else (_fcm_unregistered_public_error() if unregistered else detail)
        event = {
            "notificationId": notification_id,
            "state": "fcm_sent" if ok else ("fcm_token_unregistered" if unregistered else "fcm_failed"),
            "delivered": False,
            "versionName": version_name,
            "versionCode": version_code,
            "appVersion": _safe_short_text(current.get("appVersion"), 48),
            "appVersionCode": int(current.get("appVersionCode") or 0),
            "workerId": _safe_short_text(current.get("workerId"), 80),
            "installId": _safe_short_text(current.get("installId"), 80),
            "permission": _safe_short_text(current.get("permission"), 40),
            "detail": event_detail,
        }
        with contextlib.suppress(Exception):
            with app.test_request_context(headers={"X-Internal-Core-Worker": "fcm"}):
                # _append_core_worker_notification_event usa request.remote_addr; contexto interno evita depender de uma requisição real.
                _append_core_worker_notification_event(event)
        sent += 1 if ok else 0
        failed += 0 if ok else 1
    store["tokens"] = tokens
    store["updatedAt"] = now
    store["lastBroadcast"] = {"notificationId": notification_id, "sent": sent, "failed": failed, "reason": reason, "at": now}
    _save_core_worker_fcm_tokens(store)
    return {"ok": failed == 0, "tokens": len(records), "sent": sent, "failed": failed, "notificationId": notification_id}


def _kick_core_worker_fcm_push(manifest: dict, *, reason: str = "apk_published") -> None:
    if not isinstance(manifest, dict) or not manifest.get("notificationRequested"):
        return
    def runner() -> None:
        try:
            _send_core_worker_apk_update_pushes(manifest, reason=reason)
        except Exception as exc:
            with contextlib.suppress(Exception):
                with app.test_request_context(headers={"X-Internal-Core-Worker": "fcm"}):
                    _append_core_worker_notification_event({
                        "notificationId": manifest.get("notificationId") or "fcm",
                        "state": "fcm_failed",
                        "delivered": False,
                        "versionName": manifest.get("versionName"),
                        "versionCode": manifest.get("versionCode") or 0,
                        "detail": f"falha geral enviando FCM: {type(exc).__name__}: {str(exc)[:180]}",
                    })
    try:
        threading.Thread(target=runner, name="core-worker-fcm-push", daemon=True).start()
    except Exception:
        pass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "y", "on", "sim"}


def _expand_path(value: str | None) -> str:
    return os.path.abspath(os.path.expanduser(str(value or "")))


def _find_android_build_tool(tool: str) -> str | None:
    explicit = os.getenv(f"CORE_WORKER_APK_{tool.upper()}")
    if explicit and os.path.isfile(_expand_path(explicit)):
        return _expand_path(explicit)
    found = shutil.which(tool)
    if found:
        return found
    roots = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.getenv(env_name)
        if root:
            roots.append(_expand_path(root))
    roots.extend([
        _expand_path("~/android-sdk"),
        _expand_path("~/Android/Sdk"),
        "/opt/android-sdk",
        "/usr/lib/android-sdk",
    ])
    candidates: list[str] = []
    for root in dict.fromkeys(roots):
        build_tools = os.path.join(root, "build-tools")
        if not os.path.isdir(build_tools):
            continue
        for version in sorted(os.listdir(build_tools), reverse=True):
            candidates.append(os.path.join(build_tools, version, tool))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _strip_apk_signatures(source: str, target: str) -> None:
    """Recria o APK removendo assinaturas antigas.

    Usado apenas se a VPS for configurada explicitamente para assinatura fixa
    via CORE_WORKER_APK_SIGNING_MODE. No fluxo padrão, a VPS aceita o APK debug
    já assinado pelo phone worker builder e não precisa de Android SDK.
    """
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = str(info.filename or "")
            upper = name.upper()
            if upper.startswith("META-INF/") and (
                upper.endswith(".RSA") or upper.endswith(".DSA") or upper.endswith(".EC") or upper.endswith(".SF") or upper == "META-INF/MANIFEST.MF"
            ):
                continue
            data = zin.read(info.filename)
            info.date_time = info.date_time or (1980, 1, 1, 0, 0, 0)
            zout.writestr(info, data)


def _fixed_apk_signing_config() -> dict[str, str] | None:
    # A VPS Oracle de 1 GB não deve depender de Android SDK/apksigner para o
    # fluxo normal. O phone worker builder gera o APK debug já assinado pelo
    # Gradle; a VPS só valida, publica e notifica. Assinatura fixa na VPS fica
    # opcional para um futuro release build via CORE_WORKER_APK_SIGNING_MODE.
    raw_mode = os.getenv("CORE_WORKER_APK_SIGNING_MODE")
    if _env_bool("CORE_WORKER_APK_SIGNING_DISABLED", False):
        return None
    if raw_mode is None or not str(raw_mode).strip():
        return None
    mode = str(raw_mode).strip().lower()
    if mode in {"off", "none", "disabled", "false", "0", "worker", "uploaded", "phone-worker", "debug", "release"}:
        # Patch 57: mesmo se sobrou CORE_WORKER_APK_SIGNING_MODE=debug no .env,
        # a VPS não deve tentar assinar. O phone worker assina com a keystore
        # compatível recebida pelo payload temporário.
        return None
    if mode == "vps-debug":
        keystore = _expand_path(os.getenv("CORE_WORKER_APK_KEYSTORE") or "~/.android/debug.keystore")
        return {
            "mode": "vps-debug",
            "keystore": keystore,
            "alias": os.getenv("CORE_WORKER_APK_KEY_ALIAS") or "androiddebugkey",
            "storepass": os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "android",
            "keypass": os.getenv("CORE_WORKER_APK_KEY_PASSWORD") or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "android",
        }
    keystore = _expand_path(os.getenv("CORE_WORKER_APK_KEYSTORE") or "")
    return {
        "mode": mode or "keystore",
        "keystore": keystore,
        "alias": os.getenv("CORE_WORKER_APK_KEY_ALIAS") or "",
        "storepass": os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "",
        "keypass": os.getenv("CORE_WORKER_APK_KEY_PASSWORD") or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "",
    }


def _validate_core_worker_apk(apk_path: str) -> dict[str, object]:
    """Valida o APK antes de publicar latest.json.

    A validação é intencionalmente local e barata: ZIP íntegro, manifest/classes
    presentes, assinatura verificável quando apksigner existe e alinhamento quando
    zipalign existe. Se falhar, a VPS não deve apontar latest.json para esse APK.
    """
    result: dict[str, object] = {"ok": False, "checks": []}
    path = str(apk_path or "")
    if not path or not os.path.isfile(path):
        result["error"] = "APK não encontrado"
        return result
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad:
                result["error"] = f"ZIP corrompido em {bad}"
                return result
            names = set(zf.namelist())
            missing = [name for name in ("AndroidManifest.xml", "classes.dex") if name not in names]
            if missing:
                result["error"] = "APK sem " + ", ".join(missing)
                return result
            result["checks"].append("zip")
            result["checks"].append("manifest")
            result["entries"] = len(names)
    except Exception as exc:
        result["error"] = f"ZIP inválido: {type(exc).__name__}: {exc}"
        return result

    zipalign = _find_android_build_tool("zipalign")
    if zipalign:
        proc = subprocess.run([zipalign, "-c", "-p", "4", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["zipalign"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = "zipalign -c falhou: " + (proc.stderr or proc.stdout or "sem saída")[-400:]
            return result
        result["checks"].append("zipalign")

    apksigner = _find_android_build_tool("apksigner")
    if apksigner:
        proc = subprocess.run([apksigner, "verify", "--verbose", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["apksigner"] = proc.returncode == 0
        result["apksigner_tail"] = (proc.stdout or proc.stderr or "")[-800:]
        if proc.returncode != 0:
            result["error"] = "apksigner verify falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:]
            return result
        result["checks"].append("apksigner")

    aapt = _find_android_build_tool("aapt")
    if aapt:
        proc = subprocess.run([aapt, "dump", "badging", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["aapt"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = "aapt dump badging falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:]
            return result
        result["checks"].append("aapt")
        first_line = next((line for line in (proc.stdout or "").splitlines() if line.startswith("package:")), "")
        if first_line:
            result["badging"] = first_line[:500]

    result["ok"] = True
    return result


def _sign_core_worker_apk_with_vps_key(uploaded_apk: str, final_apk: str) -> dict[str, object]:
    cfg = _fixed_apk_signing_config()
    if cfg is None:
        shutil.copyfile(uploaded_apk, final_apk)
        return {"signedByVps": False, "signingMode": "phone-worker-debug"}
    apksigner = _find_android_build_tool("apksigner")
    if not apksigner:
        raise RuntimeError("apksigner não encontrado na VPS; configure ANDROID_HOME ou CORE_WORKER_APK_APKSIGNER")
    zipalign = _find_android_build_tool("zipalign")
    keystore = str(cfg.get("keystore") or "")
    alias = str(cfg.get("alias") or "")
    storepass = str(cfg.get("storepass") or "")
    keypass = str(cfg.get("keypass") or "")
    if not keystore or not os.path.isfile(keystore):
        raise RuntimeError("keystore fixa ausente na VPS; configure CORE_WORKER_APK_KEYSTORE ou preserve ~/.android/debug.keystore")
    if not alias or not storepass:
        raise RuntimeError("configuração de assinatura incompleta na VPS")

    base_dir = os.path.dirname(final_apk) or os.getcwd()
    with tempfile.TemporaryDirectory(prefix="core-worker-sign-", dir=base_dir) as tmpdir:
        stripped = os.path.join(tmpdir, "stripped.apk")
        aligned = os.path.join(tmpdir, "aligned.apk")
        _strip_apk_signatures(uploaded_apk, stripped)
        sign_input = stripped
        if zipalign:
            proc = subprocess.run([zipalign, "-f", "-p", "4", stripped, aligned], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError("zipalign falhou: " + (proc.stderr or proc.stdout or "sem saída")[-400:])
            sign_input = aligned
        cmd = [
            apksigner,
            "sign",
            "--ks", keystore,
            "--ks-key-alias", alias,
            "--ks-pass", f"pass:{storepass}",
            "--key-pass", f"pass:{keypass}",
            "--out", final_apk,
            sign_input,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError("apksigner falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:])
        verify = subprocess.run([apksigner, "verify", "--verbose", final_apk], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if verify.returncode != 0:
            raise RuntimeError("verificação da assinatura falhou: " + (verify.stderr or verify.stdout or "sem saída")[-500:])
    return {
        "signedByVps": True,
        "signingMode": str(cfg.get("mode") or "keystore"),
        "zipalign": bool(zipalign),
    }

def set_health_provider(provider):
    global _health_provider
    _health_provider = provider


def _purge_expired_tts_audio(now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _tts_audio_lock:
        expired = [token for token, (_path, expires_at) in _tts_audio_files.items() if expires_at <= now]
        for token in expired:
            _tts_audio_files.pop(token, None)


def register_tts_audio_file(path: str, *, ttl_seconds: float = 240.0) -> str | None:
    """Registra um áudio temporário para o Lavalink buscar via HTTP.

    O token é aleatório e expira rápido. O arquivo não é copiado para evitar RAM/IO
    extra; o endpoint apenas faz streaming do caminho já gerado pelo TTS. A URL
    pode usar a extensão real do arquivo (.ogg/.opus/.m4a/.mp3) ou apenas o token.
    """
    try:
        abs_path = os.path.abspath(str(path or ""))
        if not os.path.isfile(abs_path):
            return None
        _purge_expired_tts_audio()
        token = uuid.uuid4().hex
        ttl = max(30.0, min(900.0, float(ttl_seconds or 240.0)))
        with _tts_audio_lock:
            _tts_audio_files[token] = (abs_path, time.time() + ttl)
        return token
    except Exception:
        return None


@app.get("/")
def index():
    return "ok", 200


@app.get("/health")
def health():
    if callable(_health_provider):
        try:
            return jsonify(_health_provider()), 200
        except Exception as e:
            return jsonify({
                "ok": False,
                "healthy": False,
                "error": str(e),
            }), 500
    return jsonify({"ok": True}), 200



@app.post("/core-worker/app/publish")
def core_worker_app_publish():
    """Recebe APK compilado por um worker builder e publica latest.json.

    Apenas workers pareados com role/capability apk-builder podem publicar.
    O APK é salvo em CORE_WORKER_APK_DIR e o manifest latest.json é refeito.
    """
    from utility.commands.workers_registry import core_worker_authenticate_http

    form = request.form.to_dict(flat=True)
    status, auth_body = core_worker_authenticate_http(request.headers, {"worker_id": form.get("worker_id") or request.headers.get("X-Core-Worker-ID") or ""}, remote_addr=request.remote_addr or "")
    if status != 200:
        return jsonify(auth_body), status
    worker = auth_body.get("worker") if isinstance(auth_body.get("worker"), dict) else {}
    roles = set(str(item) for item in (worker.get("roles") or []))
    capabilities = set(str(item) for item in (worker.get("capabilities") or [])) | roles
    if "apk-builder" not in capabilities:
        return jsonify({"ok": False, "error": "worker não tem função apk-builder"}), 403

    upload = request.files.get("apk")
    if upload is None:
        return jsonify({"ok": False, "error": "arquivo apk ausente"}), 400
    version_name = str(form.get("versionName") or form.get("version") or "0.0.0").strip()[:48]
    try:
        version_code = int(str(form.get("versionCode") or 0).strip() or 0)
    except Exception:
        version_code = 0
    filename = _safe_release_filename(form.get("filename") or upload.filename or f"CoreWorker-v{version_name}-debug.apk")
    if not filename.lower().endswith(".apk"):
        return jsonify({"ok": False, "error": "arquivo precisa terminar com .apk"}), 400
    base = _core_worker_apk_dir()
    os.makedirs(base, exist_ok=True)
    target = os.path.abspath(os.path.join(base, filename))
    if target != base and not target.startswith(base + os.sep):
        return jsonify({"ok": False, "error": "nome de arquivo inválido"}), 400
    tmp = target + ".upload.tmp"
    expected_sha = str(form.get("sha256") or "").strip().lower()
    digest = hashlib.sha256()
    upload_total = 0
    max_bytes = int(os.getenv("CORE_WORKER_APK_UPLOAD_MAX_BYTES", str(220 * 1024 * 1024)))
    signing_info: dict[str, object] = {"signedByVps": False, "signingMode": "not-run"}
    try:
        with open(tmp, "wb") as fh:
            while True:
                chunk = upload.stream.read(128 * 1024)
                if not chunk:
                    break
                upload_total += len(chunk)
                if upload_total > max_bytes:
                    raise ValueError("APK grande demais")
                digest.update(chunk)
                fh.write(chunk)
        upload_sha = digest.hexdigest()
        if expected_sha and expected_sha != upload_sha:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            return jsonify({"ok": False, "error": "sha256 divergente", "expected": expected_sha, "actual": upload_sha}), 400
        try:
            signing_info = _sign_core_worker_apk_with_vps_key(tmp, target)
        except Exception as sign_exc:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            with contextlib.suppress(Exception):
                os.remove(target)
            return jsonify({
                "ok": False,
                "error": "falha assinando APK na VPS",
                "detail": str(sign_exc)[:500],
                "hint": "assinatura na VPS é opcional; deixe CORE_WORKER_APK_SIGNING_MODE vazio/disabled para aceitar o APK debug já assinado pelo phone worker",
            }), 500
        with contextlib.suppress(Exception):
            os.remove(tmp)
    except Exception as exc:
        with contextlib.suppress(Exception):
            os.remove(tmp)
        return jsonify({"ok": False, "error": f"falha salvando APK: {type(exc).__name__}"}), 500

    validation = _validate_core_worker_apk(target)
    if not bool(validation.get("ok")):
        with contextlib.suppress(Exception):
            os.remove(target)
        return jsonify({
            "ok": False,
            "error": "APK recebido, mas falhou na validação antes da publicação",
            "validation": validation,
            "hint": "latest.json foi preservado; corrija o build no phone worker antes de publicar.",
        }), 500

    final_raw = open(target, "rb").read()
    actual_sha = hashlib.sha256(final_raw).hexdigest()
    total = len(final_raw)

    changelog = _json_field(form.get("changelog") or "", ["APK compilado por worker builder"])
    if not isinstance(changelog, list):
        changelog = [str(changelog)[:160]]
    required_agent = str(form.get("requiredAgentVersion") or "").strip()[:48]
    source_sha = str(form.get("sourceFingerprint") or form.get("source_fingerprint") or form.get("sourceSha256") or form.get("source_sha256") or "").strip().lower()[:96]
    notification_id = _safe_short_text(form.get("notificationId") or _notification_event_id(version_name=version_name, version_code=version_code, sha256=actual_sha), 96)
    notification_summary = _latest_core_worker_notification_summary(notification_id)
    apk_url = _core_worker_apk_url(filename)
    manifest = {
        "ok": True,
        "versionName": version_name,
        "versionCode": version_code,
        "apkUrl": apk_url,
        "downloadUrl": _external_core_worker_url(apk_url),
        "directApkUrl": _external_core_worker_url(apk_url),
        "sha256": actual_sha,
        "uploadedSha256": upload_sha,
        "requiredAgentVersion": required_agent,
        "updateAvailable": True,
        "notifyUsers": True,
        "notificationRequested": True,
        "notificationId": notification_id,
        "notificationStatus": notification_summary,
        "sourceSha256": source_sha,
        "signedByVps": bool(signing_info.get("signedByVps")),
        "signingMode": _safe_short_text(form.get("apkSigningMode") or signing_info.get("signingMode") or "phone-worker-signed", 96),
        "signingKeystoreSha256": _safe_short_text(form.get("apkSigningKeystoreSha256") or "", 64),
        "changelog": [str(item)[:180] for item in changelog[:8]],
        "publishedByWorker": str(worker.get("name") or worker.get("worker_id") or "worker builder")[:80],
        "publishedAt": int(time.time()),
        "publishReason": "worker-builder-auto" if str(form.get("notifyUsers") or form.get("notificationRequested") or "").strip().lower() in {"1", "true", "yes", "on", "sim"} else "worker-builder",
        "bytes": total,
        "uploadedBytes": upload_total,
        "validation": validation,
    }
    manifest_path = os.path.join(base, "latest.json")
    tmp_manifest = manifest_path + ".tmp"
    with open(tmp_manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_manifest, manifest_path)
    _kick_core_worker_fcm_push(manifest, reason="apk_published")
    _kick_core_worker_pending_automation(str(worker.get("worker_id") or ""))
    return jsonify({"ok": True, "filename": filename, "bytes": total, "sha256": actual_sha, "signedByVps": bool(signing_info.get("signedByVps")), "signingMode": manifest.get("signingMode"), "validation": validation, "latest": manifest}), 200



@app.post("/core-worker/app/fcm-token")
def core_worker_app_fcm_token():
    """Registra token FCM do APK privado.

    O token é segredo operacional do dispositivo e fica só em data/ local com
    permissão 600. O painel mostra apenas resumo como Push: ativo.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        record = _register_core_worker_fcm_token(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:180]}), 400
    except Exception as exc:
        app.logger.warning("core-worker FCM token ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "push": record}), 200


@app.get("/core-worker/app/fcm-summary")
def core_worker_app_fcm_summary():
    worker_id = str(request.args.get("worker_id") or "").strip()
    return jsonify({"ok": True, "summary": _core_worker_fcm_public_summary(worker_id)}), 200


@app.post("/core-worker/app/heartbeat")
def core_worker_app_heartbeat():
    """Recebe heartbeat direto do APK, sem passar pelo Termux.

    Este endpoint é apenas telemetria de migração: não autentica jobs e não
    concede capacidade de execução. Jobs reais continuam no phone-worker/Termux.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        record = _append_core_worker_app_heartbeat(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:180]}), 400
    except Exception as exc:
        app.logger.warning("core-worker app heartbeat ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "runtime": record}), 200


@app.get("/core-worker/app/runtime-summary")
def core_worker_app_runtime_summary():
    worker_id = str(request.args.get("worker_id") or "").strip()
    install_id = str(request.args.get("install_id") or "").strip()
    return jsonify({"ok": True, "summary": _core_worker_app_runtime_public_summary(worker_id, install_id)}), 200


@app.post("/core-worker/app/jobs/fetch")
def core_worker_app_jobs_fetch():
    """Entrega jobs leves para o runtime interno do APK.

    Apenas jobs sem shell/comando são aceitos pelo APK nesta etapa. Jobs reais
    continuam no Termux. Este endpoint é best-effort e não concede execução arbitrária.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        return jsonify(_core_worker_app_jobs_fetch(payload)), 200
    except Exception as exc:
        app.logger.warning("core-worker app light jobs fetch ignored: %s", exc)
        return jsonify({"ok": False, "jobs": [], "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200


@app.post("/core-worker/app/jobs/result")
def core_worker_app_jobs_result():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        record = _core_worker_app_jobs_result(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)[:180]}), 400
    except Exception as exc:
        app.logger.warning("core-worker app light job result ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "result": record}), 200


@app.post("/core-worker/app/notification")
def core_worker_app_notification():
    """Recebe confirmação best-effort do APK privado sobre notificação local.

    O app ainda é privado e fica atrás da rede da VPS/Tailscale. Este endpoint
    armazena apenas telemetria curta; tokens FCM entram em /fcm-token.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    state = _safe_short_text(payload.get("state") or payload.get("event"), 48)
    if not state:
        return jsonify({"ok": False, "error": "state ausente"}), 400
    try:
        event = _append_core_worker_notification_event(payload)
    except Exception as exc:
        app.logger.warning("core-worker app notification ignored: %s", exc)
        return jsonify({"ok": False, "accepted": True, "error": _safe_short_text(f"{type(exc).__name__}: {exc}", 180)}), 200
    return jsonify({"ok": True, "event": event}), 200


@app.get("/core-worker/app/latest.json")
@app.get("/core-worker/latest.json")
@app.get("/core-worker/latest")
def core_worker_app_latest():
    """Manifesto privado de atualização do Core Worker APK.

    Publique um arquivo latest.json em CORE_WORKER_APK_DIR contendo versionCode,
    versionName, apkUrl e sha256. Este endpoint não usa segredos e deve ser usado
    preferencialmente apenas pela rede privada/Tailscale.
    """
    base = _core_worker_apk_dir()
    manifest = os.path.join(base, "latest.json")
    if not os.path.isfile(manifest):
        return jsonify({
            "ok": False,
            "error": "Core Worker APK ainda não publicado na VPS.",
            "expected": manifest,
            "hint": "Crie latest.json e coloque o APK no diretório de releases.",
        }), 404
    try:
        data = json.loads(open(manifest, "r", encoding="utf-8").read())
        if isinstance(data, dict):
            apk_url = str(data.get("apkUrl") or data.get("url") or "").strip()
            if apk_url:
                data["downloadUrl"] = _external_core_worker_url(apk_url)
                data["directApkUrl"] = data["downloadUrl"]
            nid = str(data.get("notificationId") or "")
            if nid:
                data["notificationStatus"] = _latest_core_worker_notification_summary(nid)
            data["pushStatus"] = _core_worker_fcm_public_summary()
            return jsonify(data), 200
    except Exception:
        pass
    return send_file(manifest, mimetype="application/json", conditional=True, max_age=0)


@app.get("/core-worker/app/<path:filename>")
def core_worker_app_file(filename: str):
    """Serve APKs privados do Core Worker a partir do diretório de releases."""
    full = _safe_core_worker_apk_file(filename)
    if not full or not os.path.isfile(full):
        abort(404)
    lowered = full.lower()
    if lowered.endswith(".apk"):
        return send_file(full, mimetype="application/vnd.android.package-archive", as_attachment=True, download_name=os.path.basename(full), conditional=True, max_age=0)
    if lowered.endswith(".json"):
        return send_file(full, mimetype="application/json", conditional=True, max_age=0)
    if lowered.endswith(".zip"):
        return send_file(full, mimetype="application/zip", conditional=True, max_age=0)
    return send_file(full, mimetype="text/plain", conditional=True, max_age=0)




def _kick_core_worker_pending_automation(worker_id: str = "") -> None:
    """Agenda processamento leve das pendências de agent/APK após heartbeat.

    Não bloqueia o /heartbeat: se um worker antigo voltar online depois do update
    da VPS, o script tenta entregar worker_update/apk_build pendentes em segundo
    plano. A VPS continua sendo a fonte de decisão; o worker só executa jobs
    whitelist.
    """
    worker_id = str(worker_id or "").strip()
    script = os.path.join(os.getcwd(), "scripts", "core-worker-automation.py")
    if not os.path.isfile(script):
        return
    py = os.path.join(os.getcwd(), ".venv", "bin", "python")
    if not os.path.isfile(py):
        py = shutil.which("python3") or "python3"
    cmd = [py, script, "process-pending"]
    if worker_id:
        cmd.extend(["--worker-id", worker_id])
    try:
        subprocess.Popen(cmd, cwd=os.getcwd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


@app.post("/core-worker/pair")
def core_worker_pair():
    from utility.commands.workers_registry import redeem_core_worker_pairing_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = redeem_core_worker_pairing_http(payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/heartbeat")
def core_worker_heartbeat():
    from utility.commands.workers_registry import core_worker_heartbeat_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_heartbeat_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
        worker_id = str(worker.get("worker_id") or payload.get("worker_id") or payload.get("id") or "")
        _kick_core_worker_pending_automation(worker_id)
    return jsonify(body), status




@app.post("/core-worker/jobs/poll")
def core_worker_jobs_poll():
    from utility.commands.workers_registry import core_worker_poll_job_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_poll_job_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        worker_id = str(payload.get("worker_id") or payload.get("id") or "")
        if worker_id:
            _kick_core_worker_pending_automation(worker_id)
    return jsonify(body), status


@app.post("/core-worker/jobs/result")
def core_worker_jobs_result():
    from utility.commands.workers_registry import core_worker_job_result_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_job_result_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        worker_id = str(payload.get("worker_id") or payload.get("id") or "")
        _kick_core_worker_pending_automation(worker_id)
    return jsonify(body), status


@app.get("/tts-audio/<token>")
@app.get("/tts-audio/<token>.<ext>")
def tts_audio(token: str, ext: str | None = None):
    token = str(token or "").strip()
    # Compatibilidade com rotas antigas onde o sufixo vinha incorporado no token.
    for suffix in (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav"):
        if token.lower().endswith(suffix):
            token = token[: -len(suffix)]
            break
    if not token:
        abort(404)
    now = time.time()
    with _tts_audio_lock:
        record = _tts_audio_files.get(token)
        if not record:
            abort(404)
        path, expires_at = record
        if expires_at <= now:
            _tts_audio_files.pop(token, None)
            abort(404)
    if not os.path.isfile(path):
        with _tts_audio_lock:
            _tts_audio_files.pop(token, None)
        abort(404)
    lowered = path.lower()
    if lowered.endswith((".ogg", ".opus")):
        mimetype = "audio/ogg"
    elif lowered.endswith((".m4a", ".aac")):
        mimetype = "audio/mp4"
    elif lowered.endswith(".wav"):
        mimetype = "audio/wav"
    else:
        mimetype = "audio/mpeg"
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)


def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print(f"[webserver] usando porta {port}")
    serve(app, host="0.0.0.0", port=port, threads=4)
