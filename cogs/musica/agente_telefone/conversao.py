from __future__ import annotations

import contextlib
from typing import Any

from ..nucleo.modelos import MusicTrack


def faixa_do_payload(payload: dict[str, Any], fallback: MusicTrack | None = None) -> MusicTrack | None:
    """Converte o estado serializado pelo Music Agent para o modelo da VPS."""
    if not isinstance(payload, dict):
        return fallback

    def primeiro_util(*values: object, generic: set[str] | None = None) -> str:
        bloqueados = generic or {
            "youtube", "link", "música", "musica", "desconhecida", "unknown",
            "worker-agent", "music-agent", "music-agent-ytdlp", "worker-ytdlp",
        }
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            lower = text.lower()
            if lower in bloqueados:
                continue
            if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
                continue
            return text
        return ""

    fallback_title = getattr(fallback, "title", "") if fallback is not None else ""
    fallback_uploader = getattr(fallback, "uploader", "") if fallback is not None else ""
    title = primeiro_util(
        payload.get("display_title"), payload.get("title"), payload.get("fulltitle"),
        payload.get("name"), payload.get("track_title"), fallback_title,
    ) or "Música"
    uploader = primeiro_util(
        payload.get("display_uploader"), payload.get("uploader"), payload.get("author"),
        payload.get("channel"), payload.get("creator"), payload.get("artist"), fallback_uploader,
        generic={"youtube", "desconhecida", "unknown", "worker-agent", "music-agent", "worker-ytdlp"},
    )
    webpage_url = str(
        payload.get("webpage_url")
        or payload.get("url")
        or payload.get("display_url")
        or (getattr(fallback, "webpage_url", "") if fallback is not None else "")
        or payload.get("query")
        or ""
    ).strip()
    requester_id = int(payload.get("requester_id") or (getattr(fallback, "requester_id", 0) if fallback is not None else 0) or 0)
    requester_name = str(payload.get("requester_name") or (getattr(fallback, "requester_name", "") if fallback is not None else "") or "").strip()
    source = str(payload.get("display_source") or payload.get("source") or (getattr(fallback, "source", "") if fallback is not None else "") or "YouTube")
    thumbnail = str(
        payload.get("display_thumbnail")
        or payload.get("thumbnail")
        or payload.get("thumb")
        or (getattr(fallback, "thumbnail", "") if fallback is not None else "")
        or ""
    )
    stream_url = str(payload.get("stream_url") or (getattr(fallback, "stream_url", "") if fallback is not None else "") or "")
    track = MusicTrack(
        title=title,
        webpage_url=webpage_url,
        original_url=str(payload.get("original_url") or payload.get("query") or (getattr(fallback, "original_url", "") if fallback is not None else "") or webpage_url),
        stream_url=stream_url,
        requester_id=requester_id,
        requester_name=requester_name,
        duration=(payload.get("duration") if payload.get("duration") is not None else (getattr(fallback, "duration", None) if fallback is not None else None)),
        uploader=uploader,
        thumbnail=thumbnail,
        source=source,
        extractor=str(payload.get("extractor") or "worker-ytdlp"),
        is_live=bool(payload.get("is_live") or (getattr(fallback, "is_live", False) if fallback is not None else False)),
    )
    track.display_source = "YouTube" if "youtube" in track.source.lower() or "ytdlp" in track.source.lower() else track.source
    track.display_title = title
    track.display_uploader = uploader
    track.display_thumbnail = thumbnail
    with contextlib.suppress(Exception):
        track.resolved_audio_abr = int(float(payload.get("resolved_audio_abr") or payload.get("audio_abr") or payload.get("abr") or 0))
    with contextlib.suppress(Exception):
        track.resolved_audio_max_abr = int(float(payload.get("resolved_audio_max_abr") or payload.get("audio_max_abr") or payload.get("max_abr") or track.resolved_audio_abr or 0))
    track.resolved_audio_ext = str(payload.get("resolved_audio_ext") or payload.get("audio_ext") or payload.get("ext") or "").strip()
    track.resolved_audio_codec = str(payload.get("resolved_audio_codec") or payload.get("audio_codec") or payload.get("codec") or "").strip()
    track.resolved_audio_format_id = str(payload.get("resolved_audio_format_id") or payload.get("audio_format_id") or payload.get("format_id") or "").strip()
    return track


def estado_da_guild_no_payload(payload: dict[str, Any], guild_id: int) -> dict[str, Any]:
    """Extrai somente o estado da guild retornado pelo Music Agent."""
    guilds = payload.get("guilds") if isinstance(payload, dict) else {}
    if not isinstance(guilds, dict):
        return {}
    state = guilds.get(str(guild_id)) or guilds.get(guild_id)
    return state if isinstance(state, dict) else {}
