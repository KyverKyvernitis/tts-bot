from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp

import config
from music_system.errors import MusicExtractionError
from music_system.models import ExtractedBatch, MusicTrack

logger = logging.getLogger(__name__)

MUSIC_WORKER_UNAVAILABLE_MESSAGE = str(
    getattr(config, "MUSIC_WORKER_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: Nenhum worker online")
    or "Sistema de música indisponível no momento: Nenhum worker online"
).strip()
MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE = str(
    getattr(config, "MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta")
    or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
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


class MusicWorkerEngineUnavailable(RuntimeError):
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
        return MUSIC_WORKER_UNAVAILABLE_MESSAGE if not self.available else ""


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
        "endpoint": f"{scheme}://{host}:{port}",
        "remote_addr": host,
        "status": {
            "profile": "turbo",
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
        },
    }
    return MusicWorkerSelection(True, worker_id=worker_id, name=str(worker["name"]), worker=worker, reason=reason)


def select_music_worker() -> MusicWorkerSelection:
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
        return MusicWorkerSelection(True, worker_id=worker_id, name=name, worker=worker, reason="ok")

    reason = "; ".join(rejected[:3]) if rejected else "nenhum worker compatível"
    configured = _configured_phone_worker_selection(reason=f"phone_worker_configurado_fallback:{reason}")
    if configured is not None:
        logger.info("[music/worker] usando phone worker configurado direto; recusas_registry=%s", reason)
        return configured
    return MusicWorkerSelection(False, reason=reason)




def _phone_worker_base_url() -> str:
    if not _as_bool(getattr(config, "PHONE_WORKER_ENABLED", False), False):
        return ""
    host = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not host or not token:
        return ""
    scheme = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    try:
        port = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
    except Exception:
        port = 8766
    return f"{scheme}://{host}:{port}"


