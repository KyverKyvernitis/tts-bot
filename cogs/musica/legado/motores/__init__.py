"""Motores legados preservados apenas durante a migração Worker-only."""

from .base import BackendHealth, BackendSearchResult, LocalPlaybackBackend, MusicBackendAdapter
from .lavalink import LavalinkBackend
from .configuracao_lavalink import LavalinkConfigStore
from .gerenciador import MusicBackendManager

__all__ = [
    "BackendHealth",
    "BackendSearchResult",
    "LocalPlaybackBackend",
    "MusicBackendAdapter",
    "LavalinkBackend",
    "LavalinkConfigStore",
    "MusicBackendManager",
]
