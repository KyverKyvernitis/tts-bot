"""O host decide uma vez por resposta se conversa vira um arquivo de áudio."""
from __future__ import annotations

import random
import re
import time
import unicodedata
from typing import Callable

from .audio import MAX_TTS_CHARS, user_asked_for_tts
from .config import GuildChatbotConfig


def prefers_text(content: str) -> bool:
    text = "".join(char for char in unicodedata.normalize("NFKD", content or "")
                   if not unicodedata.combining(char)).casefold()
    return bool(re.search(
        r"\b(?:sem|nada\s+de)\s+(?:audio|voz)\b"
        r"|\b(?:nao|nunca|nem)\s+(?:me\s+)?(?:manda|mande|mandar|envia|envie|enviar|"
        r"responde|responda|responder|fala|fale|falar|quero|queria|preciso)\s+"
        r"(?:\w+\s+){0,8}(?:audio|voz)\b"
        r"|\b(?:so|somente|apenas)\s+(?:em\s+)?texto\b"
        r"|\b(?:prefiro|quero)\s+(?:em\s+)?texto\b"
        r"|\b(?:responda|responde|fale|fala)\s+(?:em|por)\s+texto\b",
        text,
    ))


class AudioReplySelector:
    def __init__(self, *, draw: Callable[[], float] | None = None,
                 clock: Callable[[], float] | None = None):
        self._draw = draw or random.random
        self._clock = clock or time.monotonic
        self._cooldowns: dict[tuple[int, int], float] = {}

    def select(self, *, config: GuildChatbotConfig, guild_id: int, channel_id: int,
               content: str, reply: str, eligible: bool = True) -> str:
        """Retorna text, requested ou random; nenhum sorteio em turnos inelegíveis."""
        if not (eligible and reply.strip() and config.enabled
                and config.actions_enabled and config.audio_actions_enabled):
            return "text"
        if prefers_text(content):
            return "text"
        if user_asked_for_tts(content):
            return "requested"
        # Conteúdo longo ou com código precisa continuar legível. O sorteio
        # nunca trunca uma resposta normal escolhida como texto.
        if len(reply) > MAX_TTS_CHARS or "```" in reply:
            return "text"
        chance = config.audio_reply_chance_percent
        if chance <= 0:
            return "text"
        key = int(guild_id), int(channel_id)
        if self._cooldowns.get(key, 0.0) > self._clock():
            return "text"
        return "random" if self._draw() < chance / 100.0 else "text"

    def record_sent(self, *, guild_id: int, channel_id: int,
                    cooldown_seconds: int) -> None:
        now = self._clock()
        self.cleanup(now)
        self._cooldowns[(int(guild_id), int(channel_id))] = now + max(0, int(cooldown_seconds))

    def cleanup(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        for key, expires in list(self._cooldowns.items()):
            if expires <= now:
                self._cooldowns.pop(key, None)
