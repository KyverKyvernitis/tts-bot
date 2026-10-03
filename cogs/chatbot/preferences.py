"""Preferências explícitas da conversa, isoladas por membro, canal e geração."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .memory import MemoryEpoch

DOC_TYPE_PREFERENCES = "chatbot_conversation_preferences"
MODES = frozenset({"auto", "audio", "text"})
_VOICE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z", re.ASCII)
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}\Z", re.ASCII)


class PreferenceStale(ValueError):
    """Um reset ocorrido durante a operação tornou o escopo antigo inválido."""


@dataclass(frozen=True)
class ConversationPreferences:
    mode: str = "auto"
    voice: str = ""
    language: str = ""

    def to_result(self) -> dict:
        return {"mode": self.mode, "voice": self.voice, "language": self.language}


class PreferenceStore:
    def __init__(self, collection, *, memory):
        self._coll, self._memory = collection, memory

    @staticmethod
    def _identity(guild_id: int, channel_id: int, user_id: int, epoch: MemoryEpoch) -> dict:
        identifiers = tuple(int(value) for value in (guild_id, channel_id, user_id))
        if any(value <= 0 for value in identifiers) or not isinstance(epoch, MemoryEpoch):
            raise ValueError("Escopo de preferências inválido.")
        generations = (epoch.global_generation, epoch.guild_generation, epoch.user_generation)
        if any(type(value) is not int or value < 0 for value in generations):
            raise ValueError("Geração de preferências inválida.")
        # A chave inclui a geração: um write atrasado nunca substitui a
        # preferência criada depois de /reset, mesmo com vários processos.
        key = ":".join(str(value) for value in (*identifiers, *generations))
        return {"_id": f"chatbot-preferences:{key}", "type": DOC_TYPE_PREFERENCES,
                "guild_id": identifiers[0], "channel_id": identifiers[1], "user_id": identifiers[2],
                "global_generation": generations[0], "guild_generation": generations[1],
                "user_generation": generations[2]}

    async def _check_epoch(self, guild_id: int, user_id: int, epoch: MemoryEpoch) -> None:
        if self._memory is None or await self._memory.capture_epoch(guild_id, user_id) != epoch:
            raise PreferenceStale("A conversa foi reiniciada. Tente novamente.")

    @staticmethod
    def _read(doc) -> ConversationPreferences:
        if not isinstance(doc, dict):
            return ConversationPreferences()
        mode, voice, language = doc.get("mode"), doc.get("voice"), doc.get("language")
        return ConversationPreferences(
            mode=mode if isinstance(mode, str) and mode in MODES else "auto",
            voice=voice if isinstance(voice, str) and (not voice or _VOICE.fullmatch(voice)) else "",
            language=language.lower() if isinstance(language, str) and (not language or _LANGUAGE.fullmatch(language)) else "",
        )

    async def get_current(self, guild_id: int, channel_id: int, user_id: int,
                          epoch: MemoryEpoch) -> ConversationPreferences:
        identity = self._identity(guild_id, channel_id, user_id, epoch)
        await self._check_epoch(guild_id, user_id, epoch)
        document = await self._coll.find_one(identity)
        await self._check_epoch(guild_id, user_id, epoch)
        return self._read(document)

    async def set_current(self, guild_id: int, channel_id: int, user_id: int,
                          epoch: MemoryEpoch, *, mode=None, voice=None,
                          language=None) -> ConversationPreferences:
        identity = self._identity(guild_id, channel_id, user_id, epoch)
        updates = {}
        if mode is not None:
            if not isinstance(mode, str) or mode not in MODES:
                raise ValueError("Formato inválido. Use auto, audio ou text.")
            updates["mode"] = mode
        for field, value, validator in (("voice", voice, _VOICE), ("language", language, _LANGUAGE)):
            if value is not None:
                if not isinstance(value, str) or (value and not validator.fullmatch(value)):
                    raise ValueError("Preferência de voz ou idioma inválida.")
                updates[field] = value.lower() if field == "language" else value
        if not updates:
            raise ValueError("Informe ao menos uma preferência para alterar.")
        await self._check_epoch(guild_id, user_id, epoch)
        defaults = {key: value for key, value in ConversationPreferences().to_result().items()
                    if key not in updates}
        defaults.update({**identity, "created_at": time.time()})
        await self._coll.update_one(identity,
                                    {"$set": {**updates, "updated_at": time.time()},
                                     "$setOnInsert": defaults}, upsert=True)
        return await self.get_current(guild_id, channel_id, user_id, epoch)
