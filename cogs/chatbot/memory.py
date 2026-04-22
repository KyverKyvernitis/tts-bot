"""Histórico de conversas — 2 escopos.

1. Memória POR USUÁRIO: o que aquele user específico já falou com o bot.
   Chave: (guild_id, user_id). Doc único por par. Serve para continuidade
   natural de conversa individual.

2. Memória COLETIVA DO GUILD: todas as trocas recentes no servidor,
   independente de quem falou. Chave: (guild_id). Doc único por guild.
   Serve para dar contexto social ao bot.

Ambas são rolling windows — ao passar do limite, as mais antigas caem.
Cada entrada é pequena (~100-500 bytes) — docs ficam em tamanho bem
controlado mesmo com 20+30 msgs.

IMPORTANTE — prompt injection: a memória coletiva recebe mensagens de
qualquer usuário. O wrapper COLLECTIVE_MEMORY_GUARD é aplicado ANTES
desse histórico no prompt final (isso fica em `cog.py`, não aqui).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import constants as C

log = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """Uma entrada no histórico. role = 'user' ou 'assistant'."""

    role: str
    content: str
    user_id: int = 0      # quem enviou (apenas na memória coletiva — fica 0 na pessoal)
    user_name: str = ""   # display name — ajuda a dar contexto no prompt coletivo
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(
            role=str(d.get("role") or "user"),
            content=str(d.get("content") or ""),
            user_id=int(d.get("user_id") or 0),
            user_name=str(d.get("user_name") or ""),
            timestamp=float(d.get("timestamp") or time.time()),
        )


class MemoryStore:
    """Persistência de histórico pessoal + coletivo.

    Sem cache em RAM — cada read vai ao Mongo. Docs são pequenos (<30KB cada)
    e cada request faz no máximo 2 reads (user + guild) = ~60KB trafegados
    por mensagem. Na VPS 1GB, esse é o trade-off correto: zero state em
    processo, tudo em Mongo.
    """

    def __init__(self, settings_db):
        self._coll = settings_db.coll

    # --- Memória pessoal (user) ------------------------------------------------

    async def get_user_history(self, guild_id: int, user_id: int) -> list[MemoryEntry]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MEMORY,
            "scope": "user",
            "guild_id": int(guild_id),
            "user_id": int(user_id),
        })
        if not doc:
            return []
        entries = doc.get("entries") or []
        return [MemoryEntry.from_dict(e) for e in entries if isinstance(e, dict)]

    async def append_user_turn(
        self,
        guild_id: int,
        user_id: int,
        *,
        user_message: str,
        user_name: str,
        assistant_message: str,
        max_messages: int = C.USER_MEMORY_MAX_MESSAGES,
    ) -> None:
        """Adiciona o par (user, assistant) ao histórico pessoal, faz rotate.

        Usamos uma única operação Mongo com $push + $slice pra manter o doc
        bounded sem precisar ler-modificar-escrever.
        """
        now = time.time()
        new_entries = [
            MemoryEntry(
                role="user",
                content=user_message,
                user_id=int(user_id),
                user_name=user_name,
                timestamp=now,
            ).to_dict(),
            MemoryEntry(
                role="assistant",
                content=assistant_message,
                user_id=0,
                user_name="",
                timestamp=now + 0.001,
            ).to_dict(),
        ]
        await self._coll.update_one(
            {
                "type": C.DOC_TYPE_MEMORY,
                "scope": "user",
                "guild_id": int(guild_id),
                "user_id": int(user_id),
            },
            {
                "$push": {
                    "entries": {
                        "$each": new_entries,
                        "$slice": -int(max(2, max_messages)),
                    }
                },
                "$set": {"updated_at": now},
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MEMORY,
                    "scope": "user",
                    "guild_id": int(guild_id),
                    "user_id": int(user_id),
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def clear_user_history(self, guild_id: int, user_id: int) -> bool:
        result = await self._coll.delete_one({
            "type": C.DOC_TYPE_MEMORY,
            "scope": "user",
            "guild_id": int(guild_id),
            "user_id": int(user_id),
        })
        return result.deleted_count > 0

    # --- Memória coletiva (guild) ----------------------------------------------

    async def get_guild_history(self, guild_id: int) -> list[MemoryEntry]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MEMORY,
            "scope": "guild",
            "guild_id": int(guild_id),
        })
        if not doc:
            return []
        entries = doc.get("entries") or []
        return [MemoryEntry.from_dict(e) for e in entries if isinstance(e, dict)]

    async def append_guild_turn(
        self,
        guild_id: int,
        *,
        user_id: int,
        user_name: str,
        user_message: str,
        assistant_message: str,
        max_messages: int = C.GUILD_MEMORY_MAX_MESSAGES,
    ) -> None:
        """Adiciona par (user, assistant) à memória coletiva do guild, com rotate."""
        now = time.time()
        new_entries = [
            MemoryEntry(
                role="user",
                content=user_message,
                user_id=int(user_id),
                user_name=user_name,
                timestamp=now,
            ).to_dict(),
            MemoryEntry(
                role="assistant",
                content=assistant_message,
                user_id=0,
                user_name="",
                timestamp=now + 0.001,
            ).to_dict(),
        ]
        await self._coll.update_one(
            {
                "type": C.DOC_TYPE_MEMORY,
                "scope": "guild",
                "guild_id": int(guild_id),
            },
            {
                "$push": {
                    "entries": {
                        "$each": new_entries,
                        "$slice": -int(max(2, max_messages)),
                    }
                },
                "$set": {"updated_at": now},
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MEMORY,
                    "scope": "guild",
                    "guild_id": int(guild_id),
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def clear_guild_history(self, guild_id: int) -> bool:
        result = await self._coll.delete_one({
            "type": C.DOC_TYPE_MEMORY,
            "scope": "guild",
            "guild_id": int(guild_id),
        })
        return result.deleted_count > 0

    # --- Utilitário conjunto (reset total do chatbot num server) ---------------

    async def clear_all_guild_memory(self, guild_id: int) -> int:
        """Apaga memória coletiva E todas as pessoais do guild. Retorna total deletado."""
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MEMORY,
            "guild_id": int(guild_id),
        })
        return result.deleted_count
