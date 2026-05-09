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
        self._last_lavalink_shadow: dict[int, BackendSearchResult] = {}

    @property
    def active_backend_name(self) -> str:
        # Segurança deste patch: reprodução real continua sempre local.
        return "local"

    def _guild_key(self, guild_id: int | None) -> int:
        try:
            return int(guild_id or 0)
        except Exception:
            return 0

    def should_shadow_lavalink(self, guild_id: int | None = None) -> bool:
        """Retorna se o Lavalink deve ser consultado em paralelo ao player local.

        Mesmo quando o modo escolhido é ``lavalink`` ou ``auto``, este patch
        ainda mantém o áudio real no backend local. Por isso esses modos também
        são tratados como shadow para diagnóstico seguro até a ativação real.
        """
        try:
            cfg = self.lavalink_store.load(guild_id=guild_id)
        except Exception:
            logger.debug("[music/lavalink-shadow] falha ao ler config", exc_info=True)
            return False
        return bool(cfg.enabled and cfg.configured and cfg.mode in {"shadow", "lavalink", "auto"})

    def last_lavalink_shadow_result(self, guild_id: int | None = None) -> BackendSearchResult | None:
        return getattr(self, "_last_lavalink_shadow", {}).get(self._guild_key(guild_id))

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
        guild_id: int | None = None,
    ) -> LavalinkConfig:
        cfg = self.lavalink_store.update_node(
            node_name=node_name,
            host=host,
            port=port,
            password=password,
            secure=secure,
            guild_id=guild_id,
        )
        await self._replace_lavalink(cfg)
        return cfg

    async def set_lavalink_mode(self, mode: str, *, guild_id: int | None = None) -> LavalinkConfig:
        cfg = self.lavalink_store.set_mode(mode, guild_id=guild_id)
        await self._replace_lavalink(cfg)
        return cfg

    async def clear_lavalink_config(self, *, guild_id: int | None = None) -> LavalinkConfig:
        cfg = self.lavalink_store.clear(guild_id=guild_id)
        await self._replace_lavalink(cfg)
        return cfg

    async def update_lavalink_panel_options(self, **options: Any) -> dict[str, bool]:
        return self.lavalink_store.update_options(**options)

    def lavalink_config_summary(self, guild_id: int | None = None) -> dict[str, Any]:
        return self.lavalink_store.summary(guild_id=guild_id)

    async def status(self, guild_id: int | None = None) -> dict[str, BackendHealth]:
        local = await self.local.health()
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=guild_id))
        try:
            lavalink = await lavalink_backend.health()
        finally:
            await lavalink_backend.close()
        return {"local": local, "lavalink": lavalink}

    async def test_lavalink(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        guild_id: int | None = None,
    ) -> BackendSearchResult:
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=guild_id))
        try:
            result = await lavalink_backend.search(query, requester_id=requester_id, requester_name=requester_name)
            result.extra.setdefault("origin", "manual")
            return result
        finally:
            await lavalink_backend.close()

    async def shadow_lavalink_search(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        guild_id: int | None = None,
        reason: str = "play",
    ) -> BackendSearchResult | None:
        if not self.should_shadow_lavalink(guild_id):
            return None
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=guild_id))
        try:
            result = await lavalink_backend.search(query, requester_id=requester_id, requester_name=requester_name)
            result.extra.setdefault("origin", "shadow")
            result.extra.setdefault("reason", str(reason or "play"))
            result.extra.setdefault("guild_id", self._guild_key(guild_id))
            self._last_lavalink_shadow[self._guild_key(guild_id)] = result
            if result.ok:
                logger.info(
                    "[music/lavalink-shadow] OK | guild=%s query=%r tracks=%s fonte=%s latencia=%sms",
                    guild_id,
                    query,
                    result.tracks_found,
                    result.first_source or "-",
                    result.latency_ms,
                )
            else:
                logger.info(
                    "[music/lavalink-shadow] falhou | guild=%s query=%r detalhe=%s latencia=%sms",
                    guild_id,
                    query,
                    result.message,
                    result.latency_ms,
                )
            return result
        except Exception:
            logger.debug("[music/lavalink-shadow] busca paralela falhou", exc_info=True)
            return None
        finally:
            await lavalink_backend.close()

    def compact_runtime_summary(self, guild_id: int | None = None) -> dict[str, Any]:
        cfg_summary = self.lavalink_config_summary(guild_id=guild_id)
        cfg = self.lavalink_store.load(guild_id=guild_id)
        return {
            "configured_backend": self.mode,
            "active_backend": self.active_backend_name,
            "lavalink_mode": cfg.mode,
            "lavalink_enabled": cfg.enabled,
            "lavalink_configured": cfg.configured,
            "lavalink_config_source": cfg_summary.get("source", "padrão"),
            "lavalink_guild_override": bool(cfg_summary.get("guild_override")),
            "lavalink_shadow_active": self.should_shadow_lavalink(guild_id),
        }
