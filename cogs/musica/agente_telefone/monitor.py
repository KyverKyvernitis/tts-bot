from __future__ import annotations

import asyncio
import logging
from typing import Any

import config

from .comandos import music_agent_status
from .conversao import estado_da_guild_no_payload

logger = logging.getLogger(__name__)


def iniciar_monitor_music_agent(
    router: Any,
    guild_id: int,
    *,
    voice_channel_id: int | None = None,
    text_channel_id: int | None = None,
    callback_conclusao: Any | None = None,
) -> None:
    """Mantém o espelho da VPS atualizado enquanto o Phone Worker toca."""
    guild_id = int(guild_id)
    state = router.get_state(guild_id)
    task = getattr(state, "agent_monitor_task", None)
    if task is not None and not task.done():
        return

    async def _runner() -> None:
        idle_seen = 0
        try:
            while True:
                await asyncio.sleep(max(1.0, min(4.0, float(getattr(config, "MUSIC_AGENT_PANEL_POLL_SECONDS", 2.0) or 2.0))))
                try:
                    payload = await music_agent_status(timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0))
                except Exception:
                    logger.debug("[music/agent] monitor não conseguiu consultar status | guild=%s", guild_id, exc_info=True)
                    continue
                remote = estado_da_guild_no_payload(payload, guild_id)
                if not remote:
                    idle_seen += 1
                    if idle_seen >= 4:
                        return
                    continue
                await router.sync_music_agent_state(
                    guild_id,
                    None,
                    remote,
                    voice_channel_id=voice_channel_id,
                    text_channel_id=text_channel_id,
                    queued=False,
                    create_panel=True,
                )
                status = str(remote.get("status") or "").lower()
                has_current = isinstance(remote.get("current"), dict) and bool(remote.get("current"))
                idle_seen = idle_seen + 1 if status in {"idle", "stopped", "failed", "error"} and not has_current else 0
                if idle_seen >= 3:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[music/agent] monitor encerrado por falha", exc_info=True)
        finally:
            st = router.get_state(guild_id)
            if getattr(st, "agent_monitor_task", None) is asyncio.current_task():
                st.agent_monitor_task = None

    try:
        task = asyncio.create_task(_runner())
        if callable(callback_conclusao):
            task.add_done_callback(callback_conclusao)
        else:
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        state.agent_monitor_task = task
    except RuntimeError:
        state.agent_monitor_task = None
