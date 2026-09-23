from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urljoin

from ..nucleo.modelos import ExtractedBatch, MusicTrack
from .utilitarios import _as_bool


def parece_url(value: str) -> bool:
    raw = str(value or "").strip().lower()
    return raw.startswith(("http://", "https://", "www."))


def url_stream_direta(data: Mapping[str, Any]) -> str:
    for key in ("stream_url", "url", "direct_url"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def url_stream_worker(base: str, item: Mapping[str, Any]) -> str:
    explicit = str(
        item.get("worker_stream_url") or item.get("worker_audio_url") or ""
    ).strip()
    if explicit.startswith(("http://", "https://")):
        return explicit
    path = str(
        item.get("worker_stream_path") or item.get("worker_audio_path") or ""
    ).strip()
    if path.startswith("/") and base:
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    return ""


def numero_positivo_ou_nulo(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        if number <= 0:
            return None
        return number
    except Exception:
        return None


def converter_resposta_resolucao(
    data: Mapping[str, Any],
    *,
    base: str,
    query: str,
    limit: int,
    requester_id: int,
    requester_name: str,
) -> ExtractedBatch:
    raw_tracks = data.get("tracks") if isinstance(data.get("tracks"), list) else []
    tracks: list[MusicTrack] = []

    for item in raw_tracks[:limit]:
        if not isinstance(item, Mapping):
            continue
        direct_stream_url = url_stream_direta(item)
        worker_stream_url = url_stream_worker(base, item)
        # O Phone Worker pode expor um endpoint PCM/cacheado, mas esse caminho
        # ainda faz a VPS virar relé de áudio. No modo worker-only a VPS só
        # transporta estado/metadados; o áudio pertence ao agente no telefone.
        stream_url = direct_stream_url or worker_stream_url
        item_metadata_only = _as_bool(
            item.get("metadata_only") or item.get("search_only"), False
        )
        webpage_url = str(
            item.get("webpage_url") or item.get("original_url") or query
        ).strip()
        if not stream_url and not item_metadata_only:
            continue
        if item_metadata_only and not webpage_url:
            continue

        title = str(item.get("title") or item.get("fulltitle") or "Música").strip() or "Música"
        raw_source = str(
            item.get("source") or item.get("extractor") or "worker-ytdlp"
        ).strip() or "worker-ytdlp"
        lower_source = raw_source.lower()
        source = (
            "YouTube"
            if "ytdlp" in lower_source
            or "yt-dlp" in lower_source
            or "youtube" in lower_source
            else raw_source
        )
        track = MusicTrack(
            title=title,
            webpage_url=webpage_url,
            original_url=str(item.get("original_query") or query),
            stream_url=stream_url,
            requester_id=int(requester_id or 0),
            requester_name=requester_name or "",
            duration=numero_positivo_ou_nulo(item.get("duration")),
            uploader=str(item.get("uploader") or item.get("channel") or "").strip(),
            thumbnail=str(item.get("thumbnail") or "").strip(),
            source=source,
            extractor="worker-ytdlp",
            is_live=_as_bool(item.get("is_live"), False),
        )
        track.display_title = str(
            item.get("display_title") or item.get("title") or ""
        ).strip()
        track.display_uploader = str(
            item.get("display_uploader")
            or item.get("uploader")
            or item.get("channel")
            or ""
        ).strip()
        if item_metadata_only:
            track.display_source = source or "YouTube"
        if direct_stream_url:
            track.display_source = "YouTube"
        elif worker_stream_url:
            track.display_source = "Worker local"
        track.queue_item_id = str(item.get("queue_item_id") or "").strip()
        tracks.append(track)

    return ExtractedBatch(
        tracks=tracks,
        query=query,
        is_playlist=bool(data.get("is_playlist")),
        playlist_title=str(data.get("playlist_title") or ""),
        truncated=bool(data.get("truncated")),
    )
