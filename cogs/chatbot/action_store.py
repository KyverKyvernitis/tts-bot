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
TERMINAL_STATES = ("succeeded", "failed", "uncertain", "rejected", "expired", "cancelled")
MAX_PLAN_STEPS = 4
AUTOMATIC_ACTIONS = ("send_audio", "speak_voice")


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

    def _new_doc(self, data: dict, *, now: datetime) -> dict:
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
                     "rejected_by", "public_result", "finished_at", "plan_id",
                     "plan_size", "step_index", "step_id", "predecessor_id", "depends_on",
                     "automatic", "publishing_token", "publishing_at", "ready_at",
                     "source_reply_message_id", "card_removed"):
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
            "card_removed": False,
        })
        return doc

    async def create(self, data: dict) -> dict:
        doc = self._new_doc(data, now=self._now())
        await self._coll.insert_one(doc)
        return deepcopy(doc)

    async def create_plan(self, data_list: list[dict]) -> list[dict]:
        """Persiste uma cadeia inteira antes de disponibilizar a primeira etapa.

        Uma interrupção durante a inserção deixa somente etapas bloqueadas. A
        recuperação nunca executa um plano incompleto. Cada etapa seguinte ganha
        seu próprio prazo quando o predecessor conclui com sucesso.
        """
        if not 1 <= len(data_list) <= MAX_PLAN_STEPS:
            raise ValueError(f"Um plano precisa de 1 a {MAX_PLAN_STEPS} etapas.")
        now = self._now()
        docs = [self._new_doc(data, now=now) for data in data_list]
        scope = ("guild_id", "channel_id", "origin_message_id", "requester_id")
        if any(any(doc[key] != docs[0][key] for key in scope) for doc in docs[1:]):
            raise ValueError("Todas as etapas precisam pertencer à mesma conversa e membro.")
        plan_id = str(uuid4())
        for index, doc in enumerate(docs):
            predecessor_id = docs[index - 1]["request_id"] if index else None
            doc.update({
                "plan_id": plan_id, "plan_size": len(docs), "step_index": index,
                "step_id": doc["request_id"], "predecessor_id": predecessor_id,
                "depends_on": [predecessor_id] if predecessor_id else [],
                "state": "blocked",
            })
            await self._coll.insert_one(doc)
        first = await self._activate(docs[0])
        if first is not None:
            docs[0] = first
        return [deepcopy(doc) for doc in docs]

    async def list_for_plan(self, plan_id: str) -> list[dict]:
        cursor = self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST, "plan_id": str(plan_id),
        }).sort("step_index", 1)
        return [doc async for doc in cursor]

    async def _complete_plan(self, doc: dict) -> bool:
        if not doc.get("plan_id"):
            return True
        docs = await self.list_for_plan(doc["plan_id"])
        size = doc.get("plan_size")
        if not isinstance(size, int) or not 1 <= size <= MAX_PLAN_STEPS or len(docs) != size:
            return False
        return all(step.get("step_index") == index and step.get("plan_size") == size
                   and step.get("predecessor_id") == (docs[index - 1]["request_id"] if index else None)
                   for index, step in enumerate(docs))

    async def _activate(self, doc: dict) -> dict | None:
        if not await self._complete_plan(doc):
            return None
        predecessor_id = doc.get("predecessor_id")
        if predecessor_id:
            predecessor = await self.get(predecessor_id)
            if predecessor is None or predecessor.get("state") != "succeeded":
                return None
        now = self._now()
        return await self._coll.find_one_and_update(
            {**self._query(doc["request_id"]), "state": "blocked"},
            {"$set": {"state": "created", "ready_at": now,
                      "expires_at": now + REQUEST_LIFETIME,
                      "delete_at": now + RESULT_RETENTION}},
            return_document=ReturnDocument.AFTER,
        )

    async def _cancel_descendants(self, doc: dict, *, now: datetime | None = None) -> list[dict]:
        if not doc.get("plan_id"):
            return []
        now = now or self._now()
        query = {
            "type": DOC_TYPE_ACTION_REQUEST, "plan_id": doc["plan_id"],
            "step_index": {"$gt": int(doc["step_index"])},
            "state": {"$in": ["blocked", "created", "publishing", "pending"]},
        }
        cancelled = []
        async for step in self._coll.find(query):
            updated = await self._coll.find_one_and_update(
                {**query, "request_id": step["request_id"]},
                self._terminal_update(now, state="cancelled",
                                      public_result="Etapa cancelada: a ação anterior não foi concluída."),
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                cancelled.append(updated)
        return cancelled

    async def _advance(self, doc: dict) -> dict | None:
        if not doc.get("plan_id"):
            return None
        successor = await self._coll.find_one({
            "type": DOC_TYPE_ACTION_REQUEST, "plan_id": doc["plan_id"],
            "predecessor_id": doc["request_id"], "state": "blocked",
        })
        return await self._activate(successor) if successor is not None else None

    async def bind(self, request_id: str, message_id: int) -> bool:
        if int(message_id) <= 0:
            return False
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id), "state": {"$in": ["created", "publishing"]},
             "expires_at": {"$gt": self._now()}},
            {"$set": {"state": "pending", "message_id": int(message_id)}},
            return_document=ReturnDocument.AFTER,
        )
        return updated is not None

    async def claim_publication(self, request_id: str) -> dict | None:
        """Uma única instância prepara o cartão; publicação incerta não é refeita."""
        now = self._now()
        return await self._coll.find_one_and_update(
            {**self._query(request_id), "state": "created", "expires_at": {"$gt": now}},
            {"$set": {"state": "publishing", "publishing_at": now,
                      "publishing_token": str(uuid4())}},
            return_document=ReturnDocument.AFTER,
        )

    async def bind_context(self, request_id: str, message_id: int) -> bool:
        """Guarda a resposta normal sem usá-la como autorização do cartão."""
        if int(message_id) <= 0:
            return False
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id),
             "state": {"$in": ["blocked", "created", "publishing", "pending"]}},
            {"$set": {"source_reply_message_id": int(message_id)}},
            return_document=ReturnDocument.AFTER,
        )
        return updated is not None

    async def fail_unpublished(
        self, request_id: str, *, public_result: str, uncertain: bool = False,
    ) -> bool:
        now = self._now()
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id), "state": {"$in": ["created", "publishing"]}},
            self._terminal_update(now, state="uncertain" if uncertain else "failed",
                                  public_result=public_result),
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            await self._cancel_descendants(updated, now=now)
        return updated is not None

    async def arm_automatic(self, request_id: str) -> bool:
        """Prepara áudio sem cartão; ações privilegiadas nunca passam aqui."""
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id), "state": "created", "ask_permission": False,
             "action": {"$in": list(AUTOMATIC_ACTIONS)},
             "expires_at": {"$gt": self._now()}},
            {"$set": {"state": "pending", "message_id": 0, "automatic": True}},
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

    async def claim_automatic(
        self, request_id: str, *, guild_id: int, channel_id: int, actor_id: int,
    ) -> dict | None:
        now = self._now()
        if int(actor_id) <= 0:
            return None
        return await self._coll.find_one_and_update(
            {**self._bound_query(request_id, guild_id=guild_id, channel_id=channel_id,
                                 message_id=0),
             "state": "pending", "automatic": True, "ask_permission": False,
             "action": {"$in": list(AUTOMATIC_ACTIONS)},
             "requester_id": int(actor_id), "expires_at": {"$gt": now}},
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
        if updated is not None:
            await self._cancel_descendants(updated, now=now)
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
        if updated is not None:
            if state == "succeeded":
                await self._advance(updated)
            else:
                await self._cancel_descendants(updated)
        return updated is not None

    async def recover_ready(self, limit: int = 500) -> list[dict]:
        """Repara transições interrompidas e devolve somente etapas prontas.

        Não reclama ações em execução nem muda seus tokens. O chamador ainda
        precisa armar/criar o cartão e ganhar o CAS antes de qualquer efeito.
        """
        if int(limit) <= 0:
            return []
        query = {"type": DOC_TYPE_ACTION_REQUEST, "state": "blocked"}
        async for doc in self._coll.find(query):
            predecessor = await self.get(doc["predecessor_id"]) if doc.get("predecessor_id") else None
            if predecessor and predecessor.get("state") in TERMINAL_STATES and predecessor["state"] != "succeeded":
                await self._cancel_descendants(predecessor)
            elif not doc.get("predecessor_id") or (predecessor and predecessor.get("state") == "succeeded"):
                await self._activate(doc)
        now = self._now()
        cursor = self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST, "state": {"$in": ["created", "pending"]},
            "expires_at": {"$gt": now},
        }).sort("created_at", 1)
        ready = []
        async for doc in cursor:
            if doc["state"] == "created" or (
                doc.get("automatic") is True and doc.get("message_id") == 0
                and doc.get("ask_permission") is False
                and doc.get("action") in AUTOMATIC_ACTIONS
            ):
                ready.append(doc)
                if len(ready) >= min(int(limit), 500):
                    break
        return ready

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

    async def cards_to_remove(self, limit: int = 500) -> list[dict]:
        if int(limit) <= 0:
            return []
        cursor = self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST,
            "state": {"$in": ["executing", *TERMINAL_STATES]},
            "message_id": {"$gt": 0}, "card_removed": {"$ne": True},
        }).sort("created_at", 1).limit(min(int(limit), 500))
        return [doc async for doc in cursor]

    async def mark_card_removed(self, request_id: str) -> bool:
        updated = await self._coll.find_one_and_update(
            {**self._query(request_id), "message_id": {"$gt": 0},
             "state": {"$in": ["executing", *TERMINAL_STATES]}},
            {"$set": {"card_removed": True, "card_removed_at": self._now()}},
            return_document=ReturnDocument.AFTER,
        )
        return updated is not None

    async def expire_pending(self) -> list[dict]:
        now = self._now()
        query = {"type": DOC_TYPE_ACTION_REQUEST,
                 "state": {"$in": ["created", "publishing", "pending"]},
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
                changed.extend(await self._cancel_descendants(updated, now=now))
        # Não expira uma etapa completa ainda bloqueada: seu prazo começa quando
        # ficar pronta. Um plano parcialmente gravado, porém, não pode permanecer
        # com texto privado esperando uma etapa que jamais existiu.
        async for doc in self._coll.find({
            "type": DOC_TYPE_ACTION_REQUEST, "state": "blocked",
            "created_at": {"$lte": now - REQUEST_LIFETIME},
        }):
            if await self._complete_plan(doc):
                continue
            updated = await self._coll.find_one_and_update(
                {**self._query(doc["request_id"]), "state": "blocked"},
                self._terminal_update(now, state="expired", public_result="Plano incompleto expirado."),
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                changed.append(updated)
                changed.extend(await self._cancel_descendants(updated, now=now))
        return changed

    async def recover_stale_publishing(self, age_seconds: int = 300) -> list[dict]:
        now = self._now()
        cutoff = now - timedelta(seconds=max(1, int(age_seconds)))
        query = {"type": DOC_TYPE_ACTION_REQUEST, "state": "publishing",
                 "publishing_at": {"$lte": cutoff}}
        changed = []
        async for doc in self._coll.find(query):
            updated = await self._coll.find_one_and_update(
                {**query, "request_id": doc["request_id"]},
                self._terminal_update(
                    now, state="uncertain", public_result="Publicação interrompida; o pedido não será reenviado."
                ),
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                changed.append(updated)
                changed.extend(await self._cancel_descendants(updated, now=now))
        return changed

    async def cancel_legacy_audio_approvals(self) -> list[dict]:
        """Pedidos antigos de áudio não viram reprodução automática após update."""
        now = self._now()
        query = {"type": DOC_TYPE_ACTION_REQUEST, "action": {"$in": list(AUTOMATIC_ACTIONS)},
                 "ask_permission": True,
                 "state": {"$in": ["blocked", "created", "publishing", "pending"]}}
        changed = []
        async for doc in self._coll.find(query):
            updated = await self._coll.find_one_and_update(
                {**query, "request_id": doc["request_id"]},
                self._terminal_update(
                    now, state="cancelled", public_result="Pedido antigo de áudio cancelado após a atualização."
                ),
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None:
                changed.append(updated)
                changed.extend(await self._cancel_descendants(updated, now=now))
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
                changed.extend(await self._cancel_descendants(updated, now=now))
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
