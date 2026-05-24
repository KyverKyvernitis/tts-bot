from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Mapping


REGISTRY_VERSION = 1
DEFAULT_PAIRING_TTL_SECONDS = 300
DEFAULT_OFFLINE_AFTER_SECONDS = 90
DEFAULT_MAX_WORKERS = 24
DEFAULT_JOB_TTL_SECONDS = 900
DEFAULT_JOB_LEASE_SECONDS = 120
DEFAULT_JOB_HISTORY_LIMIT = 8
DEFAULT_JOB_PAYLOAD_MAX_STRING = 256 * 1024

CORE_WORKER_JOB_TYPES = {
    "ping",
    "status",
    "diagnostic_basic",
    "worker_self_check",
    "worker_logs",
    "network_probe",
    "tailscale_status",
    "service_status",
    "service_start",
    "service_stop",
    "service_restart",
    "ffmpeg_check",
    "ffprobe_check",
    "zip_validate",
    "log_summary",
    "text_stats",
    "maintenance_plan",
    "worker_update",
    "apk_build_debug",
    "apk_publish_last",
    "vps_assist_probe",
    "hash_batch",
    "endpoint_probe",
    "media_probe",
    "audio_convert",
    "log_digest",
    "zip_audit",
    "boot_status",
    "boot_repair",
}

_ROLE_RE = re.compile(r"[^a-z0-9_.:-]+")
_CODE_RE = re.compile(r"[^A-Z0-9]+")


class CoreWorkerRegistryError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = int(status)


def _repo_root() -> Path:
    # utility/commands/workers_registry.py -> repo root
    return Path(__file__).resolve().parents[2]


