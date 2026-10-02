"""Participação espontânea opcional do próprio bot, sem identidades extras."""
from __future__ import annotations

import random
import re

import discord

from . import constants as C
from .config import GuildChatbotConfig

_URL_RE = re.compile(r"https?://\S+|discord\.gg/\S+|www\.\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]{3,}")
_COMMAND_PREFIXES = C.COMMAND_PREFIXES + ("@",)


def is_spontaneous_candidate(message: discord.Message, config: GuildChatbotConfig) -> bool:
    if not config.enabled or not config.spontaneous_enabled:
        return False
    if int(getattr(getattr(message, "guild", None), "id", 0) or 0) != config.guild_id:
        return False
    channel_id = int(getattr(message.channel, "id", 0) or 0)
    parent_id = getattr(message.channel, "parent_id", None)
    if not config.allows_spontaneous_channel(channel_id, parent_id=parent_id) or not config.allows_channel(channel_id, parent_id=parent_id):
        return False
    if getattr(message.author, "bot", False) or getattr(message, "webhook_id", None) is not None:
        return False
    if getattr(message, "type", discord.MessageType.default) is not discord.MessageType.default:
        return False
    if getattr(message, "reference", None) is not None:
        return False
    text = str(getattr(message, "content", "") or "").strip()
    if not text or text.startswith(_COMMAND_PREFIXES):
        return False
    if "@everyone" in text.lower() or "@here" in text.lower():
        return False
    useful = " ".join(_URL_RE.sub("", text).split())
    return len(useful) >= C.SPONTANEOUS_MIN_MESSAGE_CHARS and _WORD_RE.search(useful) is not None


def roll_chance(config: GuildChatbotConfig) -> bool:
    return random.random() < config.spontaneous_chance_percent / 100.0


def spontaneous_prompt_hint() -> str:
    return (
        "Esta é uma resposta espontânea: você não foi chamado diretamente. "
        "Entre na conversa somente com algo natural, curto e útil. Não aja como "
        "se o usuário tivesse pedido sua resposta, nem domine a conversa. "
        f"Responda em no máximo {C.SPONTANEOUS_MAX_REPLY_CHARS} caracteres."
    )
