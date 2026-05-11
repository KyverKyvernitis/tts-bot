from __future__ import annotations

import contextlib
import logging
from typing import Any

import config

LAVALINK_REAL_TEST_GUILD_ID = 927002914449424404

from .base import BackendHealth, BackendSearchResult, LocalPlaybackBackend
from ..models import ExtractedBatch
from .lavalink import LavalinkBackend, LavalinkConfig
from .lavalink_config import LavalinkConfigStore

logger = logging.getLogger(__name__)


class MusicBackendManager:
    """Gerenciador dos backends de música.

    Lavalink agora pode ser usado como backend real em qualquer servidor com
    node configurado e modo ``lavalink``/``auto``. O backend local permanece
    apenas como fallback controlado do modo ``auto``.
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
        return self.playback_backend_for_guild(None)

    def _guild_key(self, guild_id: int | None) -> int:
        try:
            return int(guild_id or 0)
        except Exception:
            return 0

    def _real_lavalink_guild_ids(self) -> set[int]:
        """Lista opcional de restrição herdada dos testes antigos.

        Por padrão não há allowlist: qualquer servidor com modo Lavalink/Auto e
        node configurado pode usar o backend real. Se o dono quiser voltar a
        limitar temporariamente, basta definir MUSIC_LAVALINK_REAL_GUILD_IDS.
        """
        raw = getattr(config, "MUSIC_LAVALINK_REAL_GUILD_IDS", None)
        if raw in (None, "", [], (), set()):
            return set()
        if isinstance(raw, (str, int)):
            raw = [raw]
        ids: set[int] = set()
        for value in raw or []:
            try:
                guild_id = int(str(value).strip())
            except Exception:
                continue
            if guild_id > 0:
                ids.add(guild_id)
        return ids

    def is_lavalink_real_allowed_guild(self, guild_id: int | None) -> bool:
        allowed_ids = self._real_lavalink_guild_ids()
        return not allowed_ids or self._guild_key(guild_id) in allowed_ids

    def should_use_lavalink_real(self, guild_id: int | None = None) -> bool:
        """Ativa playback real via Lavalink para qualquer guild configurada.

        Requisitos: node salvo, modo ``lavalink`` ou ``auto`` e Wavelink
        instalado. A allowlist por guild é opcional e só entra se o env antigo
        MUSIC_LAVALINK_REAL_GUILD_IDS estiver definido.
        """
        try:
            cfg = self.lavalink_store.load(guild_id=guild_id)
        except Exception:
            logger.debug("[music/lavalink] falha ao ler config real", exc_info=True)
            return False
        if not (cfg.enabled and cfg.configured and cfg.mode in {"lavalink", "auto"}):
            return False
        if not self.is_lavalink_real_allowed_guild(guild_id):
            return False
        return bool(self.lavalink._wavelink_installed())

    def playback_backend_for_guild(self, guild_id: int | None = None) -> str:
        return "lavalink" if self.should_use_lavalink_real(guild_id) else "local"

    def lavalink_mode_for_guild(self, guild_id: int | None = None) -> str:
        try:
            cfg = self.lavalink_store.load(guild_id=guild_id)
            return str(getattr(cfg, "mode", "off") or "off").strip().lower()
        except Exception:
            logger.debug("[music/lavalink] falha ao ler modo da guild", exc_info=True)
            return "off"

    def should_lavalink_fallback_to_local(self, guild_id: int | None = None) -> bool:
        # Modo lavalink é lavalink-only: se o node falhar, não mistura com voice client local.
        # Modo auto pode cair para o backend local antes de iniciar áudio real.
        return self.lavalink_mode_for_guild(guild_id) == "auto"

    def should_shadow_lavalink(self, guild_id: int | None = None) -> bool:
        """Retorna se o Lavalink deve ser consultado em paralelo ao player local.

        Shadow continua existindo apenas para diagnóstico manual. Em modo
        ``lavalink``/``auto`` o backend real já é usado quando configurado.
        """
        try:
            cfg = self.lavalink_store.load(guild_id=guild_id)
        except Exception:
            logger.debug("[music/lavalink-shadow] falha ao ler config", exc_info=True)
            return False
        if self.should_use_lavalink_real(guild_id):
            return False
        return bool(cfg.enabled and cfg.configured and cfg.mode == "shadow")

    def last_lavalink_shadow_result(self, guild_id: int | None = None) -> BackendSearchResult | None:
        return getattr(self, "_last_lavalink_shadow", {}).get(self._guild_key(guild_id))

    async def close(self) -> None:
        await self.lavalink.close()
        await self.lavalink.close_wavelink_pool()
        await self.local.close()

    async def _replace_lavalink(self, cfg: LavalinkConfig) -> None:
        old = self.lavalink
        self.lavalink = LavalinkBackend.from_config(cfg)
        try:
            await old.close()
            await old.close_wavelink_pool()
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

    async def search_lavalink_tracks(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        guild_id: int | None = None,
        limit: int = 5,
    ) -> ExtractedBatch:
        if not self.should_use_lavalink_real(guild_id):
            raise RuntimeError("Playback real via Lavalink não está ativo para este servidor. Use `_musicnode` no modo Lavalink ou Auto.")
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=guild_id))
        try:
            return await lavalink_backend.extract_tracks_for_selection(
                self.bot,
                query,
                requester_id=requester_id,
                requester_name=requester_name,
                limit=limit,
            )
        finally:
            with contextlib.suppress(Exception):
                await lavalink_backend.close()

    async def resolve_lavalink_direct_tracks(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        guild_id: int | None = None,
        limit: int = 25,
    ) -> ExtractedBatch:
        if not self.should_use_lavalink_real(guild_id):
            raise RuntimeError("Playback real via Lavalink não está ativo para este servidor. Use `_musicnode` no modo Lavalink ou Auto.")
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=guild_id))
        try:
            return await lavalink_backend.extract_direct_tracks(
                self.bot,
                query,
                requester_id=requester_id,
                requester_name=requester_name,
                limit=limit,
            )
        finally:
            with contextlib.suppress(Exception):
                await lavalink_backend.close()

    async def play_lavalink_track(self, guild, voice_channel, track, *, volume: float = 1.0):
        if not self.should_use_lavalink_real(getattr(guild, "id", None)):
            raise RuntimeError("Playback real via Lavalink não está ativo para este servidor. Use `_musicnode` no modo Lavalink ou Auto.")
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=getattr(guild, "id", None)))
        try:
            return await lavalink_backend.play_track(self.bot, guild, voice_channel, track, volume=volume)
        finally:
            with contextlib.suppress(Exception):
                await lavalink_backend.close()


    async def play_lavalink_tts(
        self,
        guild,
        *,
        candidates: list[str],
        volume: float = 1.0,
        resume_volume: float = 1.0,
        resume_playable: Any | None = None,
        timeout: float = 120.0,
        should_resume=None,
    ) -> dict[str, Any]:
        if not self.should_use_lavalink_real(getattr(guild, "id", None)):
            raise RuntimeError("Playback real via Lavalink não está ativo para este servidor. Use `_musicnode` no modo Lavalink ou Auto.")
        lavalink_backend = LavalinkBackend.from_config(self.lavalink_store.load(guild_id=getattr(guild, "id", None)))
        try:
            return await lavalink_backend.play_tts_interrupt(
                self.bot,
                guild,
                candidates=candidates,
                volume=volume,
                resume_volume=resume_volume,
                resume_playable=resume_playable,
                timeout=timeout,
                should_resume=should_resume,
            )
        finally:
            with contextlib.suppress(Exception):
                await lavalink_backend.close()

    async def set_lavalink_player_volume(self, guild_id: int, volume_percent: int) -> bool:
        if not self.lavalink._wavelink_installed():
            return False
        try:
            import wavelink
            guild = self.bot.get_guild(int(guild_id))
            player = getattr(guild, "voice_client", None) if guild else None
            if player is None or not isinstance(player, wavelink.Player):
                return False
            await player.set_volume(max(0, min(150, int(volume_percent))))
            return True
        except Exception:
            logger.debug("[music/lavalink] falha ao ajustar volume do player", exc_info=True)
            return False

    def compact_runtime_summary(self, guild_id: int | None = None) -> dict[str, Any]:
        cfg_summary = self.lavalink_config_summary(guild_id=guild_id)
        cfg = self.lavalink_store.load(guild_id=guild_id)
        return {
            "configured_backend": self.mode,
            "active_backend": self.playback_backend_for_guild(guild_id),
            "lavalink_mode": cfg.mode,
            "lavalink_enabled": cfg.enabled,
            "lavalink_configured": cfg.configured,
            "lavalink_config_source": cfg_summary.get("source", "padrão"),
            "lavalink_guild_override": bool(cfg_summary.get("guild_override")),
            "lavalink_shadow_active": self.should_shadow_lavalink(guild_id),
            "lavalink_real_allowed_guild": self.is_lavalink_real_allowed_guild(guild_id),
            "lavalink_real_active": self.should_use_lavalink_real(guild_id),
            "lavalink_real_scope": "allowlist" if self._real_lavalink_guild_ids() else "todos",
        }
