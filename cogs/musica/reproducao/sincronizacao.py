from __future__ import annotations

import contextlib
from collections import deque
from typing import Any

from ..agente_telefone.conversao import faixa_do_payload
from ..nucleo.modelos import MusicTrack


def sincronizar_fila_remota(
    state: Any,
    remote: dict[str, Any],
    *,
    limite_fila: int,
    limite_historico: int,
) -> None:
    """Espelha a fila remota somente para painel e controles da VPS.

    A fila real continua pertencendo ao Phone Worker.
    """
    if not isinstance(remote, dict):
        return

    remote_queue = remote.get("queue")
    try:
        state.agent_remote_queue_size = max(0, int(remote.get("queue_size") or 0))
    except Exception:
        state.agent_remote_queue_size = 0

    if remote_queue is None and remote.get("queue_size") in (0, "0"):
        remote_queue = []
    if not isinstance(remote_queue, list):
        return

    if not state.agent_remote_queue_size:
        state.agent_remote_queue_size = len(remote_queue)
    else:
        state.agent_remote_queue_size = max(state.agent_remote_queue_size, len(remote_queue))

    mirrored: deque[MusicTrack] = deque(maxlen=limite_historico)
    for item in remote_queue[:limite_fila]:
        if isinstance(item, dict):
            track = faixa_do_payload(item)
            if track is not None:
                mirrored.append(track)

    state.forward_queue.clear()
    state.forward_queue.extend(mirrored)

    # A fila local é apenas compatibilidade visual; nunca deve assumir playback.
    with contextlib.suppress(Exception):
        while not state.queue.empty():
            state.queue.get_nowait()
            state.queue.task_done()
