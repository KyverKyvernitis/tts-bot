from __future__ import annotations

from typing import Any, Mapping

from cogs.musica import configuracao as config

from ..nucleo.modelos import MusicTrack


def faixa_para_payload(track: MusicTrack | Mapping[str, Any]) -> dict[str, Any]:
    """Converte uma faixa para o contrato JSON aceito pelo Music Agent.

    Este módulo não faz rede e não conhece Discord. Ele existe para manter o
    protocolo VPS -> Phone Worker separado do transporte HTTP.
    """
    if isinstance(track, Mapping):
        return dict(track)
    return {
        "title": track.title,
        "display_title": getattr(track, "display_title", "") or track.title,
        "webpage_url": track.webpage_url,
        "original_url": track.original_url,
        "stream_url": track.stream_url,
        "duration": track.duration,
        "uploader": track.uploader,
        "display_uploader": getattr(track, "display_uploader", "") or track.uploader,
        "thumbnail": track.thumbnail,
        "source": track.source,
        "display_source": getattr(track, "display_source", "") or track.source,
        "extractor": track.extractor,
        "requester_id": track.requester_id,
        "requester_name": track.requester_name,
        "resolved_audio_format_id": getattr(track, "resolved_audio_format_id", ""),
        "resolved_audio_ext": getattr(track, "resolved_audio_ext", ""),
        "resolved_audio_codec": getattr(track, "resolved_audio_codec", ""),
        "resolved_audio_abr": getattr(track, "resolved_audio_abr", 0),
        "audio_format_id": getattr(track, "audio_format_id", ""),
        "audio_ext": getattr(track, "audio_ext", ""),
        "audio_codec": getattr(track, "audio_codec", ""),
        "audio_abr": getattr(track, "audio_abr", 0),
    }


def montar_comando(
    action: str,
    *,
    guild_id: int = 0,
    voice_channel_id: int = 0,
    text_channel_id: int = 0,
    query: str = "",
    track: MusicTrack | Mapping[str, Any] | None = None,
    requester_id: int = 0,
    requester_name: str = "",
    timeout_seconds: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task": "music_agent_command",
        "action": str(action or ""),
        "guild_id": int(guild_id or 0),
        "voice_channel_id": int(voice_channel_id or 0),
        "text_channel_id": int(text_channel_id or 0),
        "query": str(query or ""),
        "requester_id": int(requester_id or 0),
        "requester_name": requester_name or "",
        "timeout_seconds": float(
            timeout_seconds
            or getattr(config, "MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", 12.0)
            or 12.0
        ),
    }
    if track is not None:
        payload["track"] = faixa_para_payload(track)
        if not payload["query"] and isinstance(track, MusicTrack):
            payload["query"] = (
                track.webpage_url
                or track.original_url
                or track.stream_url
                or track.title
            )
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def montar_consulta_status(
    *,
    timeout_seconds: float | None = None,
    guild_id: int = 0,
    compact: bool | None = None,
) -> dict[str, Any]:
    guild_id = int(guild_id or 0)
    payload: dict[str, Any] = {
        "task": "music_agent_status",
        "action": "status",
        "timeout_seconds": float(
            timeout_seconds
            or getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 2.5)
            or 2.5
        ),
    }
    if guild_id > 0:
        payload["guild_id"] = guild_id
        payload["compact"] = True if compact is None else bool(compact)
    return payload
