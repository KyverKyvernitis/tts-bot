from __future__ import annotations

import logging
from typing import Any, Mapping

from cogs.musica import configuracao as config

from ..nucleo.modelos import MusicTrack
from .protocolo import montar_comando, montar_consulta_status
from .modelos import (
    MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE,
    MUSIC_WORKER_UNAVAILABLE_MESSAGE,
    MusicWorkerEngineUnavailable,
    MusicWorkerUnavailable,
)
from .selecao import require_music_worker_available_async
from .roteamento import (
    destino_vinculado,
    resolver_destino_worker,
    vincular_guild_worker,
)
from .transporte_http import post_json_worker

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
    destino = destino_vinculado(guild_id)
    selection = None
    if destino is None:
        selection = await require_music_worker_available_async()
        destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    base = destino.base
    token = destino.token
    payload = montar_comando(
        action,
        guild_id=guild_id,
        voice_channel_id=voice_channel_id,
        text_channel_id=text_channel_id,
        query=query,
        track=track,
        requester_id=requester_id,
        requester_name=requester_name,
        timeout_seconds=timeout_seconds,
        **extra,
    )
    total_timeout = max(2.0, float(payload["timeout_seconds"]) + 2.0)
    try:
        data = await post_json_worker(
            url=f"{base}/task",
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
            max_erro=400,
        )
    except Exception as exc:
        message = str(exc or "").strip() or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE
        logger.warning("[music/agent] comando remoto falhou | worker=%s action=%s erro=%s", destino.worker_id or destino.name, action, message)
        # Alguns erros de aiohttp não incluem a palavra "connection" na
        # mensagem (ex.: ClientConnectionResetError: Cannot write to closing
        # transport). Inclua o tipo para não vazar erro técnico ao usuário e
        # para o fluxo tratá-lo como indisponibilidade transitória.
        lower = f"{type(exc).__name__} {message}".lower()
        if (
            "music agent" in lower
            or "configure music_agent" in lower
            or "connection" in lower
            or "connect" in lower
            or "closing transport" in lower
            or "refused" in lower
            or "timeout" in lower
            or "pynacl" in lower
            or "davey" in lower
            or "dependency" in lower
            or "unauthorized" in lower
        ):
            message = "Sistema de música indisponível no momento: O worker está reconectando; tente novamente em alguns segundos"
        raise MusicWorkerEngineUnavailable(message[:260]) from exc
    if data.get("ok") is False:
        message = str(data.get("error") or data.get("message") or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE).strip()
        lower = message.lower()
        if "music agent" in lower or "configure music_agent" in lower or "sem token" in lower:
            message = str(getattr(config, "MUSIC_AGENT_MISSING_TOKEN_MESSAGE", MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE) or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE)
        raise MusicWorkerEngineUnavailable(message[:260])
    if int(guild_id or 0) > 0 and str(action or "").strip().lower() in {"play", "enqueue_many"}:
        vincular_guild_worker(int(guild_id), destino)
    logger.info("[music/agent] comando remoto enviado | worker=%s action=%s guild=%s", destino.worker_id or destino.name, action, guild_id)
    return data

async def music_agent_status(*, timeout_seconds: float | None = None, guild_id: int = 0, known_revision: str = "") -> dict[str, Any]:
    destino = destino_vinculado(guild_id)
    selection = None
    if destino is None:
        selection = await require_music_worker_available_async()
        destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    base = destino.base
    token = destino.token
    payload = montar_consulta_status(
        timeout_seconds=timeout_seconds,
        guild_id=guild_id,
        compact=bool(guild_id),
        known_revision=known_revision,
    )
    total_timeout = max(1.0, float(payload["timeout_seconds"]) + 1.0)
    try:
        data = await post_json_worker(
            url=f"{base}/task",
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
            max_erro=220,
        )
    except Exception as exc:
        logger.info("[music/agent] status remoto indisponível | worker=%s guild=%s erro=%s", destino.worker_id or destino.name, guild_id, exc)
        return {"ok": False, "available": False, "error": str(exc)}
    data.setdefault("ok", True)
    data.setdefault("available", bool(data.get("discord_ready")))
    return data
