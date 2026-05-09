from __future__ import annotations

import logging
from typing import Any

import config

from .base import BackendHealth, BackendSearchResult, LocalPlaybackBackend
from .lavalink import LavalinkBackend, LavalinkConfig
from .lavalink_config import LavalinkConfigStore

logger = logging.getLogger(__name__)


class MusicBackendManager:
    """Gerenciador dos backends de música.

    Patch atual mantém o backend real como local. Lavalink pode ser configurado
    pelo painel `_musicnode` e fica disponível apenas para diagnóstico/teste,
    preparando a migração sem alterar o fluxo atual de playback.
    """

    def __init__(self, bot, extractor) -> None:
        self.bot = bot
        self.extractor = extractor
        self.local = LocalPlaybackBackend(extractor)
        self.lavalink_store = LavalinkConfigStore()
        self.lavalink = LavalinkBackend.from_config(self.lavalink_store.load())
        self.mode = str(getattr(config, "MUSIC_BACKEND", "local") or "local").strip().lower()

    @property
    def active_backend_name(self) -> str:
        # Segurança deste patch: reprodução real continua sempre local.
        return "local"

    async def close(self) -> None:
        await self.lavalink.close()
        await self.local.close()

    async def _replace_lavalink(self, cfg: LavalinkConfig) -> None:
        old = self.lavalink
        self.lavalink = LavalinkBackend.from_config(cfg)
        try:
            await old.close()
        except Exception:
            logger.debug("[music/lavalink] falha ao fechar sessão antiga do backend", exc_info=True)

    async def update_lavalink_node(
        self,
        *,
        node_name: str,
        host: str,
        port: int,
        password: str | None,
        secure: bool,
    ) -> LavalinkConfig:
        cfg = self.lavalink_store.update_node(
            node_name=node_name,
            host=host,
            port=port,
            password=password,
            secure=secure,
        )
        await self._replace_lavalink(cfg)
        return cfg

    async def set_lavalink_mode(self, mode: str) -> LavalinkConfig:
        cfg = self.lavalink_store.set_mode(mode)
        await self._replace_lavalink(cfg)
        return cfg

    async def clear_lavalink_config(self) -> LavalinkConfig:
        cfg = self.lavalink_store.clear()
        await self._replace_lavalink(cfg)
        return cfg

    async def update_lavalink_panel_options(self, **options: Any) -> dict[str, bool]:
        return self.lavalink_store.update_options(**options)

    def lavalink_config_summary(self) -> dict[str, Any]:
        return self.lavalink_store.summary()

    async def status(self) -> dict[str, BackendHealth]:
        local = await self.local.health()
        lavalink = await self.lavalink.health()
        return {"local": local, "lavalink": lavalink}

    async def test_lavalink(self, query: str, *, requester_id: int = 0, requester_name: str = "") -> BackendSearchResult:
        return await self.lavalink.search(query, requester_id=requester_id, requester_name=requester_name)

    def compact_runtime_summary(self) -> dict[str, Any]:
        cfg_summary = self.lavalink_config_summary()
        return {
            "configured_backend": self.mode,
            "active_backend": self.active_backend_name,
            "lavalink_mode": self.lavalink.cfg.mode,
            "lavalink_enabled": self.lavalink.cfg.enabled,
            "lavalink_configured": self.lavalink.cfg.configured,
            "lavalink_config_source": cfg_summary.get("source", "padrão"),
        }
