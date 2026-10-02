"""Configuração do chatbot único, isolada por servidor."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

from . import constants as C
from .lru_cache import LRUCacheTTL


def _channel_ids(values: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(int(value) for value in values if int(value) > 0))


@dataclass(frozen=True)
class GuildChatbotConfig:
    guild_id: int
    enabled: bool = False
    channel_ids: tuple[int, ...] = ()
    spontaneous_enabled: bool = False
    spontaneous_channel_ids: tuple[int, ...] = ()
    spontaneous_chance_percent: int = C.SPONTANEOUS_DEFAULT_CHANCE_PERCENT
    schema_version: int = C.CHATBOT_SCHEMA_VERSION
    updated_at: float = 0.0
    updated_by: int = 0

    def allows_channel(self, channel_id: int, *, parent_id: Optional[int] = None) -> bool:
        return not self.channel_ids or int(channel_id) in self.channel_ids or (
            parent_id is not None and int(parent_id) in self.channel_ids
        )

    def allows_spontaneous_channel(self, channel_id: int, *, parent_id: Optional[int] = None) -> bool:
        return int(channel_id) in self.spontaneous_channel_ids or (
            parent_id is not None and int(parent_id) in self.spontaneous_channel_ids
        )

    @classmethod
    def from_doc(cls, doc: Optional[dict], *, guild_id: int) -> "GuildChatbotConfig":
        if not doc:
            return cls(guild_id=int(guild_id))
        channels = _channel_ids(doc.get("channel_ids") or ())
        spontaneous_channels = _channel_ids(doc.get("spontaneous_channel_ids") or ())
        chance = max(C.SPONTANEOUS_MIN_CHANCE_PERCENT, min(
            C.SPONTANEOUS_MAX_CHANCE_PERCENT,
            int(doc.get("spontaneous_chance_percent") or C.SPONTANEOUS_DEFAULT_CHANCE_PERCENT),
        ))
        valid_spontaneous_channels = bool(spontaneous_channels) and (
            not channels or set(spontaneous_channels).issubset(channels)
        )
        return cls(
            guild_id=int(guild_id),
            enabled=bool(doc.get("enabled", False)),
            channel_ids=channels,
            spontaneous_enabled=bool(doc.get("spontaneous_enabled", False))
            and valid_spontaneous_channels,
            spontaneous_channel_ids=spontaneous_channels,
            spontaneous_chance_percent=chance,
            schema_version=int(doc.get("schema_version") or C.CHATBOT_SCHEMA_VERSION),
            updated_at=float(doc.get("updated_at") or 0.0),
            updated_by=int(doc.get("updated_by") or 0),
        )

    def to_doc(self) -> dict:
        return {
            "type": C.DOC_TYPE_GUILD_CONFIG,
            "schema_version": self.schema_version,
            "guild_id": self.guild_id,
            "enabled": self.enabled,
            "channel_ids": list(self.channel_ids),
            "spontaneous_enabled": self.spontaneous_enabled,
            "spontaneous_channel_ids": list(self.spontaneous_channel_ids),
            "spontaneous_chance_percent": self.spontaneous_chance_percent,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class ConfigStore:
    def __init__(self, chatbot_coll) -> None:
        self._coll = chatbot_coll
        self._cache: LRUCacheTTL[int, GuildChatbotConfig] = LRUCacheTTL(
            max_entries=C.CONFIG_CACHE_MAX_ENTRIES,
            ttl_seconds=C.CONFIG_CACHE_TTL_SECONDS,
        )

    async def get_config(self, guild_id: int) -> GuildChatbotConfig:
        gid = int(guild_id)
        cached = self._cache.get(gid)
        if cached is not None:
            return cached
        doc = await self._coll.find_one({"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": gid})
        config = GuildChatbotConfig.from_doc(doc, guild_id=gid)
        self._cache.set(gid, config)
        return config

    def quick_might_apply(self, guild_id: int, channel_id: int, *, parent_id: Optional[int] = None) -> bool:
        """Filtro sem I/O para mensagens espontâneas; cache miss exige leitura."""
        config = self._cache.get(int(guild_id))
        if config is None:
            return True
        return (
            config.enabled
            and config.spontaneous_enabled
            and config.allows_channel(channel_id, parent_id=parent_id)
            and config.allows_spontaneous_channel(channel_id, parent_id=parent_id)
        )

    async def save_config(
        self,
        *,
        guild_id: int,
        enabled: bool,
        channel_ids: Iterable[int],
        spontaneous_enabled: bool,
        spontaneous_channel_ids: Iterable[int],
        spontaneous_chance_percent: int,
        updated_by: int,
    ) -> GuildChatbotConfig:
        channels = _channel_ids(channel_ids)
        spontaneous_channels = _channel_ids(spontaneous_channel_ids)
        chance = int(spontaneous_chance_percent)
        if not C.SPONTANEOUS_MIN_CHANCE_PERCENT <= chance <= C.SPONTANEOUS_MAX_CHANCE_PERCENT:
            raise ValueError("A chance deve ser um número inteiro de 1 a 20.")
        if spontaneous_enabled and not spontaneous_channels:
            raise ValueError("Escolha ao menos um canal para respostas espontâneas.")
        if channels and not set(spontaneous_channels).issubset(channels):
            raise ValueError("Os canais espontâneos devem estar entre os canais permitidos.")
        config = GuildChatbotConfig(
            guild_id=int(guild_id),
            enabled=bool(enabled),
            channel_ids=channels,
            spontaneous_enabled=bool(spontaneous_enabled),
            spontaneous_channel_ids=spontaneous_channels,
            spontaneous_chance_percent=chance,
            updated_at=time.time(),
            updated_by=int(updated_by),
        )
        await self._coll.update_one(
            {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": config.guild_id},
            {"$set": config.to_doc(), "$setOnInsert": {"created_at": config.updated_at}},
            upsert=True,
        )
        self._cache.set(config.guild_id, config)
        return config
