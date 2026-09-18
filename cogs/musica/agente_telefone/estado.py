from __future__ import annotations

from typing import Any

from cogs.musica import configuracao as config

from .comandos import music_agent_status
from .conversao import estado_da_guild_no_payload


def usar_controles_fila_remota(router: Any, state: Any) -> bool:
    """Decide se fila/histórico devem obedecer ao estado autoritativo do Phone Worker."""
    if not (
        bool(getattr(config, "MUSIC_AGENT_ENABLED", True))
        and bool(router.music_worker_only_enabled())
    ):
        return False
    backend = str(getattr(state, "current_backend", "") or "").lower()
    if backend == "agent":
        return True
    try:
        if int(getattr(state, "agent_remote_queue_size", 0) or 0) > 0:
            return True
    except Exception:
        pass
    return bool(
        getattr(state, "last_voice_channel_id", 0)
        and getattr(state, "current", None) is not None
    )


async def atualizar_estado_controle_remoto(
    router: Any,
    guild_id: int,
    *,
    voice_channel_id: int | None = None,
    text_channel_id: int | None = None,
    create_panel: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Consulta rapidamente o Phone Worker e atualiza o espelho local da guild."""
    try:
        timeout = timeout_seconds
        if timeout is None:
            timeout = min(
                2.5,
                float(
                    getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0)
                    or 5.0
                ),
            )
        payload = await music_agent_status(timeout_seconds=timeout, guild_id=int(guild_id))
    except Exception:
        return {}

    remote = estado_da_guild_no_payload(payload, int(guild_id))
    if not remote:
        return {}

    state = router.get_state(int(guild_id))
    try:
        voice_channel_id = int(
            voice_channel_id
            or remote.get("voice_channel_id")
            or getattr(state, "last_voice_channel_id", 0)
            or 0
        ) or None
    except Exception:
        voice_channel_id = None
    try:
        text_channel_id = int(
            text_channel_id
            or remote.get("text_channel_id")
            or getattr(state, "last_text_channel_id", 0)
            or 0
        ) or None
    except Exception:
        text_channel_id = None

    await router.sync_music_agent_state(
        int(guild_id),
        state.current,
        remote,
        voice_channel_id=voice_channel_id,
        text_channel_id=text_channel_id,
        queued=False,
        create_panel=create_panel,
    )
    return remote
