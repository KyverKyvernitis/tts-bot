from __future__ import annotations

import contextlib
from typing import Any


def _roteador(bot: Any):
    return getattr(bot, "audio_router", None) if bot is not None else None


def musica_ativa(bot: Any, guild_id: int) -> bool:
    router = _roteador(bot)
    metodo = getattr(router, "is_music_active", None)
    if not callable(metodo):
        return False
    with contextlib.suppress(Exception):
        return bool(metodo(int(guild_id)))
    return False


def lavalink_ativo(bot: Any, guild_id: int) -> bool:
    """Compatibilidade temporária enquanto o backend legado ainda existe."""
    router = _roteador(bot)
    metodo = getattr(router, "is_lavalink_active_for_guild", None)
    if not callable(metodo):
        return False
    with contextlib.suppress(Exception):
        return bool(metodo(int(guild_id)))
    return False


def eh_cliente_voz_lavalink(vc: Any) -> bool:
    """Reconhece o voice client Wavelink sem espalhar esse detalhe pelo TTS."""
    if vc is None:
        return False
    module = str(getattr(type(vc), "__module__", "") or "")
    qualname = str(getattr(type(vc), "__qualname__", "") or getattr(type(vc), "__name__", "") or "")
    return module.startswith("wavelink") or (
        qualname == "Player" and hasattr(vc, "node") and hasattr(vc, "play")
    )


def deve_bloquear_voz_tts_local(bot: Any, guild_id: int) -> bool:
    router = _roteador(bot)
    metodo = getattr(router, "should_block_tts_local_voice", None)
    if callable(metodo):
        with contextlib.suppress(Exception):
            return bool(metodo(int(guild_id)))
    return lavalink_ativo(bot, guild_id)


def deve_adiar_auto_leave_tts(bot: Any, guild_id: int) -> bool:
    router = _roteador(bot)
    metodo = getattr(router, "should_defer_tts_auto_leave", None)
    if not callable(metodo):
        return False
    with contextlib.suppress(Exception):
        return bool(metodo(int(guild_id)))
    return False


async def atualizar_ocupacao_ou_agendar_idle(
    bot: Any,
    guild_id: int,
    *,
    auto_leave_enabled: bool,
    apenas_bots_ou_vazio: bool,
) -> bool:
    """Aplica a política musical de auto-leave e informa se o TTS deve adiar."""
    router = _roteador(bot)
    if router is None or not deve_adiar_auto_leave_tts(bot, guild_id):
        return False

    occupancy_update = getattr(router, "handle_music_voice_occupancy_update", None)
    if callable(occupancy_update):
        with contextlib.suppress(Exception):
            await occupancy_update(int(guild_id), auto_leave_enabled=bool(auto_leave_enabled))
            return True

    if apenas_bots_ou_vazio:
        schedule_idle = getattr(router, "schedule_music_idle_disconnect", None)
        if callable(schedule_idle):
            with contextlib.suppress(Exception):
                await schedule_idle(int(guild_id))
    return True


async def agendar_idle_musica(bot: Any, guild_id: int) -> None:
    router = _roteador(bot)
    metodo = getattr(router, "schedule_music_idle_disconnect", None)
    if callable(metodo):
        with contextlib.suppress(Exception):
            await metodo(int(guild_id))


def deve_rotear_tts_para_agente(bot: Any, guild_id: int, channel_id: int) -> bool:
    router = _roteador(bot)
    metodo = getattr(router, "should_route_tts_to_music_agent", None)
    if not callable(metodo):
        return False
    with contextlib.suppress(Exception):
        return bool(metodo(int(guild_id), int(channel_id)))
    return False


def suporta_cache_tts_agente(bot: Any) -> bool:
    return bool(getattr(_roteador(bot), "_supports_tts_cached_audio", False))


async def tocar_tts_via_agente(bot: Any, **kwargs):
    router = _roteador(bot)
    metodo = getattr(router, "play_tts_via_music_agent", None)
    if not callable(metodo):
        raise RuntimeError("integração TTS/Music Agent indisponível")
    return await metodo(**kwargs)


def erro_agente_permite_fallback_local(bot: Any, guild_id: int, exc: Exception | str) -> bool:
    texto = str(exc or "").lower()
    if not musica_ativa(bot, guild_id):
        return True
    return any(
        marcador in texto
        for marcador in (
            "sem sessão musical ativa",
            "no active music",
            "no music session",
            "music session",
        )
    )


def roteador_suporta_tts(bot: Any) -> bool:
    return callable(getattr(_roteador(bot), "play_tts", None))


async def tocar_tts_via_roteador(bot: Any, **kwargs):
    router = _roteador(bot)
    metodo = getattr(router, "play_tts", None)
    if not callable(metodo):
        return None
    return await metodo(**kwargs)


async def preparar_fallback_local_apos_lavalink(bot: Any, guild: Any, vc: Any, *, reason: str):
    router = _roteador(bot)
    metodo = getattr(router, "prepare_tts_local_fallback_after_lavalink_failure", None)
    if not callable(metodo):
        return None
    return await metodo(guild, vc, reason=reason)


async def cancelar_tts_remoto(owner: Any, item: Any) -> None:
    """Cancela um overlay TTS ativo no Music Agent sem expor o protocolo ao TTS."""
    if not getattr(item, "_tts_remote_active", False):
        return
    item._tts_remote_active = False
    requester = getattr(owner, "_request_phone_worker_json", None)
    if not callable(requester):
        return
    with contextlib.suppress(Exception):
        await requester(
            task="music_agent_command",
            payload={
                "action": "cancel_tts",
                "guild_id": int(getattr(item, "guild_id", 0) or 0),
                "tts_request_id": getattr(item, "request_id", None),
                "timeout_seconds": 2,
            },
            timeout_seconds=2,
            max_audio_mb=1,
            raise_on_worker_error=False,
        )


