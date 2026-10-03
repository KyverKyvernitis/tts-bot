"""O host decide uma vez por resposta se conversa vira um arquivo de áudio."""
from __future__ import annotations

import random
import time
from typing import Callable

from .audio import MAX_TTS_CHARS
from .config import GuildChatbotConfig


class AudioReplySelector:
    def __init__(self, *, draw: Callable[[], float] | None = None,
                 clock: Callable[[], float] | None = None):
        self._draw = draw or random.random
        self._clock = clock or time.monotonic
        self._cooldowns: dict[tuple[int, int], float] = {}

    def select(self, *, config: GuildChatbotConfig, guild_id: int, channel_id: int,
               content: str = "", reply: str, eligible: bool = True,
               mode: str = "auto", requested: bool = False) -> str:
        """Retorna text, requested ou random; nenhum sorteio em turnos inelegíveis."""
        if not (eligible and reply.strip() and config.enabled
                and config.actions_enabled and config.audio_actions_enabled):
            return "text"
        # Intenção vem de ferramenta nativa e preferência estruturada. Texto
        # livre nunca é interpretado aqui como comando operacional.
        if mode == "text":
            return "text"
        if requested or mode == "audio":
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
