"""Fatos pessoais explícitos, limitados ao canal e à geração da conversa."""
from __future__ import annotations

import time
from uuid import uuid4

from .memory import MemoryEpoch


FACT_TYPE = "chatbot_conversation_fact"
MAX_FACTS = 24
MAX_FACT_TEXT = 500


class FactStore:
    def __init__(self, collection):
        self._coll = collection

    @staticmethod
    def _scope(guild_id, channel_id, user_id, visibility_scope, epoch: MemoryEpoch):
        return {
            "type": FACT_TYPE, "guild_id": int(guild_id), "channel_id": int(channel_id),
            "user_id": int(user_id), "visibility_scope": str(visibility_scope),
            "global_generation": epoch.global_generation,
            "guild_generation": epoch.guild_generation,
            "user_generation": epoch.user_generation,
        }

    async def list(self, guild_id, channel_id, user_id, visibility_scope, epoch, *, query="", limit=10):
        scope = self._scope(guild_id, channel_id, user_id, visibility_scope, epoch)
        docs = [doc async for doc in self._coll.find(scope).sort("updated_at", -1).limit(MAX_FACTS)]
        needle = str(query).strip().casefold()[:120]
        if needle:
            docs = [doc for doc in docs if needle in str(doc.get("content", "")).casefold()]
        return [{"ref": doc["fact_id"], "content": str(doc.get("content", ""))[:MAX_FACT_TEXT]}
                for doc in docs[:max(1, min(int(limit), 20))]]

    async def remember(self, guild_id, channel_id, user_id, visibility_scope, epoch, *, content):
        text = str(content).strip()
        if not text or len(text) > MAX_FACT_TEXT or "\x00" in text:
            raise ValueError("O lembrete precisa ter de 1 a 500 caracteres.")
        scope = self._scope(guild_id, channel_id, user_id, visibility_scope, epoch)
        existing = await self._coll.find_one({**scope, "content": text})
        if existing:
            return {"ref": existing["fact_id"], "content": text}
        docs = [doc async for doc in self._coll.find(scope).sort("updated_at", -1).limit(MAX_FACTS)]
        if len(docs) >= MAX_FACTS:
            raise ValueError("Os lembretes deste canal estão cheios. Esqueça um antes de salvar outro.")
        identifier = str(uuid4())
        await self._coll.insert_one({**scope, "_id": identifier, "fact_id": identifier,
                                     "content": text, "updated_at": time.time()})
        return {"ref": identifier, "content": text}

    async def forget(self, guild_id, channel_id, user_id, visibility_scope, epoch, *, ref):
        scope = self._scope(guild_id, channel_id, user_id, visibility_scope, epoch)
        result = await self._coll.delete_one({**scope, "fact_id": str(ref)[:64]})
        return bool(result.deleted_count)
