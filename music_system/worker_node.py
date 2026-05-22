from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import config

logger = logging.getLogger(__name__)

MUSIC_WORKER_UNAVAILABLE_MESSAGE = str(
    getattr(config, "MUSIC_WORKER_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: Nenhum worker online")
    or "Sistema de música indisponível no momento: Nenhum worker online"
).strip()


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _csv(value: object) -> set[str]:
    import re

    items: set[str] = set()
    for item in re.split(r"[,;\s]+", str(value or "")):
        clean = item.strip().lower().replace("_", "-")
        clean = re.sub(r"[^a-z0-9_.:-]+", "-", clean).strip("-._:")
        if clean:
            items.add(clean)
    return items


def music_worker_only_enabled() -> bool:
    return _as_bool(getattr(config, "MUSIC_WORKER_ONLY_ENABLED", True), True)


class MusicWorkerUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class MusicWorkerSelection:
    available: bool
    worker_id: str = ""
    name: str = ""
    reason: str = ""
    worker: Mapping[str, Any] | None = None

    @property
    def message(self) -> str:
        return MUSIC_WORKER_UNAVAILABLE_MESSAGE


def _load_public_workers() -> list[Mapping[str, Any]]:
    try:
        from utility.commands.workers_registry import CoreWorkersRegistry, _compact_worker_public, _public_worker_sort_key
    except Exception as exc:
        logger.debug("[music/worker] não consegui importar registro de workers", exc_info=True)
        return []

    try:
        registry = CoreWorkersRegistry()
        with registry._lock:  # type: ignore[attr-defined]
            data = registry._load_unlocked()  # type: ignore[attr-defined]
            raw_workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
            now = time.time()
            workers = [
                _compact_worker_public(record, now=now)
                for record in raw_workers.values()
                if isinstance(record, Mapping)
            ]
        workers.sort(key=_public_worker_sort_key)
        return workers
    except Exception:
        logger.debug("[music/worker] falha ao ler registro de workers", exc_info=True)
        return []


def _nested(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    current: Any = mapping or {}
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _worker_profile(worker: Mapping[str, Any]) -> str:
    for value in (
        worker.get("profile"),
        _nested(worker, "status", "profile"),
        _nested(worker, "status", "runtime", "profile"),
        _nested(worker, "health", "profile"),
    ):
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def _worker_is_turbo(worker: Mapping[str, Any], roles_caps: set[str]) -> bool:
    if _worker_profile(worker) == "turbo":
        return True
    if "turbo" in roles_caps or "turbo-worker" in roles_caps:
        return True
    turbo_deps = _nested(worker, "status", "turbo_dependencies")
    if isinstance(turbo_deps, Mapping) and _as_bool(turbo_deps.get("turbo"), False):
        return True
    return False


def _music_node_status_ok(worker: Mapping[str, Any]) -> bool:
    status = worker.get("status") if isinstance(worker.get("status"), Mapping) else {}
    candidates = [
        status.get("music_node") if isinstance(status, Mapping) else None,
        status.get("lavalink") if isinstance(status, Mapping) else None,
        _nested(status, "services", "lavalink") if isinstance(status, Mapping) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            if _as_bool(candidate.get("ok"), False) or _as_bool(candidate.get("online"), False) or str(candidate.get("state") or "").lower() in {"ok", "online", "healthy"}:
                return True
            if any(key in candidate for key in ("ok", "online", "state")):
                return False
    # Compatibilidade: bases antigas do worker ainda não reportam music_node.
    # Nesse caso, a checagem do próprio Lavalink continua decidindo no backend.
    return not _as_bool(getattr(config, "MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS", False), False)


def select_music_worker() -> MusicWorkerSelection:
    if not music_worker_only_enabled():
        return MusicWorkerSelection(True, reason="worker-only desativado")

    required_roles = _csv(getattr(config, "MUSIC_WORKER_REQUIRED_ROLES", "phone-worker"))
    required_caps = _csv(getattr(config, "MUSIC_WORKER_REQUIRED_CAPABILITIES", "ffmpeg,ffprobe"))
    require_turbo = _as_bool(getattr(config, "MUSIC_WORKER_REQUIRE_TURBO", True), True)

    workers = _load_public_workers()
    online = [w for w in workers if bool(w.get("enabled", True)) and bool(w.get("online"))]
    if not online:
        return MusicWorkerSelection(False, reason="nenhum worker online")

    rejected: list[str] = []
    for worker in online:
        roles_caps = _csv(worker.get("roles")) | _csv(worker.get("capabilities")) | _csv(_nested(worker, "status", "profile"))
        worker_id = str(worker.get("worker_id") or "")
        name = str(worker.get("name") or worker_id or "Core Worker")
        missing_roles = sorted(required_roles - roles_caps)
        missing_caps = sorted(required_caps - roles_caps)
        if require_turbo and not _worker_is_turbo(worker, roles_caps):
            rejected.append(f"{worker_id or name}:não_turbo")
            continue
        if missing_roles or missing_caps:
            rejected.append(f"{worker_id or name}:sem_capacidade")
            continue
        if not _music_node_status_ok(worker):
            rejected.append(f"{worker_id or name}:music_node_offline")
            continue
        return MusicWorkerSelection(True, worker_id=worker_id, name=name, worker=worker, reason="ok")

    reason = "; ".join(rejected[:3]) if rejected else "nenhum worker compatível"
    return MusicWorkerSelection(False, reason=reason)


async def ensure_music_worker_available() -> MusicWorkerSelection:
    # Mantido async para uso uniforme em comandos/interações.
    return select_music_worker()


def require_music_worker_available() -> MusicWorkerSelection:
    selection = select_music_worker()
    if not selection.available:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    return selection


async def require_music_worker_available_async() -> MusicWorkerSelection:
    return require_music_worker_available()
