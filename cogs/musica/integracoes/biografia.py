from __future__ import annotations

import logging
from typing import Any, Callable

LOG = logging.getLogger(__name__)


def coletar_players_ativos(bot: Any, formatar_inteiro: Callable[[int], str] = str) -> str:
    """Coleta a métrica musical usada pela biografia do aplicativo."""
    try:
        router = getattr(bot, "audio_router", None)
        contador = getattr(router, "_active_player_count", None)
        if callable(contador):
            return formatar_inteiro(int(contador()))
    except Exception:
        LOG.debug("falha ao ler players ativos", exc_info=True)
    return "0"
