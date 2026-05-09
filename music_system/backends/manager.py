from __future__ import annotations

import logging
from typing import Any

import config

from .base import BackendHealth, BackendSearchResult, LocalPlaybackBackend
from .lavalink import LavalinkBackend

logger = logging.getLogger(__name__)


class MusicBackendManager:
    """Gerenciador dos backends de música.

    Patch 1 mantém o backend real como local. Lavalink fica disponível apenas
    para diagnóstico/teste, preparando a migração sem alterar o fluxo atual.
    """

    def __init__(self, bot, extractor) -> None:
        self.bot = bot
        self.extractor = extractor
        self.local = LocalPlaybackBackend(extractor)
        self.lavalink = LavalinkBackend.from_config()
        self.mode = str(getattr(config, "MUSIC_BACKEND", "local") or "local").strip().lower()

    @property
    def active_backend_name(self) -> str:
        # Segurança do Patch 1: reprodução real continua sempre local.
        return "local"

    async def close(self) -> None:
        await self.lavalink.close()
        await self.local.close()

    async def status(self) -> dict[str, BackendHealth]:
        local = await self.local.health()
        lavalink = await self.lavalink.health()
        return {"local": local, "lavalink": lavalink}

    async def test_lavalink(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> BackendSearchResult:
        return await self.lavalink.search(query, requester_id=requester_id, requester_name=requester_name)

    def compact_runtime_summary(self) -> dict[str, Any]:
        return {
            "configured_backend": self.mode,
            "active_backend": self.active_backend_name,
            "lavalink_mode": self.lavalink.cfg.mode,
            "lavalink_enabled": self.lavalink.cfg.enabled,
            "lavalink_configured": self.lavalink.cfg.configured,
        }
