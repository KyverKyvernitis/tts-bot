"""Pedidos de ações persistentes, com execução única e contexto público.

Permissões são verificadas pelo executor antes de ``claim``. Este armazenamento
garante que um botão ligado a outra mensagem não aprove o pedido e que somente
uma aprovação ganhe a transição atômica para execução.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from pymongo import ReturnDocument


DOC_TYPE_ACTION_REQUEST = "chatbot_action_request"
REQUEST_LIFETIME = timedelta(minutes=5)
RESULT_RETENTION = timedelta(days=7)
TERMINAL_STATES = ("succeeded", "failed", "uncertain", "rejected", "expired")


class ActionStore:
    """Estado durável dos pedidos; uma ação incerta nunca é repetida aqui."""

    def __init__(self, coll, *, clock: Callable[[], datetime] | None = None):
        self._coll = coll
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._clock()
        # O PyMongo costuma devolver datas UTC sem tzinfo por padrão. Aceitar
        # relógios assim mantém comparações/consultas coerentes em ambos os modos.
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    @staticmethod
    def _query(request_id: str) -> dict:
        return {"type": DOC_TYPE_ACTION_REQUEST, "request_id": str(request_id)}

    @classmethod
    def _bound_query(
        cls, request_id: str, *, guild_id: int, channel_id: int, message_id: int
    ) -> dict:
        return {
            **cls._query(request_id),
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": int(message_id),
        }

    @staticmethod
    def _terminal_update(now: datetime, *, state: str, public_result: str) -> dict:
        return {
            "$set": {
                "state": state,
                "public_result": str(public_result),
                "finished_at": now,
                "delete_at": now + RESULT_RETENTION,
            },
            "$unset": {"payload.text": ""},
        }

    async def create(self, data: dict) -> dict:
        now = self._now()
        doc = deepcopy(data)
        for name in ("guild_id", "channel_id", "origin_message_id", "requester_id"):
            doc[name] = int(doc[name])
            if doc[name] <= 0:
                raise ValueError(f"{name} deve identificar um objeto do Discord.")
        if not isinstance(doc.get("payload"), dict):
            raise ValueError("O pedido precisa de um payload estruturado.")
        if not isinstance(doc.get("action"), str) or not doc["action"].strip():
            raise ValueError("O pedido precisa identificar uma ação.")
        doc["staff_role_ids"] = list(dict.fromkeys(
            int(value) for value in doc.get("staff_role_ids", ()) if int(value) > 0
        ))
        doc["ask_permission"] = bool(doc.get("ask_permission", False))
        doc["base_reply"] = str(doc.get("base_reply") or "")
        # O chamador nunca escolhe IDs, estados, aprovações ou prazos internos.
        for name in ("message_id", "execution_token", "approved_by", "executing_at",
                     "rejected_by", "public_result", "finished_at"):
            doc.pop(name, None)
        request_id = str(uuid4())
        doc.update({
            "_id": request_id,
            "request_id": request_id,
            "type": DOC_TYPE_ACTION_REQUEST,
            "state": "created",
            "created_at": now,
            "expires_at": now + REQUEST_LIFETIME,
            "delete_at": now + RESULT_RETENTION,
        })
        await self._coll.insert_one(doc)
        return deepcopy(doc)

    async def bind(self, request_id: str, message_id: int) -> bool:
        if int(message_id) <= 0:
            return False
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id), "state": "created", "expires_at": {"$gt": self._now()}},
            {"$set": {"state": "pending", "message_id": int(message_id)}},
            return_document=ReturnDocument.AFTER,
        )
        return updated is not None

    async def get(self, request_id: str) -> dict | None:
        return await self._coll.find_one(self._query(request_id))

    async def claim(
        self, request_id: str, *, guild_id: int, channel_id: int,
        message_id: int, actor_id: int,
    ) -> dict | None:
        now = self._now()
        if int(actor_id) <= 0 or int(message_id) <= 0:
            return None
        return await self._coll.find_one_and_update(
            {**self._bound_query(request_id, guild_id=guild_id, channel_id=channel_id,
                                 message_id=message_id),
             "state": "pending", "expires_at": {"$gt": now}},
            {"$set": {"state": "executing", "execution_token": str(uuid4()),
                      "approved_by": int(actor_id), "executing_at": now},
             "$unset": {"delete_at": ""}},
            return_document=ReturnDocument.AFTER,
        )

    async def reject(
        self, request_id: str, *, guild_id: int, channel_id: int,
        message_id: int, actor_id: int,
    ) -> bool:
        now = self._now()
        if int(actor_id) <= 0 or int(message_id) <= 0:
            return False
        update = self._terminal_update(
            now, state="rejected", public_result="Pedido rejeitado."
        )
        update["$set"]["rejected_by"] = int(actor_id)
        updated = await self._coll.find_one_and_update(
            {**self._bound_query(request_id, guild_id=guild_id, channel_id=channel_id,
                                 message_id=message_id),
             "state": "pending", "expires_at": {"$gt": now}},
            update, return_document=ReturnDocument.AFTER,
        )
        return updated is not None

    async def finish(
        self, request_id: str, execution_token: str, *, state: str, public_result: str,
    ) -> bool:
        if state not in ("succeeded", "failed", "uncertain"):
            raise ValueError("Estado final inválido para uma execução.")
        if not execution_token:
            return False
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id), "state": "executing",
             "execution_token": str(execution_token)},
            self._terminal_update(self._now(), state=state, public_result=public_result),
            return_document=ReturnDocument.AFTER,
        )
        return updated is not None

    async def list_for_message(
        self, guild_id: int, channel_id: int, message_id: int,
    ) -> list[dict]:
        cursor = self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST, "guild_id": int(guild_id),
            "channel_id": int(channel_id), "message_id": int(message_id),
        }).sort("created_at", 1)
        return [doc async for doc in cursor]

    async def pending(self, limit: int = 500) -> list[dict]:
        cursor = self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST, "state": "pending",
            "expires_at": {"$gt": self._now()}, "message_id": {"$gt": 0},
        }).sort("created_at", 1).limit(max(0, min(int(limit), 500)))
        # Mongo's limit(0) means unbounded rather than empty.
        if int(limit) <= 0:
            return []
        return [doc async for doc in cursor]

    async def expire_pending(self) -> list[dict]:
        now = self._now()
        query = {"type": DOC_TYPE_ACTION_REQUEST, "state": {"$in": ["created", "pending"]},
                 "expires_at": {"$lte": now}}
        changed = []
        async for doc in self._coll.find(query):
            updated = await self._coll.find_one_and_update(
                {**query, "request_id": doc["request_id"]},
                self._terminal_update(now, state="expired", public_result="Pedido expirado."),
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                changed.append(updated)
        return changed

    async def recover_stale_executing(self, age_seconds: int = 600) -> list[dict]:
        now = self._now()
        cutoff = now - timedelta(seconds=max(1, int(age_seconds)))
        query = {"type": DOC_TYPE_ACTION_REQUEST, "state": "executing",
                 "executing_at": {"$lte": cutoff}}
        changed = []
        async for doc in self._coll.find(query):
            updated = await self._coll.find_one_and_update(
                {**query, "request_id": doc["request_id"]},
                self._terminal_update(
                    now, state="uncertain",
                    public_result="Execução interrompida; resultado precisa ser verificado."
                ),
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                changed.append(updated)
        return changed

    async def recent_results(
        self, guild_id: int, channel_id: int, requester_id: int, limit: int = 3,
    ) -> list[dict]:
        if int(limit) <= 0:
            return []
        cursor = self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST, "guild_id": int(guild_id),
            "channel_id": int(channel_id), "requester_id": int(requester_id),
            "state": {"$in": list(TERMINAL_STATES)},
        }).sort("finished_at", -1).limit(min(int(limit), 20))
        public_fields = (
            "request_id", "guild_id", "channel_id", "origin_message_id", "message_id",
            "requester_id", "action", "state", "public_result", "created_at", "finished_at",
        )
        return [{key: deepcopy(doc[key]) for key in public_fields if key in doc}
                async for doc in cursor]
