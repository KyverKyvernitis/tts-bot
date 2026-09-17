from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping
from urllib.parse import urljoin

import aiohttp
import config

from ..nucleo.erros import MusicExtractionError
from ..nucleo.modelos import ExtractedBatch, MusicTrack
from .modelos import MUSIC_WORKER_UNAVAILABLE_MESSAGE, MusicWorkerUnavailable
from .selecao import require_music_worker_available_async
from .utilitarios import _as_bool, _phone_worker_base_url

logger = logging.getLogger(__name__)
_RESOLVE_CACHE: dict[tuple[str, int, bool, bool], tuple[float, ExtractedBatch]] = {}
_RESOLVE_CACHE_MAX_ITEMS = 96

def _track_copy_for_request(track: MusicTrack, *, requester_id: int, requester_name: str) -> MusicTrack:
    clone = MusicTrack(
        title=track.title,
        webpage_url=track.webpage_url,
        requester_id=int(requester_id or track.requester_id or 0),
        requester_name=requester_name or track.requester_name or "",
        stream_url=track.stream_url,
        duration=track.duration,
        uploader=track.uploader,
        thumbnail=track.thumbnail,
        source=track.source,
        original_url=track.original_url,
        extractor=track.extractor,
        is_live=track.is_live,
    )
    clone.resolved_at_monotonic = track.resolved_at_monotonic
    clone.resolved_audio_max_abr = track.resolved_audio_max_abr
    clone.resolved_audio_abr = track.resolved_audio_abr
    clone.resolved_audio_ext = track.resolved_audio_ext
    clone.resolved_audio_codec = track.resolved_audio_codec
    clone.resolved_audio_format_id = track.resolved_audio_format_id
    clone.fallback_reason = track.fallback_reason
    clone.display_title = track.display_title
    clone.display_uploader = track.display_uploader
    clone.display_thumbnail = track.display_thumbnail
    clone.display_source = track.display_source
    return clone

def _batch_copy_for_request(batch: ExtractedBatch, *, requester_id: int, requester_name: str) -> ExtractedBatch:
    return ExtractedBatch(
        tracks=[_track_copy_for_request(track, requester_id=requester_id, requester_name=requester_name) for track in batch.tracks],
        query=batch.query,
        is_playlist=batch.is_playlist,
        playlist_title=batch.playlist_title,
        truncated=batch.truncated,
    )

def _resolve_cache_ttl(*, metadata_only: bool) -> float:
    if metadata_only:
        return max(0.0, float(getattr(config, "MUSIC_WORKER_SEARCH_CACHE_TTL_SECONDS", 420.0) or 0.0))
    return max(0.0, float(getattr(config, "MUSIC_WORKER_DIRECT_CACHE_TTL_SECONDS", 90.0) or 0.0))

def _resolve_cache_key(query: str, limit: int, metadata_only: bool, allow_playlist: bool = False) -> tuple[str, int, bool, bool]:
    return (str(query or "").strip().lower(), int(limit or 1), bool(metadata_only), bool(allow_playlist))

def _resolve_cache_get(key: tuple[str, int, bool, bool], *, requester_id: int, requester_name: str, metadata_only: bool) -> ExtractedBatch | None:
    ttl = _resolve_cache_ttl(metadata_only=metadata_only)
    if ttl <= 0:
        return None
    item = _RESOLVE_CACHE.get(key)
    if not item:
        return None
    created, batch = item
    if time.monotonic() - created > ttl:
        _RESOLVE_CACHE.pop(key, None)
        return None
    return _batch_copy_for_request(batch, requester_id=requester_id, requester_name=requester_name)

def _resolve_cache_put(key: tuple[str, int, bool, bool], batch: ExtractedBatch, *, metadata_only: bool) -> None:
    if _resolve_cache_ttl(metadata_only=metadata_only) <= 0 or not batch.tracks:
        return
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX_ITEMS:
        oldest = min(_RESOLVE_CACHE.items(), key=lambda item: item[1][0])[0]
        _RESOLVE_CACHE.pop(oldest, None)
    _RESOLVE_CACHE[key] = (time.monotonic(), _batch_copy_for_request(batch, requester_id=0, requester_name=""))

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
    allow_playlist: bool = False,
) -> ExtractedBatch:
    """Resolve pesquisa/link no phone worker turbo usando yt-dlp do celular.

    Em modo worker-only a VPS não chama yt-dlp local nem reproduz áudio.
    A resolução acontece no Phone Worker e o resultado é usado pelo Music Agent,
    que continua dono de toda a reprodução/voz.
    """
    selection = await require_music_worker_available_async()
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
    if not is_text_search and not allow_playlist:
        # Link direto de faixa deve respeitar exatamente o URL enviado. Playlists
        # passam allow_playlist=True e usam extração flat/metadata para enfileirar
        # rápido sem resolver todos os streams antes.
        max_limit = 1
    if metadata_only is None:
        metadata_only = bool(is_text_search)
    if metadata_only:
        default_timeout = float(getattr(config, "MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS", 12.0) or 12.0)
    else:
        default_timeout = float(getattr(config, "MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS", 28.0) or 28.0)
    total_timeout = max(5.0, float(timeout_seconds if timeout_seconds is not None else default_timeout))
    cache_key = _resolve_cache_key(clean_query, max_limit, bool(metadata_only), bool(allow_playlist))
    cached_batch = _resolve_cache_get(cache_key, requester_id=requester_id, requester_name=requester_name, metadata_only=bool(metadata_only))
    if cached_batch is not None:
        logger.info(
            "[music/worker] resolve cache hit | worker=%s query=%r tracks=%s metadata_only=%s",
            selection.worker_id or selection.name,
            clean_query,
            len(cached_batch.tracks),
            bool(metadata_only),
        )
        return cached_batch
    payload = {
        "task": "music_ytdlp_resolve",
        "query": clean_query,
        "limit": max_limit,
        "timeout_seconds": total_timeout,
        "metadata_only": bool(metadata_only),
        "allow_playlist": bool(allow_playlist),
        "js_runtimes": str(getattr(config, "MUSIC_WORKER_YTDLP_JS_RUNTIMES", "node") or "node"),
        "default_search": (
            f"ytsearch{max_limit}"
            if is_text_search
            else "auto"
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
        # Em worker-only a VPS só transporta estado/metadados do protocolo. A URL
        # resolvida por yt-dlp pertence ao fluxo do Music Agent no telefone.
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
        )
        track.display_title = str(item.get("display_title") or item.get("title") or "").strip()
        track.display_uploader = str(item.get("display_uploader") or item.get("uploader") or item.get("channel") or "").strip()
        if item_metadata_only:
            track.display_source = source or "YouTube"
        if direct_stream_url:
            # A URL foi resolvida pelo yt-dlp do Phone Worker. A VPS não toca esse
            # áudio; o Music Agent permanece responsável pela sessão de voz.
            track.display_source = "YouTube"
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
    batch = ExtractedBatch(
        tracks=tracks,
        query=clean_query,
        is_playlist=bool(data.get("is_playlist")),
        playlist_title=str(data.get("playlist_title") or ""),
        truncated=bool(data.get("truncated")),
    )
    _resolve_cache_put(cache_key, batch, metadata_only=bool(metadata_only))
    return _batch_copy_for_request(batch, requester_id=requester_id, requester_name=requester_name)
