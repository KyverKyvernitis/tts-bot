"""Memória do bot isolada por servidor, canal e nível de privacidade.

Turnos completos e gerações de reset impedem que uma resposta atrasada
reintroduza contexto apagado. Históricos arquivados nunca entram no prompt.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from . import constants as C


@dataclass(frozen=True)
class MemoryEpoch:
    global_generation: int = 0
    guild_generation: int = 0
    user_generation: int = 0


@dataclass
class MemoryEntry:
    role: str
    content: str
    user_id: int = 0
    user_name: str = ""
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
    def from_dict(cls, data: dict) -> "MemoryEntry":
        return cls(
            role=str(data.get("role") or "user"),
            content=str(data.get("content") or ""),
            user_id=int(data.get("user_id") or 0),
            user_name=str(data.get("user_name") or ""),
            timestamp=float(data.get("timestamp") or time.time()),
        )


def visibility_scope_for(channel_id: int, *, is_nsfw: bool, is_private: bool) -> str:
    """Produz um rótulo semântico; ``channel_id`` continua na chave Mongo."""
    if is_private:
        return f"private:{int(channel_id)}"
    if is_nsfw:
        return f"nsfw:{int(channel_id)}"
    return f"channel:{int(channel_id)}"


class MemoryStore:
    _MAX_LOCKS = 512

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        self._locks: "OrderedDict[tuple[int, int, str, int], asyncio.Lock]" = OrderedDict()

    def _lock_for(
        self, guild_id: int, channel_id: int, visibility_scope: str, user_id: int
    ) -> asyncio.Lock:
        key = (int(guild_id), int(channel_id), str(visibility_scope), int(user_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        else:
            self._locks.move_to_end(key)
        if len(self._locks) > self._MAX_LOCKS:
            for old_key, candidate in list(self._locks.items()):
                if old_key != key and not candidate.locked():
                    self._locks.pop(old_key, None)
                    break
        return lock

    async def capture_epoch(self, guild_id: int, user_id: int) -> MemoryEpoch:
        gid, uid = int(guild_id), int(user_id)
        keys = ("global", f"guild:{gid}", f"user:{gid}:{uid}")
        docs: dict[str, dict] = {}
        cursor = self._coll.find({
            "type": C.DOC_TYPE_MEMORY_EPOCH,
            "epoch_key": {"$in": list(keys)},
        })
        async for doc in cursor:
            docs[str(doc.get("epoch_key") or "")] = doc
        global_doc = docs.get(keys[0]) or {}
        guild_doc = docs.get(keys[1]) or {}
        user_doc = docs.get(keys[2]) or {}
        return MemoryEpoch(
            global_generation=int(global_doc.get("generation") or 0),
            guild_generation=int(guild_doc.get("guild_generation") or 0),
            user_generation=int(user_doc.get("generation") or 0),
        )

    @staticmethod
    def _flatten_turns(
        doc: Optional[dict], *, exclude_user_id: int = 0,
        user_generations: Optional[dict[int, int]] = None,
    ) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        for turn in (doc or {}).get("turns") or []:
            if not isinstance(turn, dict):
                continue
            turn_user_id = int(turn.get("user_id") or 0)
            if exclude_user_id and turn_user_id == int(exclude_user_id):
                continue
            if user_generations is not None:
                current_generation = int(user_generations.get(turn_user_id, 0))
                stored_generation = int(turn.get("user_generation") or 0)
                if stored_generation != current_generation:
                    # A delayed provider response may finish after this user
                    # reset their data. The old turn can physically arrive in
                    # the shared document, but must never become visible again.
                    continue
            user = turn.get("user")
            assistant = turn.get("assistant")
            if isinstance(user, dict):
                entry = MemoryEntry.from_dict(user)
                entry.user_id = turn_user_id or entry.user_id
                entry.user_name = str(turn.get("user_name") or entry.user_name)
                out.append(entry)
            if isinstance(assistant, dict):
                entry = MemoryEntry.from_dict(assistant)
                # A autoria do turno também acompanha a resposta, permitindo
                # excluir a troca inteira do contexto coletivo do mesmo usuário.
                entry.user_id = turn_user_id
                entry.user_name = str(turn.get("user_name") or "")
                out.append(entry)
        return out

    async def _current_user_generations(
        self, guild_id: int, user_ids: set[int]
    ) -> dict[int, int]:
        ids = {int(user_id) for user_id in user_ids if int(user_id) > 0}
        generations = {user_id: 0 for user_id in ids}
        if not ids:
            return generations
        prefix = f"user:{int(guild_id)}:"
        keys = [f"{prefix}{user_id}" for user_id in ids]
        cursor = self._coll.find({
            "type": C.DOC_TYPE_MEMORY_EPOCH,
            "epoch_key": {"$in": keys},
        })
        async for doc in cursor:
            key = str(doc.get("epoch_key") or "")
            if not key.startswith(prefix):
                continue
            try:
                user_id = int(key[len(prefix):])
            except ValueError:
                continue
            if user_id in generations:
                generations[user_id] = int(doc.get("generation") or 0)
        return generations

    def _query(
        self,
        *,
        scope: str,
        guild_id: int,
        channel_id: int,
        visibility_scope: str,
        user_id: int,
        epoch: MemoryEpoch,
    ) -> dict:
        return {
            "type": C.DOC_TYPE_MEMORY_V3,
            "scope": scope,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "visibility_scope": str(visibility_scope),
            "global_generation": int(epoch.global_generation),
            "guild_generation": int(epoch.guild_generation),
            "user_id": int(user_id if scope == "user" else 0),
            "user_generation": int(epoch.user_generation if scope == "user" else 0),
        }

    async def load_context(
        self,
        guild_id: int,
        user_id: int,
        *,
        channel_id: int,
        visibility_scope: str,
        include_collective: bool = True,
    ) -> tuple[MemoryEpoch, list[MemoryEntry], list[MemoryEntry]]:
        epoch = await self.capture_epoch(guild_id, user_id)
        user_query = self._query(
            scope="user", guild_id=guild_id, channel_id=channel_id,
            visibility_scope=visibility_scope, user_id=user_id, epoch=epoch,
        )
        if not include_collective:
            user_doc = await self._coll.find_one(user_query)
            return epoch, self._flatten_turns(user_doc), []
        guild_query = self._query(
            scope="guild", guild_id=guild_id, channel_id=channel_id,
            visibility_scope=visibility_scope, user_id=0, epoch=epoch,
        )
        user_doc, guild_doc = await asyncio.gather(
            self._coll.find_one(user_query), self._coll.find_one(guild_query)
        )
        guild_user_ids = {
            int(turn.get("user_id") or 0)
            for turn in (guild_doc or {}).get("turns") or []
            if isinstance(turn, dict)
        }
        generations = await self._current_user_generations(guild_id, guild_user_ids)
        return (
            epoch,
            self._flatten_turns(user_doc),
            self._flatten_turns(
                guild_doc, exclude_user_id=user_id,
                user_generations=generations,
            ),
        )

    async def get_user_history(
        self,
        guild_id: int,
        user_id: int,
        *,
        channel_id: int = 0,
        visibility_scope: str = "channel:0",
        epoch: Optional[MemoryEpoch] = None,
    ) -> list[MemoryEntry]:
        current = epoch or await self.capture_epoch(guild_id, user_id)
        doc = await self._coll.find_one(self._query(
            scope="user", guild_id=guild_id, channel_id=channel_id,
            visibility_scope=visibility_scope, user_id=user_id, epoch=current,
        ))
        return self._flatten_turns(doc)

    async def get_guild_history(
        self,
        guild_id: int,
        *,
        current_user_id: int = 0,
        channel_id: int = 0,
        visibility_scope: str = "channel:0",
        epoch: Optional[MemoryEpoch] = None,
    ) -> list[MemoryEntry]:
        current = epoch or await self.capture_epoch(guild_id, current_user_id)
        doc = await self._coll.find_one(self._query(
            scope="guild", guild_id=guild_id, channel_id=channel_id,
            visibility_scope=visibility_scope, user_id=0, epoch=current,
        ))
        guild_user_ids = {
            int(turn.get("user_id") or 0)
            for turn in (doc or {}).get("turns") or []
            if isinstance(turn, dict)
        }
        generations = await self._current_user_generations(guild_id, guild_user_ids)
        return self._flatten_turns(
            doc, exclude_user_id=current_user_id,
            user_generations=generations,
        )

    @staticmethod
    def _turn(
        *, user_id: int, user_name: str, user_message: str,
        assistant_message: str, user_generation: int = 0,
    ) -> dict:
        now = time.time()
        safe_user = str(user_message or "")[:C.MAX_STORED_MESSAGE_CHARS]
        safe_assistant = str(assistant_message or "")[:C.MAX_STORED_MESSAGE_CHARS]
        safe_name = str(user_name or "")[:80]
        return {
            "turn_id": f"{time.time_ns()}:{int(user_id)}",
            "user_id": int(user_id),
            "user_generation": int(user_generation),
            "user_name": safe_name,
            "timestamp": now,
            "user": MemoryEntry(
                role="user", content=safe_user, user_id=int(user_id),
                user_name=safe_name, timestamp=now,
            ).to_dict(),
            "assistant": MemoryEntry(
                role="assistant", content=safe_assistant, user_id=int(user_id),
                user_name=safe_name, timestamp=now + 0.001,
            ).to_dict(),
        }

    async def _append(
        self, query: dict, turn: dict, *, max_messages: int
    ) -> None:
        now = time.time()
        max_turns = max(1, int(max_messages) // 2)
        identity = dict(query)
        identity["created_at"] = now
        await self._coll.update_one(
            query,
            {
                "$push": {"turns": {"$each": [turn], "$slice": -max_turns}},
                "$set": {"updated_at": now, "schema_version": C.CHATBOT_SCHEMA_VERSION},
                "$setOnInsert": identity,
            },
            upsert=True,
        )

    async def append_turn(
        self,
        guild_id: int,
        user_id: int,
        *,
        channel_id: int,
        visibility_scope: str,
        epoch: MemoryEpoch,
        user_message: str,
        user_name: str,
        assistant_message: str,
        user_history_size: int = C.DEFAULT_HISTORY_SIZE,
    ) -> None:
        turn = self._turn(
            user_id=user_id, user_name=user_name, user_message=user_message,
            assistant_message=assistant_message,
            user_generation=epoch.user_generation,
        )
        lock = self._lock_for(guild_id, channel_id, visibility_scope, user_id)
        async with lock:
            user_query = self._query(
                scope="user", guild_id=guild_id, channel_id=channel_id,
                visibility_scope=visibility_scope, user_id=user_id, epoch=epoch,
            )
            guild_query = self._query(
                scope="guild", guild_id=guild_id, channel_id=channel_id,
                visibility_scope=visibility_scope, user_id=0, epoch=epoch,
            )
            # Os dois documentos são atualizados em paralelo; cada `$push` é
            # atômico no Mongo. A geração torna reset/cancelamento seguros.
            await asyncio.gather(
                self._append(
                    user_query, turn, max_messages=user_history_size,
                ),
                self._append(
                    guild_query, turn, max_messages=C.GUILD_MEMORY_MAX_MESSAGES,
                ),
            )

    async def clear_user_history(
        self, guild_id: int, user_id: int
    ) -> int:
        gid, uid = int(guild_id), int(user_id)
        await self._coll.update_one(
            {"type": C.DOC_TYPE_MEMORY_EPOCH, "epoch_key": f"user:{gid}:{uid}"},
            {
                "$inc": {"generation": 1},
                "$set": {"updated_at": time.time(), "schema_version": C.CHATBOT_SCHEMA_VERSION},
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MEMORY_EPOCH,
                    "epoch_key": f"user:{gid}:{uid}",
                    "guild_id": gid,
                    "user_id": uid,
                    "created_at": time.time(),
                },
            }, upsert=True,
        )
        user_query: dict = {
            "type": C.DOC_TYPE_MEMORY_V3,
            "scope": "user", "guild_id": gid, "user_id": uid,
        }
        guild_query: dict = {
            "type": C.DOC_TYPE_MEMORY_V3, "scope": "guild", "guild_id": gid,
        }
        removed = await self._coll.delete_many(user_query)
        pulled = await self._coll.update_many(
            guild_query, {"$pull": {"turns": {"user_id": uid}}},
        )
        extra = await self._clear_conversation_data(guild_id=gid, user_id=uid)
        return int(removed.deleted_count + pulled.modified_count + extra)

    async def _clear_conversation_data(self, *, guild_id=None, user_id=None) -> int:
        # Além da invalidação por geração, resets removem o conteúdo pessoal
        # já armazenado. Request logs de moderação permanecem separados.
        removed = 0
        for kind, user_field in (("chatbot_conversation_preferences", "user_id"),
                                 ("chatbot_conversation_fact", "user_id"),
                                 ("chatbot_sent_reply", "requester_id")):
            query = {"type": kind}
            if guild_id is not None:
                query["guild_id"] = int(guild_id)
            if user_id is not None:
                query[user_field] = int(user_id)
            result = await self._coll.delete_many(query)
            removed += int(result.deleted_count)
        return removed

    async def clear_guild_history(self, guild_id: int) -> int:
        return await self.clear_all_guild_memory(guild_id)

    async def clear_all_guild_memory(self, guild_id: int) -> int:
        gid = int(guild_id)
        await self._coll.update_one(
            {"type": C.DOC_TYPE_MEMORY_EPOCH, "epoch_key": f"guild:{gid}"},
            {
                "$inc": {"guild_generation": 1},
                "$set": {"updated_at": time.time(), "schema_version": C.CHATBOT_SCHEMA_VERSION},
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MEMORY_EPOCH,
                    "epoch_key": f"guild:{gid}", "guild_id": gid,
                    "created_at": time.time(),
                },
            }, upsert=True,
        )
        query: dict = {
            "type": C.DOC_TYPE_MEMORY_V3,
            "guild_id": gid,
        }
        result = await self._coll.delete_many(query)
        extra = await self._clear_conversation_data(guild_id=gid)
        return int(result.deleted_count + extra)

    async def clear_all_memory_everywhere(self) -> int:
        await self._coll.update_one(
            {"type": C.DOC_TYPE_MEMORY_EPOCH, "epoch_key": "global"},
            {
                "$inc": {"generation": 1},
                "$set": {"updated_at": time.time(), "schema_version": C.CHATBOT_SCHEMA_VERSION},
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MEMORY_EPOCH,
                    "epoch_key": "global", "created_at": time.time(),
                },
            }, upsert=True,
        )
        result = await self._coll.delete_many({
            "type": C.DOC_TYPE_MEMORY_V3,
        })
        extra = await self._clear_conversation_data()
        return int(result.deleted_count + extra)
