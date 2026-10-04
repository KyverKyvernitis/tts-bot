"""Fatos pessoais explícitos, limitados ao canal e à geração da conversa."""
from __future__ import annotations

import time
import math
import json
from uuid import uuid4

from .memory import MemoryEpoch
from .tool_selection import text_terms


FACT_TYPE = "chatbot_conversation_fact"
MAX_FACTS = 24
MAX_FACT_TEXT = 500


def rank_relevant(rows, query, *, content_key="content"):
    """Busca local por termos, tolerando acentos e palavras adicionais.

    Não é uma busca semântica: paráfrases sem vocabulário comum continuam
    exigindo query_own_memory. A ordem original desempata pela recência.
    """
    terms = text_terms(query)
    if not terms:
        return []
    documents = [text_terms(row.get(content_key, "")) for row in rows]
    counts = {term: sum(term in doc for doc in documents) for term in terms}
    scored = []
    for index, (row, doc) in enumerate(zip(rows, documents)):
        matches = terms & doc
        if matches:
            score = sum(math.log1p(len(rows) / max(1, counts[term])) for term in matches)
            scored.append((-score, index, row))
    return [row for _score, _index, row in sorted(scored)]


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
        if str(query).strip():
            docs = rank_relevant(docs, str(query)[:120])
        return [{"ref": doc["fact_id"], "content": str(doc.get("content", ""))[:MAX_FACT_TEXT]}
                for doc in docs[:max(1, min(int(limit), 20))]]

    async def retrieve(self, guild_id, channel_id, user_id, visibility_scope, epoch,
                       *, query, limit=4, max_chars=1000):
        """Dados relevantes para o primeiro pedido, sem chamada adicional de IA.

        Sem cache: reset, esquecimento e alterações tornam-se visíveis na
        próxima leitura. Nunca existe resultado de outro autor ou geração.
        """
        rows = await self.list(guild_id, channel_id, user_id, visibility_scope, epoch,
                               query=query, limit=max(1, min(int(limit), 4)))
        result = []
        budget = max(0, min(int(max_chars), 2000))
        for row in rows:
            # Conta também referências/chaves: o limite é o conteúdo que
            # realmente segue no contexto, não só o texto dos lembretes.
            candidate = [*result, row]
            if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > budget:
                continue
            result = candidate
        return result

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
