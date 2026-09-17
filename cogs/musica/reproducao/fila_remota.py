from __future__ import annotations

import contextlib
import logging
from typing import Any

from cogs.musica import configuracao as config

from ..agente_telefone.conversao import faixa_do_payload
from ..agente_telefone.estado import atualizar_estado_controle_remoto
from ..nucleo.estado import MusicGuildState
from ..nucleo.modelos import LoopMode, MusicTrack
from .controle_remoto import alternar_repeticao, anterior, embaralhar

logger = logging.getLogger(__name__)


async def embaralhar_fila_worker(
    router: Any,
    guild_id: int,
    state: MusicGuildState,
    *,
    member: Any = None,
) -> tuple[bool, str]:
    """Embaralha a fila autoritativa do Phone Worker.

    A VPS não altera a reprodução nem inventa uma fila local alternativa quando
    o agente é dono da sessão; apenas envia o comando e atualiza o espelho.
    """
    try:
        result = await embaralhar(
            router,
            int(guild_id),
            track=state.current,
            requester_id=int(getattr(member, "id", 0) or 0),
            requester_name=getattr(member, "display_name", str(member)) if member is not None else "",
            voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0) or None,
            text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0) or None,
        )
        remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        state.shuffle = False
        router._schedule_panel_update(guild_id, create=False)
        if bool((result or {}).get("shuffled")):
            return True, "ok"

        refreshed = await atualizar_estado_controle_remoto(router, guild_id, create_panel=True)
        queue_size = 0
        with contextlib.suppress(Exception):
            queue_size = int(
                (refreshed or remote or {}).get("queue_size")
                or getattr(router.get_state(guild_id), "agent_remote_queue_size", 0)
                or 0
            )
        if queue_size <= 1:
            return False, "not_enough"
        return False, "remote_failed"
    except Exception:
        logger.warning("[music/agent] falha ao embaralhar queue remoto | guild=%s", guild_id, exc_info=True)
        state.shuffle = False
        router._schedule_panel_update(guild_id, create=False)
        return False, "remote_failed"


async def alternar_repeticao_worker(
    router: Any,
    guild_id: int,
    state: MusicGuildState,
    *,
    member: Any = None,
) -> LoopMode:
    """Alterna o loop no Phone Worker e aplica apenas o estado confirmado."""
    try:
        result = await alternar_repeticao(
            router,
            int(guild_id),
            track=state.current,
            requester_id=int(getattr(member, "id", 0) or 0),
            requester_name=getattr(member, "display_name", str(member)) if member is not None else "",
            voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0) or None,
            text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0) or None,
        )
        remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        mode_value = str((result or {}).get("mode") or (remote or {}).get("loop_mode") or "").strip().lower()
        if mode_value in {"off", "one", "all"}:
            state.loop_mode = LoopMode(mode_value)
        router._schedule_panel_update(guild_id, create=False)
        return state.loop_mode
    except Exception:
        logger.warning("[music/agent] falha ao alternar repetição remota | guild=%s", guild_id, exc_info=True)
        router._schedule_panel_update(guild_id, create=False)
        return state.loop_mode


async def voltar_historico_worker(router: Any, guild_id: int, state: MusicGuildState) -> bool:
    """Volta uma faixa usando o histórico autoritativo do Phone Worker."""
    with contextlib.suppress(Exception):
        await atualizar_estado_controle_remoto(router, guild_id, create_panel=False)
        state = router.get_state(guild_id)

    remote_has_history = bool(int(getattr(state, "agent_remote_history_size", 0) or 0) > 0)
    local_fallback: MusicTrack | None = state.history[-1] if state.history else None
    if local_fallback is None and not remote_has_history:
        return False
    if not int(getattr(state, "last_voice_channel_id", 0) or 0):
        return False

    payload: dict[str, Any] = {
        "voice_channel_id": int(getattr(state, "last_voice_channel_id", 0) or 0),
        "text_channel_id": int(getattr(state, "last_text_channel_id", 0) or 0),
        "timeout_seconds": getattr(config, "MUSIC_AGENT_COMMAND_TIMEOUT_SECONDS", 12.0),
    }
    if not remote_has_history and local_fallback is not None:
        payload.update(
            {
                "query": local_fallback.webpage_url
                or local_fallback.original_url
                or local_fallback.stream_url
                or local_fallback.title,
                "track": local_fallback,
            }
        )

    try:
        result = await anterior(
            router,
            int(guild_id),
            voice_channel_id=payload.get("voice_channel_id"),
            text_channel_id=payload.get("text_channel_id"),
            track=payload.get("track"),
            query=payload.get("query", ""),
            timeout_seconds=payload.get("timeout_seconds"),
            create_panel=False,
        )
    except Exception:
        logger.warning("[music/agent] falha ao voltar histórico pelo worker | guild=%s", guild_id, exc_info=True)
        return False

    if not bool(result.get("ok", True)):
        remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        if remote:
            with contextlib.suppress(Exception):
                await router.sync_music_agent_state(
                    int(guild_id),
                    None,
                    remote,
                    voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0),
                    text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
                    queued=False,
                    create_panel=False,
                )
        return False

    if not remote_has_history and local_fallback is not None and state.history:
        with contextlib.suppress(Exception):
            if state.history[-1] is local_fallback:
                state.history.pop()

    state.stop_requested = False
    state.skip_requested = False
    state.skip_transition_active = True
    router._clear_idle_reason(state)
    router._cancel_music_idle_disconnect(state)

    remote = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
    previous_payload = result.get("previous") if isinstance(result, dict) and isinstance(result.get("previous"), dict) else {}
    previous_track = faixa_do_payload(previous_payload, local_fallback) if previous_payload else local_fallback
    await router.sync_music_agent_state(
        int(guild_id),
        previous_track,
        remote,
        voice_channel_id=int(getattr(state, "last_voice_channel_id", 0) or 0),
        text_channel_id=int(getattr(state, "last_text_channel_id", 0) or 0),
        queued=False,
        create_panel=True,
    )
    return True
