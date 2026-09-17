from __future__ import annotations

from typing import Any

from cogs.musica import configuracao as config

from ..agente_telefone.comandos import music_agent_command
from ..nucleo.modelos import MusicTrack


async def enviar_controle_remoto(
    router: Any,
    action: str,
    *,
    guild_id: int,
    requester_id: int = 0,
    requester_name: str = "",
    voice_channel_id: int | None = None,
    text_channel_id: int | None = None,
    track: MusicTrack | None = None,
    query: str = "",
    create_panel: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """Envia um controle ao Phone Worker e espelha o estado retornado na VPS.

    A função não toca áudio localmente. O Phone Worker é o dono da reprodução;
    a VPS apenas invalida operações pendentes quando necessário e atualiza seu
    estado/painel a partir do snapshot autoritativo devolvido pelo agente.
    """

    action = str(action or "").strip().lower()
    if action in {"stop", "skip", "shuffle", "previous"}:
        cancel = getattr(router, "cancel_pending_music_operations", None)
        if callable(cancel):
            cancel(int(guild_id), reason=f"agent_{action}")

    result = await music_agent_command(
        action,
        guild_id=int(guild_id),
        voice_channel_id=int(voice_channel_id or 0),
        text_channel_id=int(text_channel_id or 0),
        query=str(query or ""),
        track=track,
        requester_id=int(requester_id or 0),
        requester_name=str(requester_name or ""),
        timeout_seconds=extra.pop(
            "timeout_seconds",
            getattr(config, "MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", 12.0),
        ),
        **extra,
    )

    remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
    if remote:
        sync = getattr(router, "sync_music_agent_state", None)
        if callable(sync):
            await sync(
                int(guild_id),
                track,
                remote,
                voice_channel_id=int(remote.get("voice_channel_id") or voice_channel_id or 0) or None,
                text_channel_id=int(remote.get("text_channel_id") or text_channel_id or 0) or None,
                queued=False,
                create_panel=bool(create_panel),
            )
    return result


async def pausar(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "pause", guild_id=guild_id, **kwargs)


async def retomar(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "resume", guild_id=guild_id, **kwargs)


async def pular(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "skip", guild_id=guild_id, **kwargs)


async def parar(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "stop", guild_id=guild_id, **kwargs)


async def ajustar_volume(router: Any, guild_id: int, volume_percent: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(
        router,
        "volume",
        guild_id=guild_id,
        volume_percent=int(volume_percent),
        create_panel=False,
        **kwargs,
    )


async def buscar_momento(router: Any, guild_id: int, position_seconds: float, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(
        router,
        "seek",
        guild_id=guild_id,
        position_seconds=float(position_seconds),
        **kwargs,
    )


async def embaralhar(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "shuffle", guild_id=guild_id, **kwargs)


async def alternar_repeticao(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "loop", guild_id=guild_id, **kwargs)


async def anterior(router: Any, guild_id: int, **kwargs: Any) -> dict[str, Any]:
    return await enviar_controle_remoto(router, "previous", guild_id=guild_id, **kwargs)
