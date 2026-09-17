from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

import config

from .modelos import (
    MUSIC_WORKER_UNAVAILABLE_MESSAGE,
    MusicWorkerSelection,
    MusicWorkerUnavailable,
)
from .utilitarios import (
    _as_bool,
    _csv,
    _nested,
    _phone_worker_base_url,
    _version_at_least,
)

logger = logging.getLogger(__name__)
_SELECTION_CACHE: dict[str, Any] = {"at": 0.0, "selection": None}

def music_worker_only_enabled() -> bool:
    return _as_bool(getattr(config, "MUSIC_WORKER_ONLY_ENABLED", True), True)

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

def _worker_music_node(worker: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(worker, Mapping):
        return None
    status = worker.get("status") if isinstance(worker.get("status"), Mapping) else {}
    candidates = [
        status.get("music_node") if isinstance(status, Mapping) else None,
        status.get("lavalink") if isinstance(status, Mapping) else None,
        _nested(status, "services", "lavalink") if isinstance(status, Mapping) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return None

def _worker_music_agent(worker: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(worker, Mapping):
        return None
    status = worker.get("status") if isinstance(worker.get("status"), Mapping) else {}
    candidates = [
        status.get("music_agent") if isinstance(status, Mapping) else None,
        _nested(status, "services", "music_agent") if isinstance(status, Mapping) else None,
        worker.get("music_agent"),
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return None

def _music_agent_active_sessions(agent: Mapping[str, Any] | None) -> int:
    if not isinstance(agent, Mapping):
        return 0
    guilds = agent.get("guilds") if isinstance(agent.get("guilds"), Mapping) else {}
    active = 0
    for state in guilds.values():
        if not isinstance(state, Mapping):
            continue
        status = str(state.get("status") or "").strip().lower()
        if status in {"preparing", "starting", "playing", "paused", "queued"}:
            active += 1
    return active

def worker_music_agent_summary(worker: Mapping[str, Any] | None) -> dict[str, Any]:
    agent = _worker_music_agent(worker)
    min_version = str(getattr(config, "MUSIC_AGENT_MIN_VERSION", "0.3.8") or "0.3.8")
    max_sessions = max(1, int(getattr(config, "MUSIC_AGENT_MAX_SESSIONS_PER_WORKER", 2) or 2))
    if not isinstance(agent, Mapping):
        return {"available": False, "reason": "music_agent_missing", "version": "", "active_sessions": 0, "max_sessions": max_sessions}
    version = str(agent.get("version") or agent.get("file_version") or "").strip()
    active_sessions = _music_agent_active_sessions(agent)
    ok = _as_bool(agent.get("ok"), False) or _as_bool(agent.get("available"), False)
    # YouTube direto usa o Music Agent com voz/ffmpeg e não depende do pool
    # Lavalink. O pool só é obrigatório para fontes que realmente usam
    # Lavalink/LavaSrc; não pode bloquear a seleção do worker turbo online.
    ready = ok and _as_bool(agent.get("discord_ready"), False)
    deps = agent.get("voice_dependencies") if isinstance(agent.get("voice_dependencies"), Mapping) else {}
    missing_deps_raw = agent.get("dependency_missing") or (deps.get("missing") if isinstance(deps, Mapping) else [])
    if isinstance(missing_deps_raw, str):
        missing_deps = [missing_deps_raw]
    elif isinstance(missing_deps_raw, (list, tuple, set)):
        missing_deps = list(missing_deps_raw)
    else:
        missing_deps = []
    if version and not _version_at_least(version, min_version):
        return {"available": False, "reason": f"music_agent_old:{version}<{min_version}", "version": version, "active_sessions": active_sessions, "max_sessions": max_sessions}
    if missing_deps:
        return {"available": False, "reason": "dependency_missing:" + ",".join(str(x) for x in list(missing_deps)[:4]), "version": version, "active_sessions": active_sessions, "max_sessions": max_sessions}
    if not ready:
        return {"available": False, "reason": "music_agent_not_ready", "version": version, "active_sessions": active_sessions, "max_sessions": max_sessions}
    if active_sessions >= max_sessions:
        return {"available": False, "reason": "music_agent_full", "version": version, "active_sessions": active_sessions, "max_sessions": max_sessions}
    return {"available": True, "reason": "ok", "version": version, "active_sessions": active_sessions, "max_sessions": max_sessions}

def _music_agent_bootstrap_allowed(reason: object = "") -> bool:
    """True when an online turbo worker may be selected to start/fix its agent.

    A stopped/outdated agent is not the same thing as an offline worker. Selecting
    the worker lets phone_worker.py start/restart the Music Agent on the real play
    command and avoids the wrong behavior where `_play` says there is no worker.
    Capacity remains a hard block.
    """
    if not _as_bool(getattr(config, "MUSIC_AGENT_BOOTSTRAP_ON_PLAY", True), True):
        return False
    text = str(reason or "").lower()
    return "music_agent_full" not in text

def worker_music_summary(worker: Mapping[str, Any] | None) -> dict[str, Any]:
    node = _worker_music_node(worker)
    if not isinstance(node, Mapping):
        return {"available": False, "mode": "", "state": "unknown"}
    state = str(node.get("state") or "").strip().lower()
    online = _as_bool(node.get("ok"), False) or _as_bool(node.get("online"), False) or state in {"ok", "online", "healthy"}
    mode = str(node.get("mode") or node.get("kind") or "lavalink").strip().lower()
    return {
        "available": bool(online),
        "mode": mode,
        "state": state or ("healthy" if online else "offline"),
        "host": str(node.get("host") or ""),
        "port": node.get("port"),
        "error": str(node.get("error") or ""),
    }

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
    # Em worker-only, o registry pode ficar alguns heartbeats atrasado em relação
    # ao phone worker real. Quando MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS=false,
    # o gate não deve bloquear por status stale/offline; o backend Lavalink e o
    # job remoto ainda fazem o healthcheck real antes de tocar.
    require_status = _as_bool(getattr(config, "MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS", False), False)
    node = _worker_music_node(worker)
    if isinstance(node, Mapping):
        summary = worker_music_summary(worker)
        if summary.get("available"):
            return True
        if any(key in node for key in ("ok", "online", "state")):
            return not require_status
    # Compatibilidade: bases antigas do worker ainda não reportam music_node.
    # Nesse caso, a configuração/healthcheck do próprio engine decide no backend.
    return not require_status

def _probe_configured_phone_worker_health(base_url: str, token: str) -> Mapping[str, Any] | None:
    """Healthcheck curto para não tratar worker configurado/offline como online.

    O registry pode estar atrasado, então ainda aceitamos o worker configurado;
    mas ele precisa responder rapidamente. Assim `_play` falha na hora quando o
    celular/turbo worker não está realmente disponível.
    """
    if not base_url or not token:
        return None
    enabled = _as_bool(getattr(config, "MUSIC_WORKER_CONFIGURED_HEALTHCHECK_ENABLED", True), True)
    if not enabled:
        return {"ok": True, "profile": "turbo"}
    try:
        timeout = max(0.3, min(8.0, float(getattr(config, "MUSIC_WORKER_CONFIGURED_HEALTH_TIMEOUT_SECONDS", 3.5) or 3.5)))
    except Exception:
        timeout = 3.5
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/health", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(384 * 1024).decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        if isinstance(data, Mapping) and bool(data.get("ok")):
            return data
    except Exception as exc:
        logger.info("[music/worker] phone worker configurado não respondeu rápido: %s", exc)
    return None

def _configured_phone_worker_selection(reason: str = "phone_worker_configurado") -> MusicWorkerSelection | None:
    if not _as_bool(getattr(config, "PHONE_WORKER_ENABLED", False), False):
        return None
    host = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not host or not token:
        return None
    scheme = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    try:
        port = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
    except Exception:
        port = 8766

    endpoint = f"{scheme}://{host}:{port}"
    health = _probe_configured_phone_worker_health(endpoint, token)
    if health is None:
        return None

    if _as_bool(getattr(config, "MUSIC_AGENT_ENABLED", True), True):
        agent_summary = worker_music_agent_summary({"status": {"music_agent": health.get("music_agent") if isinstance(health, Mapping) else None}})
        if not agent_summary.get("available"):
            if _music_agent_bootstrap_allowed(agent_summary.get("reason")):
                logger.info(
                    "[music/worker] phone worker configurado será usado para preparar Music Agent: %s",
                    agent_summary.get("reason"),
                )
            else:
                logger.info("[music/worker] phone worker configurado sem Music Agent elegível: %s", agent_summary.get("reason"))
                return None

    lavalink_host = str(
        getattr(config, "MUSIC_WORKER_LAVALINK_HOST", "")
        or getattr(config, "AUX_LAVALINK_HOST", "")
        or os.getenv("PHONE_LAVALINK_HOST", "")
        or host
    ).strip()
    try:
        lavalink_port = int(
            getattr(config, "MUSIC_WORKER_LAVALINK_PORT", None)
            or getattr(config, "AUX_LAVALINK_PORT", None)
            or os.getenv("PHONE_LAVALINK_PORT", "")
            or 2333
        )
    except Exception:
        lavalink_port = 2333

    required_roles = _csv(getattr(config, "MUSIC_WORKER_REQUIRED_ROLES", "phone-worker"))
    required_caps = _csv(getattr(config, "MUSIC_WORKER_REQUIRED_CAPABILITIES", "ffmpeg,ffprobe"))
    roles = sorted(required_roles | {"phone-worker", "music", "music-node", "music-lavalink", "music-ytdlp"})
    capabilities = sorted(
        required_caps
        | {
            "phone-worker",
            "ffmpeg",
            "ffprobe",
            "music",
            "music-node",
            "music-lavalink",
            "music-ytdlp",
            "music-ytdlp-resolve",
            "service-control",
        }
    )
    worker_id = str(os.getenv("CORE_WORKER_ID") or getattr(config, "PHONE_WORKER_ID", "") or "phone-worker-configured").strip()
    worker = {
        "worker_id": worker_id,
        "name": str(os.getenv("CORE_WORKER_NAME") or "Phone Worker Turbo"),
        "online": True,
        "enabled": True,
        "profile": "turbo",
        "roles": roles,
        "capabilities": capabilities,
        "endpoint": endpoint,
        "remote_addr": host,
        "status": {
            "profile": str(health.get("profile") or "turbo"),
            "music_node": {
                "kind": "lavalink",
                "mode": "lavalink",
                "ok": True,
                "online": True,
                "state": "configured",
                "host": "127.0.0.1",
                "public_host": lavalink_host,
                "connect_host": lavalink_host,
                "port": lavalink_port,
                "public_port": lavalink_port,
                "connect_port": lavalink_port,
            },
            "music_agent": health.get("music_agent") if isinstance(health, Mapping) else {},
        },
    }
    return MusicWorkerSelection(True, worker_id=worker_id, name=str(worker["name"]), worker=worker, reason=reason)

def _selection_cache_seconds() -> float:
    try:
        return max(0.0, min(3.0, float(getattr(config, "MUSIC_WORKER_SELECTION_CACHE_SECONDS", 0.8) or 0.0)))
    except Exception:
        return 0.8

def select_music_worker() -> MusicWorkerSelection:
    ttl = _selection_cache_seconds()
    now = time.monotonic()
    cached = _SELECTION_CACHE.get("selection")
    if ttl > 0 and isinstance(cached, MusicWorkerSelection) and now - float(_SELECTION_CACHE.get("at") or 0.0) <= ttl:
        return cached
    selection = _select_music_worker_uncached()
    _SELECTION_CACHE["selection"] = selection
    _SELECTION_CACHE["at"] = now
    return selection

def _select_music_worker_uncached() -> MusicWorkerSelection:
    if not music_worker_only_enabled():
        return MusicWorkerSelection(True, reason="worker-only desativado")

    required_roles = _csv(getattr(config, "MUSIC_WORKER_REQUIRED_ROLES", "phone-worker"))
    required_caps = _csv(getattr(config, "MUSIC_WORKER_REQUIRED_CAPABILITIES", "ffmpeg,ffprobe"))
    require_turbo = _as_bool(getattr(config, "MUSIC_WORKER_REQUIRE_TURBO", True), True)

    workers = _load_public_workers()
    online = [w for w in workers if bool(w.get("enabled", True)) and bool(w.get("online"))]
    if not online:
        configured = _configured_phone_worker_selection(reason="phone_worker_configurado_sem_registry_online")
        if configured is not None:
            logger.info("[music/worker] usando phone worker configurado direto; registry sem worker online")
            return configured
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
        if _as_bool(getattr(config, "MUSIC_AGENT_ENABLED", True), True):
            agent_summary = worker_music_agent_summary(worker)
            if not agent_summary.get("available"):
                reason = str(agent_summary.get('reason') or 'music_agent_indisponivel')
                live = _configured_phone_worker_selection(reason=f"live_status_registry_stale:{reason}")
                if live is not None:
                    live_summary = worker_music_agent_summary(live.worker)
                    if live_summary.get("available"):
                        logger.info("[music/worker] registry stale; usando status live do phone worker | worker=%s reason=%s", live.worker_id or live.name, reason)
                        return live
                if not _music_agent_bootstrap_allowed(reason):
                    rejected.append(f"{worker_id or name}:{reason}")
                    continue
                logger.info("[music/worker] selecionando worker turbo para preparar Music Agent | worker=%s reason=%s", worker_id or name, reason)
                return MusicWorkerSelection(True, worker_id=worker_id, name=name, worker=worker, reason=f"bootstrap:{reason}")
        return MusicWorkerSelection(True, worker_id=worker_id, name=name, worker=worker, reason="ok")

    reason = "; ".join(rejected[:3]) if rejected else "nenhum worker compatível"
    configured = _configured_phone_worker_selection(reason=f"phone_worker_configurado_fallback:{reason}")
    if configured is not None:
        logger.info("[music/worker] usando phone worker configurado direto; recusas_registry=%s", reason)
        return configured
    return MusicWorkerSelection(False, reason=reason)

def _raise_unavailable(selection: MusicWorkerSelection) -> None:
    logger.info("[music/worker] indisponível: %s", selection.reason or "sem motivo")
    raise MusicWorkerUnavailable(selection.message or MUSIC_WORKER_UNAVAILABLE_MESSAGE)

async def select_music_worker_async() -> MusicWorkerSelection:
    """Seleciona worker fora do event loop.

    A seleção pode ler o registry em disco e, em fallback, consultar /health do
    phone worker configurado. Chamá-la diretamente em callback async congela o bot
    por até alguns segundos e causa interações 10062/Unknown interaction.
    """
    return await asyncio.to_thread(select_music_worker)

async def ensure_music_worker_available() -> MusicWorkerSelection:
    # Mantido async para uso uniforme em comandos/interações, agora sem bloquear o
    # event loop durante registry/healthcheck.
    return await select_music_worker_async()

def require_music_worker_available() -> MusicWorkerSelection:
    selection = select_music_worker()
    if not selection.available:
        _raise_unavailable(selection)
    return selection

async def require_music_worker_available_async() -> MusicWorkerSelection:
    selection = await select_music_worker_async()
    if not selection.available:
        _raise_unavailable(selection)
    return selection
