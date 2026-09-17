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
