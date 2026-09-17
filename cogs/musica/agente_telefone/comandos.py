from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Mapping

import aiohttp
import config

from ..nucleo.modelos import MusicTrack
from .modelos import (
    MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE,
    MUSIC_WORKER_UNAVAILABLE_MESSAGE,
    MusicWorkerEngineUnavailable,
    MusicWorkerUnavailable,
)
from .selecao import require_music_worker_available_async
from .utilitarios import _phone_worker_base_url

logger = logging.getLogger(__name__)

async def music_agent_command(
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
    """Send a command to the phone-worker Music Agent through /task.

    This is the control-plane bridge for the future architecture where the VPS
    edits UI/status while the same-bot agent on the phone owns voice/playback.
    """
    selection = await require_music_worker_available_async()
    base = _phone_worker_base_url()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    payload: dict[str, Any] = {
        "task": "music_agent_command",
        "action": action,
        "guild_id": int(guild_id or 0),
        "voice_channel_id": int(voice_channel_id or 0),
        "text_channel_id": int(text_channel_id or 0),
        "query": str(query or ""),
        "requester_id": int(requester_id or 0),
        "requester_name": requester_name or "",
        "timeout_seconds": float(timeout_seconds or getattr(config, "MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", 12.0) or 12.0),
    }
    if track is not None:
        if isinstance(track, MusicTrack):
            payload["track"] = {
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
            if not payload["query"]:
                payload["query"] = track.webpage_url or track.original_url or track.stream_url or track.title
        elif isinstance(track, Mapping):
            payload["track"] = dict(track)
    payload.update({k: v for k, v in extra.items() if v is not None})
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    total_timeout = max(2.0, float(payload["timeout_seconds"]) + 2.0)
    timeout = aiohttp.ClientTimeout(total=total_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base}/task", headers=headers, json=payload) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    detail = text[:400]
                    with contextlib.suppress(Exception):
                        parsed = json.loads(text or "{}")
                        if isinstance(parsed, Mapping):
                            detail = str(parsed.get("error") or parsed.get("message") or detail)[:400]
                    raise RuntimeError(f"Player remoto HTTP {response.status}: {detail}")
                data = json.loads(text or "{}")
    except Exception as exc:
        message = str(exc or "").strip() or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE
        logger.warning("[music/agent] comando remoto falhou | worker=%s action=%s erro=%s", selection.worker_id, action, message)
        lower = message.lower()
        if (
            "music agent" in lower
            or "configure music_agent" in lower
            or "connection" in lower
            or "connect" in lower
            or "refused" in lower
            or "timeout" in lower
            or "pynacl" in lower
            or "davey" in lower
            or "dependency" in lower
            or "unauthorized" in lower
        ):
            message = "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
        raise MusicWorkerEngineUnavailable(message[:260]) from exc
    if data.get("ok") is False:
        message = str(data.get("error") or data.get("message") or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE).strip()
        lower = message.lower()
        if "music agent" in lower or "configure music_agent" in lower or "sem token" in lower:
            message = str(getattr(config, "MUSIC_AGENT_MISSING_TOKEN_MESSAGE", MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE) or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE)
        raise MusicWorkerEngineUnavailable(message[:260])
    logger.info("[music/agent] comando remoto enviado | worker=%s action=%s guild=%s", selection.worker_id or selection.name, action, guild_id)
    return data

async def music_agent_status(*, timeout_seconds: float | None = None) -> dict[str, Any]:
    selection = await require_music_worker_available_async()
    base = _phone_worker_base_url()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    payload = {
        "task": "music_agent_status",
        "action": "status",
        "timeout_seconds": float(timeout_seconds or getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 2.5) or 2.5),
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=max(1.0, float(payload["timeout_seconds"]) + 1.0))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base}/task", headers=headers, json=payload) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}: {text[:220]}")
                data = json.loads(text or "{}")
    except Exception as exc:
        logger.info("[music/agent] status remoto indisponível | worker=%s erro=%s", selection.worker_id, exc)
        return {"ok": False, "available": False, "error": str(exc)}
    data.setdefault("ok", True)
    data.setdefault("available", bool(data.get("discord_ready")))
    return data