def bloqueio_tts_direto_por_musica(bot: Any, guild_id: int, voice_agent: Any) -> str | None:
    """Retorna o motivo musical que impede a rota TTS direta, se houver."""
    if musica_ativa(bot, guild_id):
        return "music_active_uses_music_agent_tts_route"
    if isinstance(voice_agent, dict) and voice_agent and voice_agent.get("music_ready") is False:
        return "music_agent_not_ready"
    return None


async def rotear_item_tts_para_musica(
    owner: Any,
    guild: Any,
    item: Any,
    audio_task: Any,
) -> tuple[bool, Any]:
    """Tenta reproduzir um item TTS pela sessão musical ativa.

    Retorna ``(consumido, proxima_audio_task)``. Quando o Music Agent assume o
    item, toda a política de cache, overlay, métricas e fallback fica neste
    domínio, evitando que ``cogs/tts`` implemente detalhes de música.
    """
    import asyncio
    import logging
    import time

    logger = logging.getLogger("cogs.musica.integracoes.tts")
    bot = getattr(owner, "bot", None)
    guild_id = int(getattr(guild, "id", 0) or 0)
    channel_id = int(getattr(item, "channel_id", 0) or 0)
    if bool(getattr(item, "_skip_music_agent_tts_route", False)):
        return False, audio_task
    if not deve_rotear_tts_para_agente(bot, guild_id, channel_id):
        return False, audio_task

    if audio_task is not None and not audio_task.done():
        audio_task.cancel()
        with contextlib.suppress(BaseException):
            await audio_task
    elif audio_task is not None and not audio_task.cancelled():
        with contextlib.suppress(Exception):
            routed_path, routed_cleanup = audio_task.result()
            if routed_cleanup and routed_path:
                await owner._discard_edge_stream_path(routed_path)
    audio_task = None

    dequeue_started_at = float(getattr(item, "_dequeued_at_monotonic", time.monotonic()))
    try:
        cached_payload: dict[str, Any] = {}
        if suporta_cache_tts_agente(bot):
            await owner._maybe_attach_prebuilt_direct_tts_audio(
                cached_payload,
                item,
                generate_if_missing=True,
            )
            cached_payload.setdefault("cache_key", owner._cache_key(item))
            cached_payload["tld"] = getattr(item, "tld", None)
            cached_payload["tts_request_id"] = getattr(item, "request_id", None)
            item._tts_remote_active = True

        playback_result = await tocar_tts_via_agente(
            bot,
            guild_id=guild_id,
            channel_id=channel_id,
            text=getattr(item, "text", ""),
            **cached_payload,
            engine=getattr(item, "engine", "gtts"),
            voice=getattr(item, "voice", ""),
            language=getattr(item, "language", "pt-br"),
            rate=getattr(item, "rate", "+0%"),
            pitch=getattr(item, "pitch", "+0Hz"),
            timeout=owner._estimate_playback_timeout(item),
        )
        item._tts_remote_active = False
        playback_started_at = (
            float(playback_result.get("playback_started_at", time.monotonic()) or time.monotonic())
            if isinstance(playback_result, dict)
            else time.monotonic()
        )
        queue_wait_ms = max(
            0.0,
            (dequeue_started_at - float(getattr(item, "enqueued_at_monotonic", dequeue_started_at))) * 1000.0,
        )
        dispatch_ms = max(0.0, (playback_started_at - dequeue_started_at) * 1000.0)
        playback_ms = (
            max(0.0, float((playback_result or {}).get("playback_ms", 0.0) or 0.0))
            if isinstance(playback_result, dict)
            else 0.0
        )
        owner._record_queue_timing(
            queue_wait_ms=queue_wait_ms,
            dispatch_ms=dispatch_ms,
            source_setup_ms=0.0,
            play_call_ms=0.0,
            playback_ms=playback_ms,
            total_to_playback_ms=max(
                0.0,
                (playback_started_at - float(getattr(item, "enqueued_at_monotonic", playback_started_at))) * 1000.0,
            ),
        )
        owner._schedule_worker_voice_agent_register_session(
            guild,
            item,
            None,
            source="tts_music_agent_route",
        )
        logger.info(
            "[tts_voice] TTS roteado pelo worker musical | guild=%s channel=%s engine=%s ok=%s",
            guild_id,
            channel_id,
            getattr(item, "engine", None),
            bool(isinstance(playback_result, dict) and playback_result.get("ok", True)),
        )
        return True, audio_task
    except asyncio.CancelledError:
        item._tts_remote_active = False
        raise
    except Exception as exc:
        item._tts_remote_active = False
        safe_to_fallback = erro_agente_permite_fallback_local(bot, guild_id, exc)
        if safe_to_fallback:
            setattr(item, "_skip_music_agent_tts_route", True)
            logger.warning(
                "[tts_voice] TTS do worker musical falhou; seguindo fallback seguro | guild=%s channel=%s erro=%s",
                guild_id,
                channel_id,
                exc,
            )
            return False, audio_task
        logger.warning(
            "[tts_voice] TTS do worker musical falhou; mantendo música ativa e descartando TTS para não interromper | guild=%s channel=%s erro=%s",
            guild_id,
            channel_id,
            exc,
        )
        return True, audio_task
