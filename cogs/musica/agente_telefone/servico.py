"""Fachada de compatibilidade do domínio Phone Worker.

A implementação é separada por responsabilidade. Novos consumidores devem
preferir os módulos específicos; este arquivo preserva os imports existentes
durante a modularização.
"""

from .comandos import music_agent_command, music_agent_status
from .modelos import (
    MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE,
    MUSIC_WORKER_NO_CAPACITY_MESSAGE,
    MUSIC_WORKER_UNAVAILABLE_MESSAGE,
    MusicWorkerEngineUnavailable,
    MusicWorkerSelection,
    MusicWorkerUnavailable,
)
from .resolucao import resolve_music_tracks_on_worker
from .selecao import (
    ensure_music_worker_available,
    music_worker_only_enabled,
    require_music_worker_available,
    require_music_worker_available_async,
    select_music_worker,
    select_music_worker_async,
    worker_music_agent_summary,
    worker_music_summary,
)

__all__ = [
    "MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE",
    "MUSIC_WORKER_NO_CAPACITY_MESSAGE",
    "MUSIC_WORKER_UNAVAILABLE_MESSAGE",
    "MusicWorkerEngineUnavailable",
    "MusicWorkerSelection",
    "MusicWorkerUnavailable",
    "ensure_music_worker_available",
    "music_agent_command",
    "music_agent_status",
    "music_worker_only_enabled",
    "require_music_worker_available",
    "require_music_worker_available_async",
    "resolve_music_tracks_on_worker",
    "select_music_worker",
    "select_music_worker_async",
    "worker_music_agent_summary",
    "worker_music_summary",
]
