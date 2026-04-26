"""Histórico de conversas — 2 escopos, cada um SEPARADO POR PROFILE.

Este é um ponto crítico: cada profile do chatbot tem sua própria memória,
tanto pessoal quanto coletiva. Se o server tem 2 profiles (Lua e Toguro),
a conversa com Lua NÃO aparece quando Toguro responde — são personagens
diferentes e confundir as memórias quebra a ilusão.

1. Memória POR USUÁRIO POR PROFILE: o que aquele user falou com aquele
   profile específico. Chave: (guild_id, profile_id, user_id).
   Serve para continuidade natural da conversa individual daquele usuário
   com aquele personagem.

2. Memória COLETIVA DO PROFILE: todas as trocas recentes daquele profile
   no servidor, independente de quem falou. Chave: (guild_id, profile_id).
   Dá contexto social ao profile (quem ele já cumprimentou, o que
   conversaram por perto, etc).

Ambas são rolling windows — ao passar do limite, as mais antigas caem.
Cada entrada é pequena (~100-500 bytes) — docs ficam em tamanho bem
controlado mesmo com 20+30 msgs.

IMPORTANTE — prompt injection: a memória coletiva recebe mensagens de
qualquer usuário. O wrapper COLLECTIVE_MEMORY_GUARD é aplicado ANTES
desse histórico no prompt final (isso fica em `cog.py`, não aqui).

Compatibilidade: docs antigos salvos sem profile_id não são lidos (ficam
órfãos). `clear_all_guild_memory` apaga eles junto. Quem quiser pode
migrar, mas ganhamos pouco — a memória é efêmera por design.
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
    """Persistência de histórico pessoal + coletivo, SEPARADO POR PROFILE.

    Recebe a collection dedicada do chatbot (NÃO `settings_db.coll`, que
    pertence a outros cogs e tem índice UNIQUE incompatível).

    Sem cache em RAM — cada read vai ao Mongo. Docs são pequenos (<30KB cada)
    e cada request faz no máximo 2 reads (user + guild) = ~60KB trafegados
    por mensagem. Na VPS 1GB, esse é o trade-off correto: zero state em
    processo, tudo em Mongo.
    """

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll

    # --- Memória pessoal (user x profile) --------------------------------------

    async def get_user_history(
        self, guild_id: int, profile_id: str, user_id: int
    ) -> list[MemoryEntry]:
        """Retorna histórico do user COM ESSE PROFILE ESPECÍFICO.

        Se mudar de profile, memória começa zerada — é o comportamento certo,
        personagens diferentes não compartilham lembranças."""
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MEMORY,
            "scope": "user",
            "guild_id": int(guild_id),
            "profile_id": str(profile_id),
            "user_id": int(user_id),
        })
        if not doc:
            return []
        entries = doc.get("entries") or []
        return [MemoryEntry.from_dict(e) for e in entries if isinstance(e, dict)]

    async def append_user_turn(
        self,
        guild_id: int,
        profile_id: str,
        user_id: int,
        *,
        user_message: str,
        user_name: str,
        assistant_message: str,
        max_messages: int = C.USER_MEMORY_MAX_MESSAGES,
    ) -> None:
        """Adiciona o par (user, assistant) ao histórico pessoal DESSE PROFILE.

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
                "profile_id": str(profile_id),
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
                    "profile_id": str(profile_id),
                    "user_id": int(user_id),
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def clear_user_history(
        self,
        guild_id: int,
        user_id: int,
        profile_id: Optional[str] = None,
    ) -> int:
        """Apaga memória pessoal.

        - Se `profile_id` passado: apaga só aquela com aquele profile.
        - Se None: apaga memória do user com TODOS os profiles do guild
          (é o que o `/reset` user-facing usa — "esquece tudo sobre mim aqui").
        """
        query: dict = {
            "type": C.DOC_TYPE_MEMORY,
            "scope": "user",
            "guild_id": int(guild_id),
            "user_id": int(user_id),
        }
        if profile_id is not None:
            query["profile_id"] = str(profile_id)
        result = await self._coll.delete_many(query)
        return result.deleted_count

    # --- Memória coletiva (guild x profile) ------------------------------------

    async def get_guild_history(
        self, guild_id: int, profile_id: str
    ) -> list[MemoryEntry]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MEMORY,
            "scope": "guild",
            "guild_id": int(guild_id),
            "profile_id": str(profile_id),
        })
        if not doc:
            return []
        entries = doc.get("entries") or []
        return [MemoryEntry.from_dict(e) for e in entries if isinstance(e, dict)]

    async def append_guild_turn(
        self,
        guild_id: int,
        profile_id: str,
        *,
        user_id: int,
        user_name: str,
        user_message: str,
        assistant_message: str,
        max_messages: int = C.GUILD_MEMORY_MAX_MESSAGES,
    ) -> None:
        """Adiciona par (user, assistant) à memória coletiva DESSE PROFILE."""
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
                "profile_id": str(profile_id),
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
                    "profile_id": str(profile_id),
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def clear_guild_history(
        self, guild_id: int, profile_id: Optional[str] = None
    ) -> int:
        """Apaga memória coletiva de um profile específico, ou todos do guild."""
        query: dict = {
            "type": C.DOC_TYPE_MEMORY,
            "scope": "guild",
            "guild_id": int(guild_id),
        }
        if profile_id is not None:
            query["profile_id"] = str(profile_id)
        result = await self._coll.delete_many(query)
        return result.deleted_count

    # --- Ao apagar profile, limpar suas memórias -------------------------------

    async def clear_profile_memory(self, guild_id: int, profile_id: str) -> int:
        """Apaga TODA memória (pessoal + coletiva) daquele profile.
        Chamado quando a staff apaga um profile — evita deixar doc órfão."""
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MEMORY,
            "guild_id": int(guild_id),
            "profile_id": str(profile_id),
        })
        return result.deleted_count

    # --- Utilitário conjunto (reset total do chatbot num server) ---------------

    async def clear_all_guild_memory(self, guild_id: int) -> int:
        """Apaga TODA memória do guild, de todos os profiles inclusive docs
        legados sem profile_id. Retorna total deletado."""
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MEMORY,
            "guild_id": int(guild_id),
        })
        return result.deleted_count

    async def clear_all_memory_everywhere(self) -> int:
        """Apaga TODA memória de chatbot do banco — todas as guilds, todos
        os profiles, pessoal e coletiva. Operação destrutiva irreversível.
        Usada pelo comando admin `/chatbot reset_global`. Retorna total
        deletado."""
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MEMORY,
        })
        return result.deleted_count