def _direct_stream_url(data: Mapping[str, Any]) -> str:
    for key in ("stream_url", "url", "direct_url"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""




def _looks_like_url(value: str) -> bool:
    raw = str(value or "").strip().lower()
    return raw.startswith(("http://", "https://", "www."))


def _worker_stream_url(base: str, item: Mapping[str, Any]) -> str:
    explicit = str(item.get("worker_stream_url") or item.get("worker_audio_url") or "").strip()
    if explicit.startswith(("http://", "https://")):
        return explicit
    path = str(item.get("worker_stream_path") or item.get("worker_audio_path") or "").strip()
    if path.startswith("/") and base:
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    return ""
def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        if number <= 0:
            return None
        return number
    except Exception:
        return None


async def resolve_music_tracks_on_worker(
    query: str,
    *,
    requester_id: int = 0,
    requester_name: str = "",
    limit: int = 5,
    timeout_seconds: float | None = None,
    metadata_only: bool | None = None,
) -> ExtractedBatch:
    """Resolve pesquisa/link no phone worker turbo usando yt-dlp do celular.

    Em modo worker-only a VPS não deve chamar yt-dlp local nem depender de
    scsearch/SoundCloud como substituto. O worker retorna URLs diretas de áudio
    que o Lavalink do próprio worker toca pela fonte HTTP.
    """
    selection = require_music_worker_available()
    base = _phone_worker_base_url()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    clean_query = str(query or "").strip()
    if not clean_query:
        return ExtractedBatch(tracks=[], query="", is_playlist=False)
    try:
        max_limit = max(1, min(10, int(limit or 5)))
    except Exception:
        max_limit = 5
    is_text_search = not _looks_like_url(clean_query)
    if metadata_only is None:
        metadata_only = bool(is_text_search)
    if metadata_only:
        default_timeout = float(getattr(config, "MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS", 12.0) or 12.0)
    else:
        default_timeout = float(getattr(config, "MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS", 28.0) or 28.0)
    total_timeout = max(5.0, float(timeout_seconds if timeout_seconds is not None else default_timeout))
    payload = {
        "task": "music_ytdlp_resolve",
        "query": clean_query,
        "limit": max_limit,
        "timeout_seconds": total_timeout,
        "metadata_only": bool(metadata_only),
        "js_runtimes": str(getattr(config, "MUSIC_WORKER_YTDLP_JS_RUNTIMES", "node") or "node"),
        "default_search": (
            f"ytsearch{max_limit}"
            if is_text_search
            else str(getattr(config, "MUSIC_WORKER_YTDLP_DEFAULT_SEARCH", "ytsearch") or "ytsearch")
        ),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=total_timeout + 2.0)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base}/task", headers=headers, json=payload) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}: {text[:240]}")
                data = json.loads(text or "{}")
    except MusicWorkerUnavailable:
        raise
    except Exception as exc:
        logger.warning("[music/worker] yt-dlp remoto falhou | worker=%s query=%r erro=%s", selection.worker_id, clean_query, exc)
        raise MusicExtractionError("`⚠️` Não consegui resolver essa música no worker agora. Tente novamente em alguns segundos.", detail=str(exc)) from exc

    if data.get("ok") is False:
        message = str(data.get("message") or data.get("error") or "worker retornou erro ao resolver música")
        raise MusicExtractionError(f"`⚠️` {message[:220]}", detail=message)

    raw_tracks = data.get("tracks") if isinstance(data.get("tracks"), list) else []
    tracks: list[MusicTrack] = []
    for item in raw_tracks[:max_limit]:
        if not isinstance(item, Mapping):
            continue
        direct_stream_url = _direct_stream_url(item)
        worker_stream_url = _worker_stream_url(base, item)
        # O phone worker pode expor um endpoint PCM/cacheado, mas esse caminho
        # ainda faz a VPS virar relé de áudio e engasga em rede móvel/Tailscale.
        # Para música worker-only, a VPS deve passar a URL direta resolvida pelo
        # yt-dlp para o Lavalink do próprio worker transportar até o Discord.
        stream_url = direct_stream_url or worker_stream_url
        item_metadata_only = _as_bool(item.get("metadata_only") or item.get("search_only"), False)
        webpage_url = str(item.get("webpage_url") or item.get("original_url") or clean_query).strip()
        if not stream_url and not item_metadata_only:
            continue
        if item_metadata_only and not webpage_url:
            continue
        title = str(item.get("title") or item.get("fulltitle") or "Música").strip() or "Música"
        raw_source = str(item.get("source") or item.get("extractor") or "worker-ytdlp").strip() or "worker-ytdlp"
        lower_source = raw_source.lower()
        # O extractor interno continua worker-ytdlp para roteamento/playback, mas
        # o painel público deve mostrar a origem de conteúdo, não o nome técnico do job.
        source = "YouTube" if "ytdlp" in lower_source or "yt-dlp" in lower_source or "youtube" in lower_source else raw_source
        track = MusicTrack(
            title=title,
            webpage_url=webpage_url,
            original_url=str(item.get("original_query") or clean_query),
            stream_url=stream_url,
            requester_id=int(requester_id or 0),
            requester_name=requester_name or "",
            duration=_float_or_none(item.get("duration")),
            uploader=str(item.get("uploader") or item.get("channel") or "").strip(),
            thumbnail=str(item.get("thumbnail") or "").strip(),
            source=source,
            extractor="worker-ytdlp",
            is_live=_as_bool(item.get("is_live"), False),
            lavalink_query=direct_stream_url or stream_url,
            lavalink_resolved=bool(direct_stream_url),
        )
        if item_metadata_only:
            track.lavalink_query = ""
            track.lavalink_resolved = False
            track.display_source = "YouTube"
        if direct_stream_url:
            # Metadados vêm do worker/yt-dlp, mas o transporte de áudio é feito
            # pelo Lavalink do worker. Não marque como Worker local/PCM.
            track.display_source = "YouTube"
            track.lavalink_query = direct_stream_url
            track.lavalink_resolved = True
        elif worker_stream_url:
            # Compatibilidade com workers antigos: só use esse endpoint se o
            # worker não devolveu URL direta. O roteador atual evitará esse
            # caminho para pesquisa comum sempre que houver direct_stream_url.
            track.display_source = "Worker local"
        
        tracks.append(track)
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    logger.info(
        "[music/worker] yt-dlp remoto ok | worker=%s query=%r tracks=%s metadata_only=%s elapsed_ms=%.1f js=%s search=%s cli_rc=%s cli_error=%r",
        selection.worker_id or selection.name,
        clean_query,
        len(tracks),
        bool(data.get("metadata_only") or metadata_only),
        elapsed_ms,
        data.get("js_runtime") or "",
        data.get("default_search") or "",
        data.get("cli_rc"),
        str(data.get("cli_error") or data.get("api_error") or "")[:220],
    )
    return ExtractedBatch(
        tracks=tracks,
        query=clean_query,
        is_playlist=bool(data.get("is_playlist")),
        playlist_title=str(data.get("playlist_title") or ""),
        truncated=bool(data.get("truncated")),
    )


async def ensure_music_worker_available() -> MusicWorkerSelection:
    # Mantido async para uso uniforme em comandos/interações.
    return select_music_worker()


def require_music_worker_available() -> MusicWorkerSelection:
    selection = select_music_worker()
    if not selection.available:
        logger.info("[music/worker] indisponível: %s", selection.reason or "sem motivo")
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    return selection


async def require_music_worker_available_async() -> MusicWorkerSelection:
    return require_music_worker_available()
