"""CAS, reinício, limpeza e privacidade dos pedidos de ações do chatbot."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pymongo import ReturnDocument

from cogs.chatbot.action_store import (
    ActionStore, DOC_TYPE_ACTION_REQUEST, REQUEST_LIFETIME, RESULT_RETENTION,
)
from cogs.chatbot.db import ensure_indexes


_MISSING = object()


def _get(doc, name):
    for part in name.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return _MISSING
        doc = doc[part]
    return doc


def _matches(doc, query):
    for name, expected in query.items():
        actual = _get(doc, name)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == "$in":
                    if actual not in value:
                        return False
                elif actual is _MISSING:
                    return False
                elif op == "$gt" and not actual > value:
                    return False
                elif op == "$lte" and not actual <= value:
                    return False
        elif actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = deepcopy(docs)

    def sort(self, name, direction):
        self.docs.sort(key=lambda doc: _get(doc, name), reverse=direction < 0)
        return self

    def limit(self, count):
        if count:
            self.docs = self.docs[:count]
        return self

    def __aiter__(self):
        self._iterator = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self):
        self.docs = []
        self.indexes = {}
        self._lock = asyncio.Lock()

    async def insert_one(self, doc):
        assert not any(existing["_id"] == doc["_id"] for existing in self.docs)
        self.docs.append(deepcopy(doc))
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, query):
        return next((deepcopy(doc) for doc in self.docs if _matches(doc, query)), None)

    def find(self, query):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def find_one_and_update(self, query, update, *, return_document):
        assert return_document == ReturnDocument.AFTER
        # Competidores chegam à operação juntos; a alteração inteira é atômica.
        await asyncio.sleep(0)
        async with self._lock:
            doc = next((doc for doc in self.docs if _matches(doc, query)), None)
            if doc is None:
                return None
            doc.update(deepcopy(update.get("$set", {})))
            for name in update.get("$unset", {}):
                parts = name.split(".")
                target = doc
                for part in parts[:-1]:
                    target = target.get(part, {})
                target.pop(parts[-1], None)
            return deepcopy(doc)

    async def create_index(self, keys, **kwargs):
        self.indexes[kwargs["name"]] = {"keys": keys, **kwargs}


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def state():
    coll, clock = _Collection(), _Clock()
    return ActionStore(coll, clock=clock), coll, clock


def _data(**changes):
    return {
        "guild_id": 11, "channel_id": 22, "origin_message_id": 33,
        "requester_id": 44, "action": "send_audio",
        "payload": {"text": "Conteúdo privado da fala.", "target_id": 44},
        "ask_permission": True, "staff_role_ids": [55], "base_reply": "Posso falar?",
        **changes,
    }


def _binding(**changes):
    return {"guild_id": 11, "channel_id": 22, "message_id": 66, "actor_id": 44,
            **changes}


async def _pending(store, **changes):
    doc = await store.create(_data(**changes))
    assert await store.bind(doc["request_id"], 66)
    return doc["request_id"]


@pytest.mark.asyncio
async def test_create_normalizes_and_overrides_caller_controlled_state(state):
    store, coll, clock = state
    data = _data(guild_id="11", staff_role_ids=[55, "55", -1], state="succeeded",
                 request_id="chosen", _id="chosen", expires_at=clock.now,
                 message_id=66, execution_token="chosen", approved_by=44)
    doc = await store.create(data)
    assert doc["_id"] == doc["request_id"] != "chosen"
    assert doc["type"] == DOC_TYPE_ACTION_REQUEST
    assert doc["state"] == "created"
    assert doc["guild_id"] == 11
    assert doc["staff_role_ids"] == [55]
    assert doc["expires_at"] == clock.now + REQUEST_LIFETIME
    assert doc["delete_at"] == clock.now + RESULT_RETENTION
    assert "message_id" not in doc and "execution_token" not in doc
    doc["payload"]["text"] = "Changed returned copy"
    assert coll.docs[0]["payload"]["text"] == "Conteúdo privado da fala."
    assert data["request_id"] == "chosen"


@pytest.mark.asyncio
async def test_bind_once_and_claim_requires_bound_message(state):
    store, _, _ = state
    doc = await store.create(_data())
    rid = doc["request_id"]
    assert await store.claim(rid, **_binding()) is None
    assert not await store.bind(rid, 0)
    assert await store.bind(rid, 66)
    assert not await store.bind(rid, 77)
    for changes in ({"guild_id": 99}, {"channel_id": 99}, {"message_id": 99},
                    {"actor_id": 0}):
        assert await store.claim(rid, **_binding(**changes)) is None
    claimed = await store.claim(rid, **_binding())
    assert claimed["message_id"] == 66
    assert claimed["approved_by"] == 44
    assert claimed["state"] == "executing" and claimed["execution_token"]
    assert "delete_at" not in claimed


@pytest.mark.asyncio
async def test_simultaneous_approvals_have_exactly_one_winner(state):
    store, _, _ = state
    rid = await _pending(store)
    claims = await asyncio.gather(*(
        store.claim(rid, **_binding(actor_id=100 + index)) for index in range(12)
    ))
    winners = [doc for doc in claims if doc is not None]
    assert len(winners) == 1
    assert (await store.get(rid))["execution_token"] == winners[0]["execution_token"]


@pytest.mark.asyncio
async def test_approval_and_rejection_race_cannot_both_win(state):
    store, _, _ = state
    rid = await _pending(store)
    claimed, rejected = await asyncio.gather(
        store.claim(rid, **_binding()), store.reject(rid, **_binding()),
    )
    assert int(claimed is not None) + int(rejected) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("final_state", ["succeeded", "failed", "uncertain"])
async def test_only_execution_token_can_finish_and_private_speech_is_purged(state, final_state):
    store, _, clock = state
    rid = await _pending(store)
    claimed = await store.claim(rid, **_binding())
    assert not await store.finish(rid, "wrong", state=final_state, public_result="Concluído.")
    assert not await store.finish(rid, "", state=final_state, public_result="Concluído.")
    assert await store.finish(rid, claimed["execution_token"], state=final_state,
                              public_result="Concluído.")
    doc = await store.get(rid)
    assert doc["state"] == final_state
    assert "text" not in doc["payload"]
    assert doc["payload"]["target_id"] == 44
    assert doc["delete_at"] == clock.now + RESULT_RETENTION
    assert not await store.finish(rid, claimed["execution_token"], state="succeeded",
                                  public_result="Não sobrescrever.")
    with pytest.raises(ValueError):
        await store.finish(rid, claimed["execution_token"], state="pending", public_result="")


@pytest.mark.asyncio
async def test_reject_checks_scope_and_purges_speech(state):
    store, _, _ = state
    rid = await _pending(store)
    for changes in ({"guild_id": 99}, {"channel_id": 99}, {"message_id": 99},
                    {"actor_id": 0}):
        assert not await store.reject(rid, **_binding(**changes))
    assert await store.reject(rid, **_binding(actor_id=77))
    doc = await store.get(rid)
    assert doc["state"] == "rejected" and doc["rejected_by"] == 77
    assert "text" not in doc["payload"]
    assert not await store.reject(rid, **_binding())
    assert await store.claim(rid, **_binding()) is None


@pytest.mark.asyncio
async def test_expiry_cleans_created_pending_but_not_executing(state):
    store, _, clock = state
    unbound = await store.create(_data())
    pending = await _pending(store)
    executing = await _pending(store)
    claimed = await store.claim(executing, **_binding())
    clock.advance(300)
    assert await store.pending() == []
    assert await store.claim(pending, **_binding()) is None
    assert not await store.reject(pending, **_binding())
    assert not await store.bind(unbound["request_id"], 66)
    expired = await store.expire_pending()
    assert {doc["request_id"] for doc in expired} == {unbound["request_id"], pending}
    assert all(doc["state"] == "expired" and "text" not in doc["payload"] for doc in expired)
    assert all(doc["delete_at"] == clock.now + RESULT_RETENTION for doc in expired)
    assert await store.expire_pending() == []
    assert (await store.get(executing))["state"] == "executing"
    assert await store.finish(executing, claimed["execution_token"], state="succeeded",
                              public_result="Fala enviada.")


@pytest.mark.asyncio
async def test_pending_and_message_lookup_restore_only_right_bound_requests(state):
    store, coll, clock = state
    rid = await _pending(store)
    await store.create(_data())
    restored = ActionStore(coll, clock=clock)
    assert [doc["request_id"] for doc in await restored.pending()] == [rid]
    assert await restored.pending(limit=0) == []
    assert await restored.list_for_message(11, 99, 66) == []
    assert [doc["request_id"] for doc in await restored.list_for_message(11, 22, 66)] == [rid]
    claimed = await restored.claim(rid, **_binding())
    assert claimed and await store.pending() == []


@pytest.mark.asyncio
async def test_restart_marks_stale_execution_uncertain_without_retry(state):
    store, coll, clock = state
    rid = await _pending(store)
    claimed = await store.claim(rid, **_binding())
    clock.advance(599)
    restarted = ActionStore(coll, clock=clock)
    assert await restarted.recover_stale_executing() == []
    clock.advance(1)
    recovered = await restarted.recover_stale_executing()
    assert len(recovered) == 1 and recovered[0]["state"] == "uncertain"
    assert "text" not in recovered[0]["payload"]
    assert recovered[0]["delete_at"] == clock.now + RESULT_RETENTION
    assert await restarted.recover_stale_executing() == []
    assert await restarted.claim(rid, **_binding()) is None
    assert not await store.finish(rid, claimed["execution_token"], state="succeeded",
                                  public_result="Resposta atrasada.")


@pytest.mark.asyncio
async def test_cleanup_retention_is_separate_from_expiry_and_never_loses_execution(state):
    store, coll, clock = state
    pending = await _pending(store)
    executing = await _pending(store)
    await store.claim(executing, **_binding())
    clock.advance(300)
    await store.expire_pending()
    # O pedido expirado continua disponível para atualizar botões e contexto.
    assert (await store.get(pending))["state"] == "expired"
    clock.advance(7 * 24 * 60 * 60)
    # Simula somente a regra TTL real registrada para delete_at.
    coll.docs = [doc for doc in coll.docs
                 if "delete_at" not in doc or doc["delete_at"] > clock.now]
    assert await store.get(pending) is None
    assert (await store.get(executing))["state"] == "executing"
    assert len(await store.recover_stale_executing()) == 1
    assert (await store.get(executing))["delete_at"] == clock.now + RESULT_RETENTION


@pytest.mark.asyncio
async def test_recent_results_are_scoped_bounded_and_never_include_private_fields(state):
    store, _, clock = state
    expected = []
    for index in range(4):
        rid = await _pending(store)
        clock.advance(1)
        assert await store.reject(rid, **_binding())
        expected.append(rid)
    other_user = await _pending(store, requester_id=99)
    assert await store.reject(other_user, **_binding())
    await _pending(store)
    results = await store.recent_results(11, 22, 44)
    assert [doc["request_id"] for doc in results] == list(reversed(expected[-3:]))
    assert all("payload" not in doc and "base_reply" not in doc
               and "execution_token" not in doc for doc in results)
    assert "Conteúdo privado" not in str(results)
    assert await store.recent_results(11, 99, 44) == []
    assert await store.recent_results(11, 22, 44, limit=0) == []


@pytest.mark.asyncio
async def test_action_indexes_never_ttl_on_permission_expiry(state):
    _, coll, _ = state
    await ensure_indexes(coll)
    unique = coll.indexes["chatbot_action_request_unique"]
    assert unique["unique"]
    assert unique["partialFilterExpression"] == {"type": DOC_TYPE_ACTION_REQUEST}
    cleanup = coll.indexes["chatbot_action_request_cleanup"]
    assert cleanup["keys"] == [("delete_at", 1)]
    assert cleanup["expireAfterSeconds"] == 0
    assert cleanup["partialFilterExpression"] == {"type": DOC_TYPE_ACTION_REQUEST}
    assert not any(index.get("expireAfterSeconds") == 0 and
                   index.get("partialFilterExpression") == {"type": DOC_TYPE_ACTION_REQUEST} and
                   ("expires_at", 1) in index["keys"] for index in coll.indexes.values())
