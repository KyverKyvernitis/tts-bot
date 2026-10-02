"""Índice persistente das mensagens nativas enviadas pelo chatbot.

O tipo de documento V3 não reconhece mensagens dos webhooks antigos.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

from . import constants as C
from .lru_cache import LRUCacheTTL


@dataclass(frozen=True)
class ChatbotMessageRef:
    guild_id: int
    channel_id: int
    message_id: int
    created_at: float = 0.0

    @classmethod
    def from_doc(cls, doc: dict) -> "ChatbotMessageRef":
        return cls(
            guild_id=int(doc.get("guild_id") or 0),
            channel_id=int(doc.get("channel_id") or 0),
            message_id=int(doc.get("message_id") or 0),
            created_at=float(doc.get("created_at") or 0.0),
        )


class ChatbotMessageIndex:
    """Reconhece respostas do bot mesmo quando o Discord não resolve o reply."""

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        self._cache: LRUCacheTTL[int, ChatbotMessageRef] = LRUCacheTTL(
            max_entries=C.MESSAGE_CACHE_MAX_ENTRIES,
            ttl_seconds=C.MESSAGE_CACHE_TTL_SECONDS,
        )

    async def remember(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        if min(int(guild_id), int(channel_id), int(message_id)) <= 0:
            return
        now = time.time()
        ref = ChatbotMessageRef(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            message_id=int(message_id),
            created_at=now,
        )
        self._cache.set(int(message_id), ref)
        await self._coll.update_one(
            {
                "type": C.DOC_TYPE_MESSAGE_MAP,
                "message_id": int(message_id),
            },
            {
                "$set": {
                    "type": C.DOC_TYPE_MESSAGE_MAP,
                    "guild_id": int(guild_id),
                    "channel_id": int(channel_id),
                    "message_id": int(message_id),
                    "created_at": now,
                    "expires_at": datetime.now(timezone.utc) + timedelta(
                        seconds=C.MESSAGE_CACHE_TTL_SECONDS
                    ),
                }
            },
            upsert=True,
        )

    async def resolve(self, message_id: int) -> Optional[ChatbotMessageRef]:
        message_id_i = int(message_id or 0)
        if message_id_i <= 0:
            return None
        cached = self._cache.get(message_id_i)
        if cached is not None:
            if cached.created_at > time.time() - C.MESSAGE_CACHE_TTL_SECONDS:
                return cached
            self._cache.pop(message_id_i)
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MESSAGE_MAP,
            "message_id": message_id_i,
        })
        if not doc:
            return None
        # A remoção TTL do Mongo é assíncrona. Não reconhecer um documento
        # vencido enquanto o monitor de TTL ainda não o removeu.
        if float(doc.get("created_at") or 0) <= time.time() - C.MESSAGE_CACHE_TTL_SECONDS:
            return None
        expires_at = doc.get("expires_at")
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None
        ref = ChatbotMessageRef.from_doc(doc)
        if ref.message_id and ref.guild_id and ref.channel_id:
            self._cache.set(message_id_i, ref)
            return ref
        return None

    async def cleanup_old(self) -> int:
        cutoff = time.time() - C.MESSAGE_CACHE_TTL_SECONDS
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MESSAGE_MAP,
            "created_at": {"$lt": cutoff},
        })
        return int(getattr(result, "deleted_count", 0) or 0)
