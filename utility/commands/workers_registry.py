from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Mapping


REGISTRY_VERSION = 1
DEFAULT_PAIRING_TTL_SECONDS = 300
DEFAULT_OFFLINE_AFTER_SECONDS = 90
DEFAULT_MAX_WORKERS = 24

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


def _safe_dict(value: object, *, max_items: int = 32) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key, item in list(value.items())[:max_items]:
        k = _short_text(key, limit=48)
        if not k:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            clean[k] = item
        elif isinstance(item, list):
            clean[k] = [x for x in item[:24] if isinstance(x, (str, int, float, bool)) or x is None]
        elif isinstance(item, Mapping):
            clean[k] = _safe_dict(item, max_items=12)
        else:
            clean[k] = _short_text(item, limit=120)
    return clean


def _compact_worker_public(record: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    ts = _now() if now is None else float(now)
    offline_after = max(15, _env_int("CORE_WORKER_OFFLINE_AFTER_SECONDS", DEFAULT_OFFLINE_AFTER_SECONDS))
    last_seen = float(record.get("last_heartbeat_at") or record.get("updated_at") or 0.0)
    age = max(0.0, ts - last_seen) if last_seen else None
    enabled = bool(record.get("enabled", True))
    online = enabled and age is not None and age <= offline_after
    public = {
        "worker_id": str(record.get("worker_id") or ""),
        "name": _short_text(record.get("name"), limit=64, default="Core Worker"),
        "enabled": enabled,
        "online": online,
        "last_seen_age_seconds": round(age, 3) if age is not None else None,
        "registered_at": record.get("registered_at"),
        "last_heartbeat_at": record.get("last_heartbeat_at"),
        "roles": normalize_roles(record.get("roles"), limit=16),
        "capabilities": normalize_roles(record.get("capabilities"), limit=24),
        "version": _short_text(record.get("version"), limit=48),
        "source": _short_text(record.get("source"), limit=32, default="apk"),
        "endpoint": _short_text(record.get("endpoint"), limit=160),
        "battery": _safe_dict(record.get("battery"), max_items=16),
        "network": _safe_dict(record.get("network"), max_items=16),
        "health": _safe_dict(record.get("health"), max_items=24),
        "status": _safe_dict(record.get("status"), max_items=24),
        "remote_addr": _short_text(record.get("remote_addr"), limit=64),
    }
    return public


class CoreWorkersRegistry:
    """Registro leve dos Core Workers.

    Armazena somente hash de pairing codes e tokens. O token real é entregue uma
    única vez ao APK/agent no pareamento e nunca volta a ser escrito em disco.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or _registry_path()
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {"version": REGISTRY_VERSION, "pairings": {}, "workers": {}}

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
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
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

    def heartbeat(self, payload: Mapping[str, Any], *, token: str, remote_addr: str = "") -> dict[str, Any]:
        worker_id = _safe_worker_id(payload.get("worker_id") or payload.get("id"))
        if not token:
            raise CoreWorkerRegistryError("token ausente", status=401)
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            record = workers.get(worker_id)
            if not isinstance(record, dict):
                raise CoreWorkerRegistryError("worker não encontrado", status=404)
            if str(record.get("token_hash") or "") != _hash_secret(token):
                raise CoreWorkerRegistryError("token inválido", status=403)
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
            for key, max_items in (("battery", 16), ("network", 16), ("health", 24), ("status", 24)):
                if key in payload:
                    record[key] = _safe_dict(payload.get(key), max_items=max_items)
            workers[worker_id] = record
            data["workers"] = workers
            self._save_unlocked(data)
            public = _compact_worker_public(record, now=ts)
        return {"ok": True, "worker": public}

    def snapshot(self) -> dict[str, Any]:
        ts = _now()
        with self._lock:
            data = self._load_unlocked()
            expired = self._cleanup_pairings_unlocked(data, now=ts)
            if expired:
                self._save_unlocked(data)
            pairings_raw = data.get("pairings") if isinstance(data.get("pairings"), dict) else {}
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
        workers.sort(key=lambda item: (not bool(item.get("online")), str(item.get("name") or "").casefold()))
        return {
            "ok": True,
            "path": str(self.path),
            "workers": workers,
            "pairings": sorted(pairings, key=lambda item: float(item.get("expires_at") or 0.0)),
            "summary": {
                "registered": len(workers),
                "online": sum(1 for item in workers if item.get("online")),
                "offline": sum(1 for item in workers if not item.get("online")),
                "pairings_active": len(pairings),
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
        result = get_core_workers_registry().heartbeat(payload, token=_bearer_token(headers), remote_addr=remote_addr)
        return 200, result
    except CoreWorkerRegistryError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"falha interna: {type(exc).__name__}"}