def _registry_path() -> Path:
    raw = str(os.getenv("CORE_WORKERS_REGISTRY_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "data" / "core_workers_registry.json"


def _now() -> float:
    return time.time()


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def normalize_pairing_code(value: object) -> str:
    text = str(value or "").strip().upper()
    compact = _CODE_RE.sub("", text)
    if compact.startswith("CORE") and len(compact) > 4:
        compact = compact[4:]
    compact = compact[:12]
    if not compact:
        return ""
    return f"CORE-{compact}"


def _short_text(value: object, *, limit: int = 80, default: str = "") -> str:
    text = str(value or default or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[:limit].rstrip()
    return text


def _safe_worker_id(value: object | None = None) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9_.:-]+", "-", raw).strip("-._:")
    if raw and 3 <= len(raw) <= 64:
        return raw
    return "cw-" + secrets.token_hex(8)


def normalize_roles(value: object, *, default: list[str] | None = None, limit: int = 16) -> list[str]:
    raw_items: list[object]
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = re.split(r"[,;\s]+", str(value or ""))

    roles: list[str] = []
    for item in raw_items:
        role = str(item or "").strip().lower().replace("_", "-")
        role = _ROLE_RE.sub("-", role).strip("-._:")
        if not role:
            continue
        if role not in roles:
            roles.append(role[:32])
        if len(roles) >= limit:
            break
    if not roles and default:
        for role in default:
            if role and role not in roles:
                roles.append(role)
            if len(roles) >= limit:
                break
    return roles




def normalize_job_types(value: object, *, default: list[str] | None = None, limit: int = 64) -> list[str]:
    """Normaliza tipos de jobs/tasks preservando underscore.

    `normalize_roles()` troca `_` por `-`, o que é correto para roles, mas
    quebra tasks como `service_status`. Esta função aceita tanto
    `service-status` quanto `service_status` e devolve sempre underscore.
    """
    raw_items: list[object]
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = re.split(r"[,;\s]+", str(value or ""))

    tasks: list[str] = []
    for item in raw_items:
        task = str(item or "").strip().lower().replace("-", "_")
        task = re.sub(r"[^a-z0-9_]+", "_", task).strip("_")
        if not task:
            continue
        if task not in tasks:
            tasks.append(task[:48])
        if len(tasks) >= limit:
            break
    if not tasks and default:
        for task in normalize_job_types(default, limit=limit):
            if task not in tasks:
                tasks.append(task)
            if len(tasks) >= limit:
                break
    return tasks


def _merge_unique(base: list[str], extra: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    for item in list(base or []) + list(extra or []):
        clean = str(item or "").strip()
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= limit:
            break
    return result


def _job_type_set(value: object) -> set[str]:
    return set(normalize_job_types(value, limit=96))


def _normalize_job_type(value: object) -> str:
    items = normalize_job_types([value], limit=1)
    return items[0] if items else ""


def _worker_status_dict(worker: dict[str, Any]) -> dict[str, Any]:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    clean = dict(status)
    worker["status"] = clean
    return clean


def _merge_worker_status(worker: dict[str, Any], incoming: object) -> None:
    """Atualiza status sem apagar subestado local mantido pelo registry.

    O heartbeat do worker pode carregar subárvores grandes. O registry precisa
    guardar só o suficiente para seleção/diagnóstico; payload bruto grande deve
    ficar nos logs/arquivos do worker, não em data/core_workers_registry.json.
    """
    if not isinstance(incoming, Mapping):
        return
    current = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    merged = dict(current)
    for key, value in _safe_dict(incoming, max_items=18, max_string=1024).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged.get(key) or {})
            nested.update(value)
            merged[key] = _safe_dict(nested, max_items=18, max_string=1024)
        else:
            merged[key] = value
    worker["status"] = _safe_dict(merged, max_items=18, max_string=1024)


def _safe_dict(value: object, *, max_items: int = 32, max_string: int = 8192) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key, item in list(value.items())[:max_items]:
        k = _short_text(key, limit=48)
        if not k:
            continue
        if isinstance(item, str):
            clean[k] = item if len(item) <= max_string else item[:max_string] + "…[truncated]"
        elif isinstance(item, (int, float, bool)) or item is None:
            clean[k] = item
        elif isinstance(item, list):
            clean_list: list[Any] = []
            for x in item[:24]:
                if isinstance(x, Mapping):
                    clean_list.append(_safe_dict(x, max_items=16, max_string=max_string))
                elif isinstance(x, (str, int, float, bool)) or x is None:
                    clean_list.append(x if not isinstance(x, str) or len(x) <= max_string else x[:max_string] + "…[truncated]")
            clean[k] = clean_list
        elif isinstance(item, Mapping):
            clean[k] = _safe_dict(item, max_items=12, max_string=max_string)
        else:
            clean[k] = _short_text(item, limit=120)
    return clean




def _safe_job_type(value: object) -> str:
    job_type = str(value or "").strip().lower().replace("-", "_")
    job_type = re.sub(r"[^a-z0-9_]+", "_", job_type).strip("_")
    if job_type not in CORE_WORKER_JOB_TYPES:
        raise CoreWorkerRegistryError("tipo de job não permitido", status=400)
    return job_type


def _compact_job_public(record: Mapping[str, Any], *, include_result: bool = False, now: float | None = None) -> dict[str, Any]:
    ts = _now() if now is None else float(now)
    created_at = float(record.get("created_at") or 0.0)
    public = {
        "job_id": str(record.get("job_id") or ""),
        "type": _short_text(record.get("type"), limit=40),
        "status": _short_text(record.get("status"), limit=24, default="queued"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "age_seconds": round(max(0.0, ts - created_at), 3) if created_at else None,
        "worker_id": _short_text(record.get("worker_id"), limit=64),
        "target_worker_id": _short_text(record.get("target_worker_id"), limit=64),
        "attempts": int(record.get("attempts") or 0),
        "max_attempts": int(record.get("max_attempts") or 1),
        "expires_at": record.get("expires_at"),
        "lease_until": record.get("lease_until"),
        "summary": _short_text(record.get("summary"), limit=160),
        "error": _short_text(record.get("error"), limit=180),
    }
    if include_result:
        public["result"] = _safe_dict(record.get("result"), max_items=32)
    return public

def _compact_worker_public(record: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    ts = _now() if now is None else float(now)
    offline_after = max(15, _env_int("CORE_WORKER_OFFLINE_AFTER_SECONDS", DEFAULT_OFFLINE_AFTER_SECONDS))
    last_seen = float(record.get("last_heartbeat_at") or record.get("updated_at") or 0.0)
    age = max(0.0, ts - last_seen) if last_seen else None
    enabled = bool(record.get("enabled", True))
    online = enabled and age is not None and age <= offline_after
    roles = _merge_unique(normalize_roles(record.get("roles"), limit=16), normalize_roles(record.get("manual_roles"), limit=16), limit=16)
    capabilities = _merge_unique(normalize_roles(record.get("capabilities"), limit=24), normalize_roles(record.get("manual_capabilities"), limit=24), limit=24)
    supported_tasks = _merge_unique(normalize_job_types(record.get("supported_tasks"), limit=64), normalize_job_types(record.get("manual_supported_tasks"), limit=64), limit=64)
    status = record.get("status") if isinstance(record.get("status"), Mapping) else {}
    runtime = status.get("runtime") if isinstance(status.get("runtime"), Mapping) else {}
    runtime_mode = _short_text(record.get("runtime_mode") or status.get("runtime_mode") or runtime.get("mode") or "", limit=32)
    if not runtime_mode:
        source_hint = str(record.get("source") or "").lower()
        runtime_mode = "termux" if "termux" in source_hint else "unknown"
    public = {
        "worker_id": str(record.get("worker_id") or ""),
        "name": _short_text(record.get("name"), limit=64, default="Core Worker"),
        "enabled": enabled,
        "online": online,
        "last_seen_age_seconds": round(age, 3) if age is not None else None,
        "registered_at": record.get("registered_at"),
        "last_heartbeat_at": record.get("last_heartbeat_at"),
        "roles": roles,
        "capabilities": capabilities,
        "supported_tasks": supported_tasks,
        "version": _short_text(record.get("version"), limit=48),
        "source": _short_text(record.get("source"), limit=32, default="apk"),
        "runtime_mode": runtime_mode,
        "runtime": _safe_dict(runtime, max_items=16),
        "endpoint": _short_text(record.get("endpoint"), limit=160),
        "battery": _safe_dict(record.get("battery"), max_items=12, max_string=512),
        "network": _safe_dict(record.get("network"), max_items=12, max_string=512),
        "health": _safe_dict(record.get("health"), max_items=16, max_string=1024),
        "status": _safe_dict(record.get("status"), max_items=18, max_string=1024),
        "remote_addr": _short_text(record.get("remote_addr"), limit=64),
    }
    return public


def _public_worker_ping_ms(worker: Mapping[str, Any]) -> float | None:
    network = worker.get("network") if isinstance(worker.get("network"), Mapping) else {}
    for key in ("vps_ping_ms", "ping_ms", "latency_ms", "vps_latency_ms"):
        value = network.get(key)
        if value is None:
            continue
        try:
            ms = float(value)
            if 0 <= ms < 60000:
                return ms
        except Exception:
            continue
    return None


def _public_worker_battery_level(worker: Mapping[str, Any]) -> float | None:
    battery = worker.get("battery") if isinstance(worker.get("battery"), Mapping) else {}
    for key in ("level", "percent", "percentage"):
        value = battery.get(key)
        if value is None:
            continue
        try:
            pct = float(value)
            if 0 <= pct <= 100:
                return pct
        except Exception:
            continue
    return None


def _public_worker_sort_key(worker: Mapping[str, Any]) -> tuple[Any, ...]:
    """Ordena workers para o painel e para preferências do failover.

    Online vem primeiro; entre online, preferir menor ping e bateria maior.
    Campos ausentes ficam no fim sem impedir uso do worker.
    """
    online = bool(worker.get("online"))
    enabled = bool(worker.get("enabled", True))
    ping = _public_worker_ping_ms(worker)
    battery = _public_worker_battery_level(worker)
    name = str(worker.get("name") or worker.get("worker_id") or "").casefold()
    return (
        0 if enabled else 1,
        0 if online else 1,
        ping if ping is not None else 999999.0,
        -(battery if battery is not None else -1.0),
        name,
    )


def _sanitize_finished_job_for_storage(job: dict[str, Any]) -> None:
    """Remove payloads grandes de jobs finalizados antes de salvar o registry.

    Jobs de APK/update podem carregar keystore/config/source metadata no payload.
    Manter isso em dezenas de jobs finalizados fez o registry crescer para dezenas
    de MB e travar health/painéis em VPS pequena. O worker já recebeu o payload;
    para histórico basta summary/error/result compacto.
    """
    status = str(job.get("status") or "queued").strip().lower()
    if status in {"queued", "running"}:
        return
    if job.get("payload"):
        job["payload_dropped_after_finish"] = True
        job["payload"] = {}
    if isinstance(job.get("result"), Mapping):
        job["result"] = _safe_dict(job.get("result"), max_items=24, max_string=2048)


def _sanitize_registry_for_storage(data: dict[str, Any]) -> None:
    workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
    for worker in workers.values():
        if not isinstance(worker, dict):
            continue
        for key, max_items in (("battery", 12), ("network", 12), ("health", 16), ("status", 18)):
            if isinstance(worker.get(key), Mapping):
                worker[key] = _safe_dict(worker.get(key), max_items=max_items, max_string=1024)
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    for job in jobs.values():
        if isinstance(job, dict):
            _sanitize_finished_job_for_storage(job)


class CoreWorkersRegistry:
    """Registro leve dos Core Workers.

    Armazena somente hash de pairing codes e tokens. O token real é entregue uma
    única vez ao APK/agent no pareamento e nunca volta a ser escrito em disco.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or _registry_path()
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {"version": REGISTRY_VERSION, "pairings": {}, "workers": {}, "jobs": {}}

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("version", REGISTRY_VERSION)
        if not isinstance(data.get("pairings"), dict):
            data["pairings"] = {}
        if not isinstance(data.get("workers"), dict):
            data["workers"] = {}
        if not isinstance(data.get("jobs"), dict):
            data["jobs"] = {}
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        _sanitize_registry_for_storage(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def _cleanup_pairings_unlocked(self, data: dict[str, Any], *, now: float | None = None) -> int:
        ts = _now() if now is None else float(now)
        pairings = data.get("pairings") if isinstance(data.get("pairings"), dict) else {}
        expired = [pid for pid, record in pairings.items() if float(record.get("expires_at") or 0.0) <= ts]
        for pid in expired:
            pairings.pop(pid, None)
        return len(expired)

    def create_pairing(self, *, created_by_id: int, created_by_name: str = "", ttl_seconds: int | None = None) -> dict[str, Any]:
        ttl = int(ttl_seconds or _env_int("CORE_WORKER_PAIRING_TTL_SECONDS", DEFAULT_PAIRING_TTL_SECONDS))
        ttl = max(60, min(1800, ttl))
        ts = _now()
        # 8 hex chars: fácil de digitar e com entropia suficiente para um código efêmero.
        code = normalize_pairing_code(secrets.token_hex(4).upper())
        pair_id = "pair-" + secrets.token_hex(8)
        with self._lock:
            data = self._load_unlocked()
            self._cleanup_pairings_unlocked(data, now=ts)
            data["pairings"][pair_id] = {
                "pairing_id": pair_id,
                "code_hash": _hash_secret(code),
                "created_at": ts,
                "expires_at": ts + ttl,
                "created_by_id": int(created_by_id or 0),
                "created_by_name": _short_text(created_by_name, limit=80),
            }
            self._save_unlocked(data)
        return {
            "pairing_id": pair_id,
            "code": code,
            "created_at": ts,
            "expires_at": ts + ttl,
            "ttl_seconds": ttl,
        }

    def redeem_pairing(self, payload: Mapping[str, Any], *, remote_addr: str = "") -> dict[str, Any]:
        code = normalize_pairing_code(payload.get("code"))
        if not code:
            raise CoreWorkerRegistryError("código de pareamento ausente", status=400)
        code_hash = _hash_secret(code)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            self._cleanup_pairings_unlocked(data, now=ts)
            pairings = data.get("pairings") if isinstance(data.get("pairings"), dict) else {}
            match_id = ""
            match = None
            for pair_id, record in pairings.items():
                if not isinstance(record, Mapping):
                    continue
                if record.get("code_hash") == code_hash:
                    match_id = str(pair_id)
                    match = record
                    break
            if not match:
                raise CoreWorkerRegistryError("código inválido ou expirado", status=403)

            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            max_workers = max(1, _env_int("CORE_WORKER_MAX_WORKERS", DEFAULT_MAX_WORKERS))
            requested_id = payload.get("worker_id") or payload.get("device_id")
            worker_id = _safe_worker_id(requested_id)
            if worker_id not in workers and len(workers) >= max_workers:
                raise CoreWorkerRegistryError("limite de workers atingido", status=409)

            token = "cw_" + secrets.token_urlsafe(32)
            name = _short_text(payload.get("name") or payload.get("device_name"), limit=64, default="Core Worker")
            roles = normalize_roles(payload.get("roles"), default=["worker", "diagnostics"], limit=16)
            capabilities = normalize_roles(payload.get("capabilities"), default=roles, limit=24)
            endpoint = _short_text(payload.get("endpoint") or payload.get("base_url") or payload.get("url"), limit=160)
            version = _short_text(payload.get("version"), limit=48)
            source = _short_text(payload.get("source"), limit=32, default="apk")

            record = {
                "worker_id": worker_id,
                "name": name,
                "enabled": True,
                "token_hash": _hash_secret(token),
                "registered_at": ts,
                "updated_at": ts,
                "last_heartbeat_at": ts,
                "paired_by_id": int(match.get("created_by_id") or 0),
                "paired_by_name": _short_text(match.get("created_by_name"), limit=80),
                "roles": roles,
                "capabilities": capabilities,
                "supported_tasks": normalize_job_types(payload.get("supported_tasks"), limit=64),
                "endpoint": endpoint,
                "version": version,
                "source": source,
                "remote_addr": _short_text(remote_addr, limit=64),
                "battery": _safe_dict(payload.get("battery"), max_items=16),
                "network": _safe_dict(payload.get("network"), max_items=16),
                "health": _safe_dict(payload.get("health"), max_items=24),
                "status": _safe_dict(payload.get("status"), max_items=24),
            }
            workers[worker_id] = record
            data["workers"] = workers
            pairings.pop(match_id, None)
            self._save_unlocked(data)

        public = _compact_worker_public(record, now=ts)
        return {
            "ok": True,
            "worker_id": worker_id,
            "token": token,
            "worker": public,
            "message": "pareado; salve este token localmente no APK/agent, ele não será mostrado de novo",
        }

    def ensure_direct_worker(self, payload: Mapping[str, Any], *, token: str, remote_addr: str = "") -> dict[str, Any]:
        """Registra/renova um phone-worker direto confiável.

        Esse caminho cobre o modo legado/direto usado pelo painel da VPS. Ele não
        substitui o pareamento normal do APK: só aceita tokens explicitamente
        configurados na VPS e usa o worker_id estável enviado pelo agent.
        """
        if not _is_trusted_direct_worker_token(token, remote_addr=remote_addr):
            raise CoreWorkerRegistryError("worker não encontrado", status=404)
        worker_id = _safe_worker_id(payload.get("worker_id") or payload.get("id"))
        if not worker_id:
            raise CoreWorkerRegistryError("worker_id ausente", status=400)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            existing = workers.get(worker_id)
            if isinstance(existing, dict):
                old_hash = str(existing.get("token_hash") or "")
                if old_hash and old_hash != _hash_secret(token) and not bool(existing.get("direct", False)):
                    raise CoreWorkerRegistryError("worker_id já pertence a outro worker", status=403)
                record = dict(existing)
            else:
                max_workers = max(1, _env_int("CORE_WORKER_MAX_WORKERS", DEFAULT_MAX_WORKERS))
                if len(workers) >= max_workers:
                    raise CoreWorkerRegistryError("limite de workers atingido", status=409)
                record = {
                    "worker_id": worker_id,
                    "registered_at": ts,
                    "paired_by_id": 0,
                    "paired_by_name": "VPS direct",
                    "manual_roles": ["phone-worker", "apk-builder"],
                    "manual_capabilities": ["phone-worker", "apk-builder"],
                }
            record.update({
                "worker_id": worker_id,
                "enabled": True,
                "token_hash": _hash_secret(token),
                "updated_at": ts,
                "last_heartbeat_at": ts,
                "remote_addr": _short_text(remote_addr, limit=64),
                "direct": True,
                "source": _short_text(payload.get("source") or "phone-worker-direct", limit=32),
                "name": _short_text(payload.get("name") or record.get("name") or "Core Phone Worker", limit=64),
            })
            if payload.get("endpoint") or payload.get("base_url") or payload.get("url"):
                record["endpoint"] = _short_text(payload.get("endpoint") or payload.get("base_url") or payload.get("url"), limit=160)
            if payload.get("version"):
                record["version"] = _short_text(payload.get("version"), limit=48)
            roles = normalize_roles(payload.get("roles"), default=normalize_roles(record.get("roles"), default=["phone-worker", "diagnostics", "apk-builder"]), limit=16)
            capabilities = normalize_roles(payload.get("capabilities"), default=normalize_roles(record.get("capabilities"), default=roles), limit=24)
            tasks = normalize_job_types(payload.get("supported_tasks"), default=normalize_job_types(record.get("supported_tasks")), limit=64)
            record["roles"] = _merge_unique(roles, normalize_roles(record.get("manual_roles"), limit=16), limit=16)
            record["capabilities"] = _merge_unique(capabilities, normalize_roles(record.get("manual_capabilities"), limit=24), limit=24)
            if tasks:
                record["supported_tasks"] = tasks
            for key, max_items in (("battery", 16), ("network", 16), ("health", 24), ("status", 24)):
                if key not in payload:
                    continue
                if key == "status":
                    _merge_worker_status(record, payload.get(key))
                else:
                    record[key] = _safe_dict(payload.get(key), max_items=max_items)
            workers[worker_id] = record
            data["workers"] = workers
            self._reconcile_jobs_from_worker_status_unlocked(data, now=ts)
            self._save_unlocked(data)
            public = _compact_worker_public(record, now=ts)
        return {"ok": True, "worker_id": worker_id, "worker": public, "auto_registered": True}


    def heartbeat(self, payload: Mapping[str, Any], *, token: str, remote_addr: str = "") -> dict[str, Any]:
        worker_id = _safe_worker_id(payload.get("worker_id") or payload.get("id"))
        if not token:
            raise CoreWorkerRegistryError("token ausente", status=401)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            worker_id, record = self._authenticate_worker_unlocked(data, worker_id=worker_id, token=token)
            record["updated_at"] = ts
            record["last_heartbeat_at"] = ts
            record["remote_addr"] = _short_text(remote_addr, limit=64)
            for key in ("name", "endpoint", "version", "source"):
                if key in payload:
                    record[key] = _short_text(payload.get(key), limit=160 if key == "endpoint" else 64)
            if "roles" in payload:
                record["roles"] = normalize_roles(payload.get("roles"), default=normalize_roles(record.get("roles")), limit=16)
            if "capabilities" in payload:
                record["capabilities"] = normalize_roles(payload.get("capabilities"), default=normalize_roles(record.get("capabilities")), limit=24)
            if "supported_tasks" in payload:
                record["supported_tasks"] = normalize_job_types(payload.get("supported_tasks"), default=normalize_job_types(record.get("supported_tasks")), limit=64)
            for key, max_items in (("battery", 16), ("network", 16), ("health", 24), ("status", 24)):
                if key not in payload:
                    continue
                if key == "status":
                    _merge_worker_status(record, payload.get(key))
                else:
                    record[key] = _safe_dict(payload.get(key), max_items=max_items)
            workers[worker_id] = record
            data["workers"] = workers
            self._reconcile_jobs_from_worker_status_unlocked(data, now=ts)
            self._save_unlocked(data)
            public = _compact_worker_public(record, now=ts)
        return {"ok": True, "worker": public}

    def _reconcile_jobs_from_worker_status_unlocked(self, data: dict[str, Any], *, now: float | None = None) -> int:
        """Fecha jobs ativos quando o worker informa último resultado no status."""
        ts = _now() if now is None else float(now)
        jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
        changed = 0
        for worker_id, worker in workers.items():
            if not isinstance(worker, dict):
                continue
            status = worker.get("status") if isinstance(worker.get("status"), Mapping) else {}
            queue = status.get("core_worker_jobs") if isinstance(status.get("core_worker_jobs"), Mapping) else {}
            if not queue:
                continue
            job_id = _short_text(queue.get("last_result_job_id") or queue.get("last_completed_job_id"), limit=64)
            final_status = str(queue.get("last_result_status") or queue.get("last_completed_status") or "").strip().lower()
            if not job_id or final_status not in {"succeeded", "failed"}:
                continue
            job = jobs.get(job_id)
            if not isinstance(job, dict):
                continue
            current = str(job.get("status") or "queued").strip().lower()
            if current not in {"queued", "running"}:
                continue
            assigned = str(job.get("worker_id") or job.get("target_worker_id") or "")
            if assigned and assigned != str(worker_id):
                continue
            summary = _short_text(queue.get("last_result_summary") or job.get("summary") or final_status, limit=160)
            finished_at = queue.get("last_result_at") or queue.get("last_completed_at") or ts
            job["status"] = final_status
            job["worker_id"] = str(worker_id)
            job["lease_until"] = 0
            job["finished_at"] = finished_at
            job["updated_at"] = ts
            if summary:
                job["summary"] = summary
            if final_status == "failed" and not job.get("error"):
                job["error"] = summary or "worker informou falha"
            if not isinstance(job.get("result"), Mapping) or not job.get("result"):
                job["result"] = {"ok": final_status == "succeeded", "summary": summary, "recovered_from_worker_status": True}
            jobs[job_id] = job
            changed += 1
        data["jobs"] = jobs
        return changed

    def _cleanup_jobs_unlocked(self, data: dict[str, Any], *, now: float | None = None, keep_history: int | None = None) -> dict[str, int]:
        ts = _now() if now is None else float(now)
        keep = max(5, min(50, int(keep_history or _env_int("CORE_WORKER_JOB_HISTORY_LIMIT", DEFAULT_JOB_HISTORY_LIMIT))))
        jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        reconciled = self._reconcile_jobs_from_worker_status_unlocked(data, now=ts)
        jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        expired_running = 0
        expired_queued = 0
        for job in list(jobs.values()):
            if not isinstance(job, dict):
                continue
            status = str(job.get("status") or "queued")
            expires_at = float(job.get("expires_at") or 0.0)
            lease_until = float(job.get("lease_until") or 0.0)
            if status == "running" and lease_until and lease_until <= ts:
                attempts = int(job.get("attempts") or 0)
                max_attempts = int(job.get("max_attempts") or 1)
                if attempts < max_attempts and (not expires_at or expires_at > ts):
                    job["status"] = "queued"
                    job["worker_id"] = ""
                    job["lease_until"] = 0
                    job["updated_at"] = ts
                else:
                    job["status"] = "failed"
                    job["error"] = "lease expirou sem resultado"
                    job["finished_at"] = ts
                    job["updated_at"] = ts
                expired_running += 1
            elif status == "queued" and expires_at and expires_at <= ts:
                job["status"] = "expired"
                job["error"] = "job expirou antes de ser executado"
                job["finished_at"] = ts
                job["updated_at"] = ts
                expired_queued += 1

        ordered = sorted(
            [(jid, job) for jid, job in jobs.items() if isinstance(job, Mapping)],
            key=lambda item: float(item[1].get("updated_at") or item[1].get("created_at") or 0.0),
            reverse=True,
        )
        active_status = {"queued", "running"}
        active = [(jid, job) for jid, job in ordered if str(job.get("status") or "queued") in active_status]
        done = [(jid, job) for jid, job in ordered if str(job.get("status") or "queued") not in active_status]
        for _jid, done_job in done:
            if isinstance(done_job, dict):
                _sanitize_finished_job_for_storage(done_job)
        trimmed = 0
        for jid, _job in done[keep:]:
            jobs.pop(jid, None)
            trimmed += 1
        data["jobs"] = jobs
        return {"expired_running": expired_running, "expired_queued": expired_queued, "trimmed": trimmed, "reconciled": reconciled}

    def create_job(
        self,
        *,
        job_type: str,
        payload: Mapping[str, Any] | None = None,
        created_by_id: int = 0,
        created_by_name: str = "",
        target_worker_id: str = "",
        required_roles: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        ttl_seconds: int | None = None,
        lease_seconds: int | None = None,
        max_attempts: int = 1,
        summary: str = "",
    ) -> dict[str, Any]:
        kind = _safe_job_type(job_type)
        ts = _now()
        ttl = max(30, min(7200, int(ttl_seconds or _env_int("CORE_WORKER_JOB_TTL_SECONDS", DEFAULT_JOB_TTL_SECONDS))))
        lease = max(10, min(7200, int(lease_seconds or _env_int("CORE_WORKER_JOB_LEASE_SECONDS", DEFAULT_JOB_LEASE_SECONDS))))
        job_id = "job-" + secrets.token_hex(8)
        record = {
            "job_id": job_id,
            "type": kind,
            "payload": _safe_dict(payload or {}, max_items=64, max_string=_env_int("CORE_WORKER_JOB_PAYLOAD_MAX_STRING", DEFAULT_JOB_PAYLOAD_MAX_STRING)),
            "status": "queued",
            "created_at": ts,
            "updated_at": ts,
            "expires_at": ts + ttl,
            "lease_seconds": lease,
            "lease_until": 0,
            "created_by_id": int(created_by_id or 0),
            "created_by_name": _short_text(created_by_name, limit=80),
            "target_worker_id": _safe_worker_id(target_worker_id) if target_worker_id else "",
            "worker_id": "",
            "required_roles": normalize_roles(required_roles or [], limit=12),
            "required_capabilities": normalize_roles(required_capabilities or [], limit=12),
            "attempts": 0,
            "max_attempts": max(1, min(5, int(max_attempts or 1))),
            "summary": _short_text(summary or kind, limit=160),
            "result": {},
            "error": "",
        }
        with self._lock:
            data = self._load_unlocked()
            self._cleanup_jobs_unlocked(data, now=ts)
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            if record["target_worker_id"]:
                target = workers.get(record["target_worker_id"])
                if not isinstance(target, Mapping):
                    raise CoreWorkerRegistryError("worker alvo não encontrado", status=404)
                if not _compact_worker_public(target, now=ts).get("online"):
                    raise CoreWorkerRegistryError("worker alvo está offline", status=409)
                if not self._job_matches_worker(record, str(record["target_worker_id"]), target):
                    raise CoreWorkerRegistryError("worker alvo não suporta este job", status=409)
            else:
                compatible_online: list[Mapping[str, Any]] = []
                for wid, worker in workers.items():
                    if not isinstance(worker, Mapping):
                        continue
                    public_worker = _compact_worker_public(worker, now=ts)
                    if not public_worker.get("online"):
                        continue
                    if self._job_matches_worker(record, str(wid), worker):
                        compatible_online.append(public_worker)
                if not compatible_online:
                    raise CoreWorkerRegistryError("nenhum worker online compatível para este job", status=409)
                compatible_online.sort(key=_public_worker_sort_key)
                record["preferred_worker_id"] = str(compatible_online[0].get("worker_id") or "")
            jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
            jobs[job_id] = record
            data["jobs"] = jobs
            self._save_unlocked(data)
        return {"ok": True, "job": _compact_job_public(record, include_result=False, now=ts)}

    def cleanup_jobs(self, *, keep_history: int | None = None, clear_active: bool = False) -> dict[str, Any]:
        with self._lock:
            data = self._load_unlocked()
            counts = self._cleanup_jobs_unlocked(data, now=_now(), keep_history=keep_history)
            removed_active = 0
            if clear_active:
                jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
                for job_id, job in list(jobs.items()):
                    if isinstance(job, Mapping) and str(job.get("status") or "queued") in {"queued", "running"}:
                        jobs.pop(job_id, None)
                        removed_active += 1
                data["jobs"] = jobs
            self._save_unlocked(data)
            total = len(data.get("jobs") if isinstance(data.get("jobs"), dict) else {})
        return {"ok": True, "total_jobs": total, "removed_active": removed_active, **counts}

    def _authenticate_worker_unlocked(self, data: dict[str, Any], *, worker_id: object, token: str) -> tuple[str, dict[str, Any]]:
        safe_id = _safe_worker_id(worker_id)
        if not token:
            raise CoreWorkerRegistryError("token ausente", status=401)
        workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
        record = workers.get(safe_id)
        if not isinstance(record, dict):
            raise CoreWorkerRegistryError("worker não encontrado", status=404)
        if str(record.get("token_hash") or "") != _hash_secret(token):
            raise CoreWorkerRegistryError("token inválido", status=403)
        if not bool(record.get("enabled", True)):
            raise CoreWorkerRegistryError("worker desativado", status=403)
        return safe_id, record

    def _job_matches_worker(self, job: Mapping[str, Any], worker_id: str, worker: Mapping[str, Any]) -> bool:
        target = str(job.get("target_worker_id") or "").strip()
        if target and target != worker_id:
            return False
        roles = set(normalize_roles(worker.get("roles"), limit=32)) | set(normalize_roles(worker.get("manual_roles"), limit=32))
        capabilities = (set(normalize_roles(worker.get("capabilities"), limit=48)) | set(normalize_roles(worker.get("manual_capabilities"), limit=48)) | roles)
        required_roles = set(normalize_roles(job.get("required_roles"), limit=16))
        required_capabilities = set(normalize_roles(job.get("required_capabilities"), limit=16))
        if required_roles and not required_roles.issubset(roles | capabilities):
            return False
        if required_capabilities and not required_capabilities.issubset(capabilities):
            return False
        supported_tasks = _job_type_set(worker.get("supported_tasks")) | _job_type_set(worker.get("manual_supported_tasks"))
        job_type = _normalize_job_type(job.get("type"))
        if supported_tasks and job_type and job_type not in supported_tasks:
            return False
        return True

    def poll_job(self, payload: Mapping[str, Any], *, token: str, remote_addr: str = "") -> dict[str, Any]:
        worker_id_from_payload = payload.get("worker_id") or payload.get("id")
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            self._cleanup_jobs_unlocked(data, now=ts)
            worker_id, worker = self._authenticate_worker_unlocked(data, worker_id=worker_id_from_payload, token=token)
            # Poll também conta como sinal de vida, para o painel não depender só do heartbeat separado.
            worker["updated_at"] = ts
            worker["last_heartbeat_at"] = ts
            worker["remote_addr"] = _short_text(remote_addr, limit=64)
            if "roles" in payload:
                worker["roles"] = normalize_roles(payload.get("roles"), default=normalize_roles(worker.get("roles")), limit=16)
            if "capabilities" in payload:
                worker["capabilities"] = normalize_roles(payload.get("capabilities"), default=normalize_roles(worker.get("capabilities")), limit=24)
            if "supported_tasks" in payload:
                worker["supported_tasks"] = normalize_job_types(payload.get("supported_tasks"), default=normalize_job_types(worker.get("supported_tasks")), limit=64)
            for key, max_items in (("battery", 16), ("network", 16), ("health", 24), ("status", 24)):
                if key not in payload:
                    continue
                if key == "status":
                    _merge_worker_status(worker, payload.get(key))
                else:
                    worker[key] = _safe_dict(payload.get(key), max_items=max_items)

            self._reconcile_jobs_from_worker_status_unlocked(data, now=ts)
            jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
            candidates = sorted(
                [job for job in jobs.values() if isinstance(job, dict) and str(job.get("status") or "queued") == "queued"],
                key=lambda job: float(job.get("created_at") or 0.0),
            )
            selected: dict[str, Any] | None = None
            skipped_reasons: list[str] = []
            for job in candidates:
                expires_at = float(job.get("expires_at") or 0.0)
                if expires_at and expires_at <= ts:
                    skipped_reasons.append(f"{job.get('job_id')}:expirado")
                    continue
                if not self._job_matches_worker(job, worker_id, worker):
                    skipped_reasons.append(f"{job.get('job_id')}:{_normalize_job_type(job.get('type')) or 'job'} incompatível")
                    continue
                selected = job
                break

            status_dict = _worker_status_dict(worker)
            queue_status = status_dict.get("core_worker_jobs") if isinstance(status_dict.get("core_worker_jobs"), dict) else {}
            queue_status.update({
                "last_poll_at": ts,
                "last_poll_queued_seen": len(candidates),
                "last_poll_worker_id": worker_id,
            })

            if selected is not None:
                selected["status"] = "running"
                selected["worker_id"] = worker_id
                selected["attempts"] = int(selected.get("attempts") or 0) + 1
                lease = max(10, min(7200, int(selected.get("lease_seconds") or DEFAULT_JOB_LEASE_SECONDS)))
                selected["lease_until"] = ts + lease
                selected["started_at"] = selected.get("started_at") or ts
                selected["updated_at"] = ts
                jobs[str(selected.get("job_id"))] = selected
                queue_status.update({
                    "last_poll_state": "delivered",
                    "last_job_id": str(selected.get("job_id") or ""),
                    "last_job_type": _normalize_job_type(selected.get("type")),
                    "last_poll_reason": "",
                })
            else:
                queue_status.update({
                    "last_poll_state": "no_job" if not candidates else "no_compatible_job",
                    "last_poll_reason": "; ".join(skipped_reasons[:3]),
                })
            status_dict["core_worker_jobs"] = queue_status
            data["workers"][worker_id] = worker
            data["jobs"] = jobs
            self._save_unlocked(data)

        if selected is None:
            return {"ok": True, "job": None}
        return {
            "ok": True,
            "job": {
                "job_id": str(selected.get("job_id") or ""),
                "type": str(selected.get("type") or ""),
                "payload": _safe_dict(selected.get("payload"), max_items=64, max_string=_env_int("CORE_WORKER_JOB_PAYLOAD_MAX_STRING", DEFAULT_JOB_PAYLOAD_MAX_STRING)),
                "lease_until": selected.get("lease_until"),
                "attempts": int(selected.get("attempts") or 0),
            },
        }

    def submit_job_result(self, payload: Mapping[str, Any], *, token: str, remote_addr: str = "") -> dict[str, Any]:
        worker_id_from_payload = payload.get("worker_id") or payload.get("id")
        job_id = _short_text(payload.get("job_id"), limit=64)
        if not job_id:
            raise CoreWorkerRegistryError("job_id ausente", status=400)
        status = str(payload.get("status") or "succeeded").strip().lower()
        if status not in {"succeeded", "failed"}:
            raise CoreWorkerRegistryError("status de job inválido", status=400)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            worker_id, worker = self._authenticate_worker_unlocked(data, worker_id=worker_id_from_payload, token=token)
            jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
            job = jobs.get(job_id)
            if not isinstance(job, dict):
                # Resultado antigo/local de um job que a VPS já limpou. O worker
                # precisa receber 200 para remover o pending-results local e parar
                # o loop, mas ainda registramos o resumo no status do worker.
                result_summary = _short_text(payload.get("summary") or payload.get("error") or "resultado antigo descartado", limit=160)
                status_dict = _worker_status_dict(worker)
                queue_status = status_dict.get("core_worker_jobs") if isinstance(status_dict.get("core_worker_jobs"), dict) else {}
                queue_status.update({
                    "last_stale_result_at": ts,
                    "last_stale_result_job_id": job_id,
                    "last_stale_result_status": status,
                    "last_stale_result_summary": result_summary,
                    "last_result_at": ts,
                    "last_result_job_id": job_id,
                    "last_result_type": _normalize_job_type((payload.get("result") or {}).get("type") if isinstance(payload.get("result"), Mapping) else ""),
                    "last_result_status": status,
                    "last_result_summary": result_summary,
                    "last_result_stale": True,
                })
                status_dict["core_worker_jobs"] = queue_status
                worker["updated_at"] = ts
                worker["last_heartbeat_at"] = ts
                worker["remote_addr"] = _short_text(remote_addr, limit=64)
                data["workers"][worker_id] = worker
                self._save_unlocked(data)
                return {
                    "ok": True,
                    "accepted": True,
                    "stale": True,
                    "job_missing": True,
                    "summary": "resultado antigo aceito e descartado; job não existe mais no registry",
                    "job": {
                        "job_id": job_id,
                        "status": status,
                        "worker_id": worker_id,
                        "summary": result_summary,
                    },
                }
            assigned = str(job.get("worker_id") or "")
            if assigned and assigned != worker_id:
                raise CoreWorkerRegistryError("job pertence a outro worker", status=403)
            job["status"] = status
            job["updated_at"] = ts
            job["finished_at"] = ts
            job["lease_until"] = 0
            job["worker_id"] = worker_id
            job["result"] = _safe_dict(payload.get("result"), max_items=32, max_string=4096)
            job["payload_dropped_after_finish"] = True
            job["payload"] = {}
            job["error"] = _short_text(payload.get("error"), limit=240)
            submitted_summary = _short_text(payload.get("summary"), limit=160)
            if submitted_summary:
                job["summary"] = submitted_summary
            elif not job.get("summary"):
                job["summary"] = _short_text(payload.get("summary"), limit=160)
            worker["updated_at"] = ts
            worker["last_heartbeat_at"] = ts
            worker["remote_addr"] = _short_text(remote_addr, limit=64)
            status_dict = _worker_status_dict(worker)
            queue_status = status_dict.get("core_worker_jobs") if isinstance(status_dict.get("core_worker_jobs"), dict) else {}
            queue_status.update({
                "last_result_at": ts,
                "last_result_job_id": job_id,
                "last_result_type": _normalize_job_type(job.get("type")),
                "last_result_status": status,
                "last_result_summary": _short_text(job.get("summary") or payload.get("summary") or payload.get("error"), limit=120),
            })
            status_dict["core_worker_jobs"] = queue_status
            data["workers"][worker_id] = worker
            jobs[job_id] = job
            data["jobs"] = jobs
            self._cleanup_jobs_unlocked(data, now=ts)
            self._save_unlocked(data)
            public = _compact_job_public(job, include_result=True, now=ts)
        return {"ok": True, "job": public}

    def get_job(self, job_id: str) -> dict[str, Any]:
        safe_id = _short_text(job_id, limit=64)
        if not safe_id:
            raise CoreWorkerRegistryError("job_id ausente", status=400)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            self._cleanup_jobs_unlocked(data, now=ts)
            jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
            job = jobs.get(safe_id)
            if not isinstance(job, Mapping):
                raise CoreWorkerRegistryError("job não encontrado", status=404)
            public = _compact_job_public(job, include_result=True, now=ts)
        return {"ok": True, "job": public}

    def latest_job_for_worker(self, worker_id: str) -> dict[str, Any]:
        safe_worker_id = _safe_worker_id(worker_id)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            self._cleanup_jobs_unlocked(data, now=ts)
            jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
            candidates = []
            for job in jobs.values():
                if not isinstance(job, Mapping):
                    continue
                assigned = str(job.get("worker_id") or job.get("target_worker_id") or "")
                if safe_worker_id and assigned and assigned != safe_worker_id:
                    continue
                if safe_worker_id and not assigned:
                    continue
                candidates.append(job)
            candidates.sort(key=lambda job: float(job.get("finished_at") or job.get("updated_at") or job.get("created_at") or 0.0), reverse=True)
            if not candidates:
                return {"ok": True, "job": None}
            return {"ok": True, "job": _compact_job_public(candidates[0], include_result=True, now=ts)}

    def authenticate_worker(self, worker_id: str, token: str) -> dict[str, Any]:
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            safe_worker_id, worker = self._authenticate_worker_unlocked(data, worker_id=worker_id, token=token)
            public = _compact_worker_public(worker, now=ts)
        return {"ok": True, "worker_id": safe_worker_id, "worker": public}

    def rename_worker(self, worker_id: str, name: str) -> dict[str, Any]:
        safe_worker_id = _safe_worker_id(worker_id)
        clean_name = _short_text(name, limit=64)
        if not clean_name:
            raise CoreWorkerRegistryError("nome ausente", status=400)
        if len(clean_name) < 2:
            raise CoreWorkerRegistryError("nome curto demais", status=400)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            worker = workers.get(safe_worker_id)
            if not isinstance(worker, dict):
                raise CoreWorkerRegistryError("worker não encontrado", status=404)
            worker["name"] = clean_name
            worker["updated_at"] = ts
            workers[safe_worker_id] = worker
            data["workers"] = workers
            self._save_unlocked(data)
            public = _compact_worker_public(worker, now=ts)
        return {"ok": True, "worker": public}

    def update_worker_roles(self, worker_id: str, roles: object, capabilities: object | None = None, supported_tasks: object | None = None) -> dict[str, Any]:
        safe_worker_id = _safe_worker_id(worker_id)
        new_roles = normalize_roles(roles, limit=16)
        if not new_roles:
            raise CoreWorkerRegistryError("roles ausentes", status=400)
        new_capabilities = normalize_roles(capabilities if capabilities is not None else new_roles, default=new_roles, limit=24)
        manual_tasks = normalize_job_types(supported_tasks, limit=64) if supported_tasks is not None else []
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            worker = workers.get(safe_worker_id)
            if not isinstance(worker, dict):
                raise CoreWorkerRegistryError("worker não encontrado", status=404)
            # Mantemos override manual separado porque o heartbeat do agent pode
            # reenviar o perfil local antigo. O painel deve continuar refletindo
            # a escolha feita no Discord até o usuário/app trocar de perfil.
            worker["manual_roles"] = new_roles
            worker["manual_capabilities"] = new_capabilities
            if manual_tasks:
                worker["manual_supported_tasks"] = manual_tasks
            worker["roles"] = _merge_unique(normalize_roles(worker.get("roles"), limit=16), new_roles, limit=16)
            worker["capabilities"] = _merge_unique(normalize_roles(worker.get("capabilities"), limit=24), new_capabilities, limit=24)
            if manual_tasks:
                worker["supported_tasks"] = _merge_unique(normalize_job_types(worker.get("supported_tasks"), limit=64), manual_tasks, limit=64)
            worker["updated_at"] = ts
            workers[safe_worker_id] = worker
            data["workers"] = workers
            self._save_unlocked(data)
            public = _compact_worker_public(worker, now=ts)
        return {"ok": True, "worker": public}

    def set_worker_enabled(self, worker_id: str, enabled: bool) -> dict[str, Any]:
        safe_worker_id = _safe_worker_id(worker_id)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            worker = workers.get(safe_worker_id)
            if not isinstance(worker, dict):
                raise CoreWorkerRegistryError("worker não encontrado", status=404)
            worker["enabled"] = bool(enabled)
            worker["updated_at"] = ts
            workers[safe_worker_id] = worker
            data["workers"] = workers
            self._save_unlocked(data)
            public = _compact_worker_public(worker, now=ts)
        return {"ok": True, "worker": public}

    def delete_worker(self, worker_id: str, *, only_offline: bool = True) -> dict[str, Any]:
        safe_worker_id = _safe_worker_id(worker_id)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            worker = workers.get(safe_worker_id)
            if not isinstance(worker, dict):
                raise CoreWorkerRegistryError("worker não encontrado", status=404)
            public = _compact_worker_public(worker, now=ts)
            if only_offline and public.get("online"):
                raise CoreWorkerRegistryError("não removo worker online", status=409)
            workers.pop(safe_worker_id, None)
            data["workers"] = workers
            jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
            for job in jobs.values():
                if not isinstance(job, dict):
                    continue
                if str(job.get("target_worker_id") or "") == safe_worker_id and str(job.get("status") or "queued") in {"queued", "running"}:
                    job["target_worker_id"] = ""
                    job["worker_id"] = ""
                    job["status"] = "queued"
                    job["lease_until"] = 0
                    job["updated_at"] = ts
                    job["summary"] = _short_text(f"failover após remover {safe_worker_id}", limit=160)
            data["jobs"] = jobs
            self._save_unlocked(data)
        return {"ok": True, "removed_worker_id": safe_worker_id}

    def snapshot(self, *, lock_timeout_seconds: float | None = None) -> dict[str, Any]:
        ts = _now()
        stale = False
        error = ""
        acquired = False

        if lock_timeout_seconds is None:
            self._lock.acquire()
            acquired = True
        else:
            try:
                acquired = self._lock.acquire(timeout=max(0.0, float(lock_timeout_seconds)))
            except TypeError:
                acquired = self._lock.acquire(False)

        if acquired:
            try:
                data = self._load_unlocked()
                expired = self._cleanup_pairings_unlocked(data, now=ts)
                job_cleanup = self._cleanup_jobs_unlocked(data, now=ts)
                if expired or any(int(v or 0) for v in job_cleanup.values()):
                    self._save_unlocked(data)
            finally:
                self._lock.release()
        else:
            # Painéis e consultas de status não podem travar o event loop esperando
            # heartbeat/jobs. A leitura do JSON é segura sem lock porque salvamos
            # via arquivo temporário + replace atômico; se houver corrida, _load_unlocked
            # já cai para estado vazio em vez de propagar exceção.
            data = self._load_unlocked()
            stale = True
            error = "registry_lock_timeout"

        pairings_raw = data.get("pairings") if isinstance(data.get("pairings"), dict) else {}
        jobs_raw = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
        workers_raw = data.get("workers") if isinstance(data.get("workers"), dict) else {}
        pairings = []
        for record in pairings_raw.values():
            if not isinstance(record, Mapping):
                continue
            expires_at = float(record.get("expires_at") or 0.0)
            pairings.append({
                "pairing_id": str(record.get("pairing_id") or ""),
                "created_at": record.get("created_at"),
                "expires_at": expires_at,
                "ttl_left_seconds": max(0, round(expires_at - ts, 3)),
                "created_by_id": int(record.get("created_by_id") or 0),
                "created_by_name": _short_text(record.get("created_by_name"), limit=80),
            })
        workers = [
            _compact_worker_public(record, now=ts)
            for record in workers_raw.values()
            if isinstance(record, Mapping)
        ]
        jobs = [
            _compact_job_public(record, include_result=False, now=ts)
            for record in jobs_raw.values()
            if isinstance(record, Mapping)
        ]
        workers.sort(key=_public_worker_sort_key)
        jobs.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0.0), reverse=True)
        queued = sum(1 for item in jobs if item.get("status") == "queued")
        running = sum(1 for item in jobs if item.get("status") == "running")
        failed = sum(1 for item in jobs if item.get("status") == "failed")
        succeeded = sum(1 for item in jobs if item.get("status") == "succeeded")
        return {
            "ok": not stale,
            "error": error,
            "stale": stale,
            "path": str(self.path),
            "workers": workers,
            "pairings": sorted(pairings, key=lambda item: float(item.get("expires_at") or 0.0)),
            "jobs": jobs[:12],
            "summary": {
                "registered": len(workers),
                "online": sum(1 for item in workers if item.get("online")),
                "offline": sum(1 for item in workers if not item.get("online")),
                "pairings_active": len(pairings),
                "jobs_total": len(jobs),
                "jobs_queued": queued,
                "jobs_running": running,
                "jobs_failed": failed,
                "jobs_succeeded": succeeded,
            },
        }


_REGISTRY = CoreWorkersRegistry()


def get_core_workers_registry() -> CoreWorkersRegistry:
    return _REGISTRY


def _bearer_token(headers: Mapping[str, Any]) -> str:
    auth = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for key in ("X-Core-Worker-Token", "x-core-worker-token", "X-Phone-Worker-Token", "x-phone-worker-token"):
        value = str(headers.get(key) or "").strip()
        if value:
            return value
    return ""


def _trusted_direct_worker_tokens() -> set[str]:
    tokens: set[str] = set()
    for key in (
        "PHONE_WORKER_TOKEN",
        "CORE_WORKER_TOKEN",
        "CORE_WORKER_DIRECT_TOKEN",
        "CORE_WORKER_DIRECT_WORKER_TOKEN",
    ):
        value = str(os.getenv(key) or "").strip()
        if value:
            tokens.add(value)
    for raw in (os.getenv("CORE_WORKER_DIRECT_WORKER_TOKENS") or "").replace(";", ",").split(","):
        value = raw.strip()
        if value:
            tokens.add(value)
    return tokens


def _normalize_remote_addr(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("::ffff:"):
        text = text.removeprefix("::ffff:")
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    # Evita cortar IPv6 puro. Só remove porta de IPv4/host:porta.
    if text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
        text = text.rsplit(":", 1)[0]
    return text.strip()


def _trusted_direct_worker_hosts() -> set[str]:
    hosts: set[str] = set()
    raw_values = [
        os.getenv("PHONE_WORKER_HOST"),
        os.getenv("CORE_WORKER_PHONE_HOST"),
        os.getenv("PHONE_WORKER_DIRECT_HOST"),
    ]
    raw_values.extend((os.getenv("CORE_WORKER_DIRECT_WORKER_HOSTS") or "").replace(";", ",").split(","))
    for raw in raw_values:
        host = _normalize_remote_addr(raw)
        if not host:
            continue
        hosts.add(host)
        # Quando o usuário configurar hostname, tenta resolver uma vez. Falha de
        # DNS não deve impedir a comparação pelo texto original.
        if not re.match(r"^[0-9a-f:.]+$", host):
            try:
                hosts.add(_normalize_remote_addr(socket.gethostbyname(host)))
            except Exception:
                pass
    return hosts


def _is_trusted_direct_worker_token(token: str, *, remote_addr: str = "") -> bool:
    if not _env_bool("CORE_WORKER_DIRECT_AUTO_REGISTER", True):
        return False
    token = str(token or "").strip()
    if not token:
        return False
    if token in _trusted_direct_worker_tokens():
        return True
    # Ponte de bootstrap: em instalações legadas o phone-worker direto pode ter
    # CORE_WORKER_TOKEN antigo salvo localmente, enquanto a VPS só tem o host do
    # worker direto em PHONE_WORKER_HOST. Permitir auto-registro por host evita
    # que heartbeat/poll/result/publish fiquem presos em 404 e deixa a VPS enviar
    # o próximo worker_update que normaliza o token. Desativável por env.
    if not _env_bool("CORE_WORKER_DIRECT_AUTO_REGISTER_BY_HOST", True):
        return False
    remote = _normalize_remote_addr(remote_addr)
    return bool(remote and remote in _trusted_direct_worker_hosts())


def _retry_after_direct_autoregister(func, headers: Mapping[str, Any], payload: Mapping[str, Any], *, remote_addr: str = "") -> tuple[int, dict[str, Any]]:
    token = _bearer_token(headers)
    try:
        return 200, func(payload, token=token, remote_addr=remote_addr)
    except CoreWorkerRegistryError as exc:
        if exc.status != 404:
            raise
        registry = get_core_workers_registry()
        registry.ensure_direct_worker(payload, token=token, remote_addr=remote_addr)
        return 200, func(payload, token=token, remote_addr=remote_addr)


def core_worker_authenticate_http(headers: Mapping[str, Any], payload: Mapping[str, Any], *, remote_addr: str = "") -> tuple[int, dict[str, Any]]:
    try:
        worker_id = payload.get("worker_id") or payload.get("id")
        token = _bearer_token(headers)
        try:
            result = get_core_workers_registry().authenticate_worker(str(worker_id or ""), token=token)
        except CoreWorkerRegistryError as exc:
            if exc.status != 404:
                raise
            get_core_workers_registry().ensure_direct_worker(payload, token=token, remote_addr=remote_addr)
            result = get_core_workers_registry().authenticate_worker(str(worker_id or ""), token=token)
        return 200, result
    except CoreWorkerRegistryError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"falha interna: {type(exc).__name__}"}


def redeem_core_worker_pairing_http(payload: Mapping[str, Any], *, remote_addr: str = "") -> tuple[int, dict[str, Any]]:
    try:
        result = get_core_workers_registry().redeem_pairing(payload, remote_addr=remote_addr)
        return 200, result
    except CoreWorkerRegistryError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"falha interna: {type(exc).__name__}"}


def core_worker_heartbeat_http(headers: Mapping[str, Any], payload: Mapping[str, Any], *, remote_addr: str = "") -> tuple[int, dict[str, Any]]:
    try:
        status, result = _retry_after_direct_autoregister(get_core_workers_registry().heartbeat, headers, payload, remote_addr=remote_addr)
        return status, result
    except CoreWorkerRegistryError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"falha interna: {type(exc).__name__}"}


def core_worker_poll_job_http(headers: Mapping[str, Any], payload: Mapping[str, Any], *, remote_addr: str = "") -> tuple[int, dict[str, Any]]:
    try:
        status, result = _retry_after_direct_autoregister(get_core_workers_registry().poll_job, headers, payload, remote_addr=remote_addr)
        return status, result
    except CoreWorkerRegistryError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"falha interna: {type(exc).__name__}"}


def core_worker_job_result_http(headers: Mapping[str, Any], payload: Mapping[str, Any], *, remote_addr: str = "") -> tuple[int, dict[str, Any]]:
    try:
        status, result = _retry_after_direct_autoregister(get_core_workers_registry().submit_job_result, headers, payload, remote_addr=remote_addr)
        return status, result
    except CoreWorkerRegistryError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"falha interna: {type(exc).__name__}"}
