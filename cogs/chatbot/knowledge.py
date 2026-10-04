"""Conhecimento publicado pela staff, recuperado localmente sem outra IA.

O chamador deve validar a staff antes de publicar/remover e fornecer escopos
obtidos do Discord. Estes documentos são dados, nunca instruções ou permissões.
Fatos pessoais continuam em ``FactStore``; nenhuma conversa é publicada aqui.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import unicodedata
from collections import Counter, OrderedDict
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from .memory import MemoryEpoch, MemoryStore


KNOWLEDGE_TYPE = "chatbot_published_knowledge"
MAX_KNOWLEDGE_ENTRIES = 100
MAX_KNOWLEDGE_TITLE = 80
MAX_KNOWLEDGE_CONTENT = 2000
MAX_KNOWLEDGE_TAGS = 160
MAX_RETRIEVAL_CHARS = 1200
_PUBLIC_SCOPE = "guild_public"
_REFERENCE = re.compile(r"k-[a-f0-9]{12}\Z")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = frozenset((
    "a ao aos as até com como da das de do dos e ela ele em entre essa esse esta este "
    "estou eu foi isso isto mais mas me meu minha muito na nas não no nos nós o os "
    "ou para pela pelo por porque pra pro que qual quando se sem ser seu sua tem "
    "um uma umas uns você vocês vou the and are for from how is it of on or to with"
).split())


def _normalize(text: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", str(text).casefold())
        if not unicodedata.combining(character)
    )


_NORMAL_STOP_WORDS = frozenset(_normalize(word) for word in _STOP_WORDS)


def _tokens(text: str, *, maximum: int = 0) -> set[str]:
    words = []
    for match in _WORDS.finditer(_normalize(text)):
        word = match.group()
        if len(word) >= 2 and word not in _NORMAL_STOP_WORDS:
            words.append(word)
            if maximum and len(words) >= maximum:
                break
    return set(words)


def _excerpt(content: str, terms: set[str], maximum: int) -> str:
    """Recorta perto dos termos encontrados, preservando o texto original."""
    if len(content) <= maximum:
        return content
    hits = [match.start() for match in _WORDS.finditer(content)
            if _normalize(match.group()) in terms]
    start = max(0, (hits[0] if hits else 0) - maximum // 4)
    end = min(len(content), start + maximum - 2)
    # Evita começar no meio de uma palavra sempre que houver espaço próximo.
    if start:
        boundary = content.find(" ", start, min(end, start + 30))
        if boundary >= 0:
            start = boundary + 1
    result = content[start:end].strip()
    return (("…" if start else "") + result + ("…" if end < len(content) else ""))[:maximum]


class KnowledgeStore:
    """Até 100 entradas da geração atual de cada servidor.

    Slots com índice único garantem o limite mesmo entre processos; o lock só
    evita disputa desnecessária na instância. Todos os campos de consulta são
    construídos aqui a partir de IDs/visibilidade confiáveis do chamador.
    """

    _MAX_LOCKS = 128

    def __init__(self, collection):
        self._coll = collection
        self._memory = MemoryStore(collection)
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        guild_id = int(guild_id)
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        else:
            self._locks.move_to_end(guild_id)
        if len(self._locks) > self._MAX_LOCKS:
            for key, candidate in list(self._locks.items()):
                if key != guild_id and not candidate.locked():
                    self._locks.pop(key, None)
                    break
        return lock

    @staticmethod
    def _generation(guild_id: int, epoch: MemoryEpoch) -> dict:
        return {"type": KNOWLEDGE_TYPE, "guild_id": int(guild_id),
                "global_generation": int(epoch.global_generation),
                "guild_generation": int(epoch.guild_generation)}

    @classmethod
    def _scope(cls, guild_id: int, channel_id: int, visibility_scope: str,
               epoch: MemoryEpoch) -> dict:
        gid, cid = int(guild_id), int(channel_id)
        visibility = str(visibility_scope)
        if gid <= 0 or cid <= 0 or visibility not in {
            f"channel:{cid}", f"private:{cid}", f"nsfw:{cid}",
        }:
            raise ValueError("O conhecimento precisa de um canal válido deste servidor.")
        return {**cls._generation(gid, epoch), "$or": [
            {"channel_id": cid, "visibility_scope": visibility},
            {"channel_id": 0, "visibility_scope": _PUBLIC_SCOPE},
        ]}

    async def _epoch_is_current(self, guild_id: int, epoch: MemoryEpoch) -> bool:
        current = await self._memory.capture_epoch(int(guild_id), 0)
        return (current.global_generation == epoch.global_generation
                and current.guild_generation == epoch.guild_generation)

    async def _documents(self, guild_id: int, channel_id: int, visibility_scope: str,
                         epoch: MemoryEpoch) -> list[dict]:
        if self._coll is None:
            return []
        scope = self._scope(guild_id, channel_id, visibility_scope, epoch)
        if not await self._epoch_is_current(guild_id, epoch):
            return []
        documents = [doc async for doc in self._coll.find(scope)
                     .sort("updated_at", -1).limit(MAX_KNOWLEDGE_ENTRIES)]
        # Um reset pode ocorrer durante a leitura. A geração antiga não retorna.
        if not await self._epoch_is_current(guild_id, epoch):
            return []
        return documents

    @staticmethod
    def _item(doc: dict, *, excerpt: str = "") -> dict:
        return {"ref": str(doc.get("knowledge_ref", "")),
                "title": str(doc.get("title", ""))[:MAX_KNOWLEDGE_TITLE],
                "scope": "guild" if doc.get("visibility_scope") == _PUBLIC_SCOPE else "channel",
                "excerpt": excerpt}

    async def list(self, guild_id: int, channel_id: int, visibility_scope: str,
                   epoch: MemoryEpoch, *, limit: int = 20) -> list[dict]:
        documents = await self._documents(guild_id, channel_id, visibility_scope, epoch)
        return [self._item(doc, excerpt=_excerpt(str(doc.get("content", "")), set(), 240))
                for doc in documents[:max(1, min(int(limit), 100))]]

    async def retrieve(self, guild_id: int, channel_id: int, visibility_scope: str,
                       epoch: MemoryEpoch, *, query: str, limit: int = 3,
                       max_chars: int = MAX_RETRIEVAL_CHARS) -> list[dict]:
        """Busca local por termos: no máximo 3 trechos e 1.200 caracteres JSON."""
        terms = _tokens(str(query)[:1000], maximum=32)
        if not terms:
            return []
        documents = await self._documents(guild_id, channel_id, visibility_scope, epoch)
        tokens = [(_tokens(str(doc.get("title", ""))),
                   _tokens(" ".join(doc.get("tags") or [])),
                   _tokens(str(doc.get("content", "")))) for doc in documents]
        frequencies = Counter(term for groups in tokens for term in set().union(*groups))
        ranked = []
        for doc, (title, tags, body) in zip(documents, tokens):
            matches = terms & (title | tags | body)
            if not matches:
                continue
            score = sum((1 + math.log((len(documents) + 1) / (frequencies[term] + 1)))
                        * (6 * int(term in title) + 5 * int(term in tags) + int(term in body))
                        for term in matches)
            ranked.append((score, len(matches), float(doc.get("updated_at") or 0), doc))
        ranked.sort(key=lambda row: row[:3], reverse=True)
        chosen = ranked[:max(1, min(int(limit), 3))]
        budget = max(0, min(int(max_chars), MAX_RETRIEVAL_CHARS))
        result = []
        for index, (_, _, _, doc) in enumerate(chosen):
            remaining = budget - len(json.dumps(result, ensure_ascii=False))
            if remaining < 100:
                break
            allowance = min(700, max(40, remaining // (len(chosen) - index) - 100))
            content = str(doc.get("content", ""))[:MAX_KNOWLEDGE_CONTENT]
            item = {key: value for key, value in self._item(doc).items() if key != "scope"}
            item["excerpt"] = _excerpt(content, terms, allowance)
            candidate = [*result, item]
            while item["excerpt"] and len(json.dumps(candidate, ensure_ascii=False)) > budget:
                item["excerpt"] = item["excerpt"][:-1]
            if item["excerpt"] and len(json.dumps(candidate, ensure_ascii=False)) <= budget:
                result = candidate
        return result

    @staticmethod
    def _text(value, *, label: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label} precisa ser texto.")
        text = value.strip()
        if not text or len(text) > maximum or "\x00" in text:
            raise ValueError(f"{label} precisa ter de 1 a {maximum} caracteres.")
        return text

    @classmethod
    def _tags(cls, value: str) -> list[str]:
        if not isinstance(value, str) or len(value) > MAX_KNOWLEDGE_TAGS or "\x00" in value:
            raise ValueError("As tags precisam ter até 160 caracteres.")
        tags = list(dict.fromkeys(tag.strip() for tag in value.split(",") if tag.strip()))
        if len(tags) > 8 or any(len(tag) > 32 for tag in tags):
            raise ValueError("Use até 8 tags com no máximo 32 caracteres cada.")
        return tags

    async def publish(self, guild_id: int, channel_id: int, visibility_scope: str,
                      epoch: MemoryEpoch, *, title: str, content: str, tags: str = "",
                      guild_public: bool = False, ref: str = "") -> dict:
        """Staff já autenticada publica conteúdo explícito, não uma conversa."""
        if self._coll is None:
            raise ValueError("A base de conhecimento ainda não está pronta.")
        accessible = self._scope(guild_id, channel_id, visibility_scope, epoch)
        title = self._text(title, label="O título", maximum=MAX_KNOWLEDGE_TITLE)
        content = self._text(content, label="O conteúdo", maximum=MAX_KNOWLEDGE_CONTENT)
        tags_list = self._tags(tags)
        if type(guild_public) is not bool:
            raise ValueError("Escolha explicitamente se a publicação vale para o servidor.")
        reference = str(ref).strip()
        if reference and not _REFERENCE.fullmatch(reference):
            raise ValueError("Use a referência retornada ao publicar o conhecimento.")
        async with self._lock_for(guild_id):
            if not await self._epoch_is_current(guild_id, epoch):
                raise ValueError("A memória mudou. Tente publicar novamente.")
            current = None
            if reference:
                current = await self._coll.find_one({**accessible, "knowledge_ref": reference})
                if current is None:
                    raise ValueError("Esse conhecimento não está disponível neste canal.")
                was_public = current.get("visibility_scope") == _PUBLIC_SCOPE
                if was_public != guild_public:
                    raise ValueError("Para mudar o alcance, remova a entrada e publique uma nova.")
            fields = {"title": title, "content": content, "tags": tags_list,
                      "updated_at": time.time()}
            if current:
                await self._coll.update_one({**accessible, "knowledge_ref": reference}, {"$set": fields})
                doc = {**current, **fields}
            else:
                generation = self._generation(guild_id, epoch)
                existing = [doc async for doc in self._coll.find(generation)
                            .limit(MAX_KNOWLEDGE_ENTRIES)]
                if len(existing) >= MAX_KNOWLEDGE_ENTRIES:
                    raise ValueError("O servidor já tem 100 entradas. Remova uma antes de publicar outra.")
                occupied = {int(doc.get("entry_slot", -1)) for doc in existing}
                reference = "k-" + uuid4().hex[:12]
                doc = {**generation, **fields, "_id": uuid4().hex,
                       "knowledge_ref": reference,
                       "channel_id": 0 if guild_public else int(channel_id),
                       "visibility_scope": _PUBLIC_SCOPE if guild_public else str(visibility_scope)}
                for slot in range(MAX_KNOWLEDGE_ENTRIES):
                    if slot in occupied:
                        continue
                    doc["entry_slot"] = slot
                    try:
                        await self._coll.insert_one(doc)
                        break
                    except DuplicateKeyError:
                        # Outro processo publicou nesse slot enquanto consultávamos.
                        continue
                else:
                    raise ValueError("O servidor já tem 100 entradas. Remova uma antes de publicar outra.")
            if not await self._epoch_is_current(guild_id, epoch):
                await self._coll.delete_one({**self._generation(guild_id, epoch),
                                             "knowledge_ref": reference})
                raise ValueError("A memória mudou. Tente publicar novamente.")
            return {key: value for key, value in self._item(doc).items() if key != "excerpt"}

    async def forget(self, guild_id: int, channel_id: int, visibility_scope: str,
                     epoch: MemoryEpoch, *, ref: str) -> bool:
        """Remove somente uma entrada acessível, após autenticação externa."""
        if self._coll is None:
            return False
        scope = self._scope(guild_id, channel_id, visibility_scope, epoch)
        if not isinstance(ref, str) or not _REFERENCE.fullmatch(ref):
            raise ValueError("Use a referência retornada ao publicar o conhecimento.")
        if not await self._epoch_is_current(guild_id, epoch):
            return False
        result = await self._coll.delete_one({**scope, "knowledge_ref": ref})
        return bool(result.deleted_count)
