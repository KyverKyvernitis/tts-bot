"""Modo extrovertido do chatbot.

Configura uma chance controlada para um ou mais profiles responderem mensagens
normais em canais permitidos, sem precisar de menção ou reply direto.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import discord

from . import constants as C
from .lru_cache import LRUCacheTTL
from .profiles import ChatbotProfile


_URL_RE = re.compile(r"https?://\S+|discord\.gg/\S+|www\.\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_WORDISH_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]{3,}", re.UNICODE)


@dataclass(frozen=True)
class ExtrovertOptions:
    allow_text_channels: bool = True
    allow_voice_channels: bool = True
    ignore_links: bool = True
    ignore_short: bool = True
    avoid_channel_streak: bool = True


@dataclass(frozen=True)
class ExtrovertConfig:
    guild_id: int
    enabled: bool = False
    chance_percent: int = C.EXTROVERT_DEFAULT_CHANCE_PERCENT
    profile_ids: tuple[str, ...] = field(default_factory=tuple)
    channel_ids: tuple[int, ...] = field(default_factory=tuple)
    max_profiles_per_message: int = 1
    options: ExtrovertOptions = field(default_factory=ExtrovertOptions)
    updated_by: int = 0
    updated_at: float = 0.0

    @classmethod
    def disabled(cls, guild_id: int) -> "ExtrovertConfig":
        return cls(guild_id=int(guild_id), enabled=False)

    @classmethod
    def from_doc(cls, doc: Optional[dict], guild_id: int) -> "ExtrovertConfig":
        if not doc:
            return cls.disabled(guild_id)
        options = dict(doc.get("options") or {})
        return cls(
            guild_id=int(doc.get("guild_id") or guild_id),
            enabled=bool(doc.get("enabled") or False),
            chance_percent=_clamp_chance(doc.get("chance_percent")),
            profile_ids=tuple(str(p) for p in (doc.get("profile_ids") or []) if str(p or "").strip()),
            channel_ids=tuple(int(c) for c in (doc.get("channel_ids") or []) if int(c or 0) > 0),
            max_profiles_per_message=1,
            options=ExtrovertOptions(
                allow_text_channels=bool(options.get("allow_text_channels", True)),
                allow_voice_channels=bool(options.get("allow_voice_channels", True)),
                ignore_links=bool(options.get("ignore_links", True)),
                ignore_short=bool(options.get("ignore_short", True)),
                avoid_channel_streak=bool(options.get("avoid_channel_streak", True)),
            ),
            updated_by=int(doc.get("updated_by") or 0),
            updated_at=float(doc.get("updated_at") or 0.0),
        )

    def to_doc(self) -> dict:
        return {
            "type": C.DOC_TYPE_EXTROVERT,
            "guild_id": int(self.guild_id),
            "enabled": bool(self.enabled),
            "chance_percent": _clamp_chance(self.chance_percent),
            "profile_ids": list(self.profile_ids),
            "channel_ids": [int(c) for c in self.channel_ids],
            "max_profiles_per_message": 1,
            "options": {
                "allow_text_channels": bool(self.options.allow_text_channels),
                "allow_voice_channels": bool(self.options.allow_voice_channels),
                "ignore_links": bool(self.options.ignore_links),
                "ignore_short": bool(self.options.ignore_short),
                "avoid_channel_streak": bool(self.options.avoid_channel_streak),
            },
            "updated_by": int(self.updated_by),
            "updated_at": float(self.updated_at or time.time()),
        }


@dataclass(frozen=True)
class ExtrovertModalConfig:
    enabled: bool
    chance_percent: int
    profile_ids: tuple[str, ...]
    channel_ids: tuple[int, ...]
    options: ExtrovertOptions


def _clamp_chance(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = C.EXTROVERT_DEFAULT_CHANCE_PERCENT
    return max(C.EXTROVERT_MIN_CHANCE_PERCENT, min(C.EXTROVERT_MAX_CHANCE_PERCENT, number))


class ExtrovertStore:
    """Persistência da config `/chatbot extrovert`, com cache por guild."""

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        self._cache: LRUCacheTTL[int, ExtrovertConfig] = LRUCacheTTL(
            max_entries=C.EXTROVERT_CONFIG_CACHE_MAX_ENTRIES,
            ttl_seconds=C.EXTROVERT_CONFIG_CACHE_TTL_SECONDS,
        )

    def quick_might_apply(self, guild_id: int, channel_id: int) -> bool:
        """Filtro barato para o listener.

        True também em cache miss para permitir carregar a config depois do
        restart. Depois que o cache aprende que está desativado, fica barato.
        """
        cfg = self._cache.get(int(guild_id))
        if cfg is None:
            return True
        if not cfg.enabled:
            return False
        return int(channel_id) in set(cfg.channel_ids)

    async def get_config(self, guild_id: int) -> ExtrovertConfig:
        guild_id_i = int(guild_id)
        cached = self._cache.get(guild_id_i)
        if cached is not None:
            return cached
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_EXTROVERT,
            "guild_id": guild_id_i,
        })
        cfg = ExtrovertConfig.from_doc(doc, guild_id_i)
        self._cache.set(guild_id_i, cfg)
        return cfg

    async def save_config(
        self,
        *,
        guild_id: int,
        enabled: bool,
        chance_percent: int,
        profile_ids: Iterable[str],
        channel_ids: Iterable[int],
        options: ExtrovertOptions,
        updated_by: int,
    ) -> ExtrovertConfig:
        cfg = ExtrovertConfig(
            guild_id=int(guild_id),
            enabled=bool(enabled),
            chance_percent=_clamp_chance(chance_percent),
            profile_ids=tuple(dict.fromkeys(str(p) for p in profile_ids if str(p or "").strip())),
            channel_ids=tuple(dict.fromkeys(int(c) for c in channel_ids if int(c or 0) > 0)),
            max_profiles_per_message=1,
            options=options,
            updated_by=int(updated_by),
            updated_at=time.time(),
        )
        await self._coll.update_one(
            {"type": C.DOC_TYPE_EXTROVERT, "guild_id": int(guild_id)},
            {"$set": cfg.to_doc()},
            upsert=True,
        )
        self._cache.set(int(guild_id), cfg)
        return cfg


def channel_kind_allowed(channel, config: ExtrovertConfig) -> bool:
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return bool(config.options.allow_text_channels)
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return bool(config.options.allow_voice_channels)
    return False


def _looks_like_command(text: str) -> bool:
    stripped = str(text or "").lstrip()
    if not stripped:
        return False
    if stripped.startswith(C.PERSONA_COMMAND_PREFIXES):
        return True
    return stripped.lower().startswith(("/chatbot", "/imagem", "bot ", "cmd "))


def _is_link_only(text: str) -> bool:
    without = _URL_RE.sub("", text).strip(" \n\t.,;:!?'\"()[]{}<>")
    return not without


def is_extrovert_candidate(message: discord.Message, config: ExtrovertConfig) -> bool:
    if not config.enabled:
        return False
    if int(getattr(message.channel, "id", 0) or 0) not in set(config.channel_ids):
        return False
    if not channel_kind_allowed(message.channel, config):
        return False
    if message.reference is not None:
        return False
    text = str(getattr(message, "content", "") or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "@everyone" in lowered or "@here" in lowered:
        return False
    if _looks_like_command(text):
        return False
    if config.options.ignore_links and _is_link_only(text):
        return False
    if config.options.ignore_short:
        compact = _WS_RE.sub(" ", _URL_RE.sub("", text)).strip()
        if len(compact) < C.EXTROVERT_MIN_MESSAGE_CHARS:
            return False
        if _WORDISH_RE.search(compact) is None:
            return False
    return True


def roll_chance(config: ExtrovertConfig) -> bool:
    return random.random() < (_clamp_chance(config.chance_percent) / 100.0)


def pick_profile(
    *,
    profiles: Iterable[ChatbotProfile],
    config: ExtrovertConfig,
    blocked_profile_ids: set[str] | None = None,
) -> Optional[ChatbotProfile]:
    allowed = set(config.profile_ids)
    blocked = blocked_profile_ids or set()
    candidates = [
        p for p in profiles
        if p.profile_id in allowed and p.profile_id not in blocked
    ]
    if not candidates:
        return None
    return random.choice(candidates)


def extrovert_prompt_hint() -> str:
    return (
        "IMPORTANTE: esta é uma resposta espontânea. Você não foi chamado por "
        "menção direta. Entre na conversa só com algo natural, curto e útil. "
        "Não aja como se o usuário tivesse pedido sua resposta diretamente, "
        "não domine a conversa e não escreva textão. Responda em no máximo "
        f"{C.EXTROVERT_MAX_REPLY_CHARS} caracteres."
    )
