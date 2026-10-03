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
    MAX_PLAN_STEPS, TERMINAL_STATES,
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
                elif op == "$ne":
                    if actual == value:
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
    plan_lookup = coll.indexes["chatbot_action_plan_lookup"]
    assert plan_lookup["keys"] == [("type", 1), ("plan_id", 1), ("step_index", 1)]
    assert plan_lookup["partialFilterExpression"] == {"type": DOC_TYPE_ACTION_REQUEST}
    assert not any(index.get("expireAfterSeconds") == 0 and
                   index.get("partialFilterExpression") == {"type": DOC_TYPE_ACTION_REQUEST} and
                   ("expires_at", 1) in index["keys"] for index in coll.indexes.values())


def _plan_data():
    return [
        _data(action="join_voice", ask_permission=True,
              payload={"target_id": 44, "voice_channel_id": 77}),
        _data(action="speak_voice", ask_permission=False,
              payload={"text": "Fala privada depois da entrada.", "voice_channel_id": 77}),
        _data(action="ban_member", ask_permission=True,
              payload={"target_id": 88, "reason": "Motivo da primeira moderação."}),
        _data(action="ban_member", ask_permission=True,
              payload={"target_id": 99, "reason": "Motivo da segunda moderação."}),
    ]


@pytest.mark.asyncio
async def test_plan_makes_only_first_step_ready_and_keeps_independent_authorization(state):
    store, _, clock = state
    docs = await store.create_plan(_plan_data())
    assert len(docs) == MAX_PLAN_STEPS
    assert len({doc["plan_id"] for doc in docs}) == 1
    assert [doc["state"] for doc in docs] == ["created", "blocked", "blocked", "blocked"]
    assert [doc["step_index"] for doc in docs] == list(range(4))
    assert docs[0]["depends_on"] == []
    assert docs[1]["depends_on"] == [docs[0]["request_id"]]
    assert [doc["request_id"] for doc in await store.recover_ready()] == [docs[0]["request_id"]]
    for doc in docs[1:]:
        assert not await store.bind(doc["request_id"], 66)
        assert not await store.arm_automatic(doc["request_id"])
        assert await store.claim(doc["request_id"], **_binding()) is None
    assert await store.bind(docs[0]["request_id"], 66)
    entered = await store.claim(docs[0]["request_id"], **_binding(actor_id=111))
    clock.advance(250)
    assert await store.finish(docs[0]["request_id"], entered["execution_token"],
                              state="succeeded", public_result="Entrei na call.")
    next_doc = await store.get(docs[1]["request_id"])
    assert next_doc["state"] == "created"
    assert next_doc["expires_at"] == clock.now + REQUEST_LIFETIME
    assert "approved_by" not in next_doc and "execution_token" not in next_doc
    assert (await store.get(docs[2]["request_id"]))["state"] == "blocked"


@pytest.mark.asyncio
async def test_chain_speech_uses_requester_and_each_ban_gets_its_own_card_and_claim(state):
    store, _, _ = state
    docs = await store.create_plan(_plan_data())
    ids = [doc["request_id"] for doc in docs]
    assert await store.bind(ids[0], 66)
    entered = await store.claim(ids[0], **_binding(actor_id=111))
    assert await store.finish(ids[0], entered["execution_token"],
                              state="succeeded", public_result="Entrei.")
    assert await store.arm_automatic(ids[1])
    assert await store.claim_automatic(ids[1], guild_id=11, channel_id=22, actor_id=111) is None
    spoken = await store.claim_automatic(ids[1], guild_id=11, channel_id=22, actor_id=44)
    assert spoken["approved_by"] == 44 and spoken["message_id"] == 0
    assert await store.finish(ids[1], spoken["execution_token"],
                              state="succeeded", public_result="Falei na call.")
    assert not await store.arm_automatic(ids[2])
    assert await store.claim(ids[2], **_binding()) is None
    assert await store.bind(ids[2], 67)
    assert await store.claim(ids[2], **_binding()) is None
    first_ban = await store.claim(ids[2], **_binding(message_id=67, actor_id=111))
    assert await store.finish(ids[2], first_ban["execution_token"],
                              state="succeeded", public_result="Membro banido.")
    assert (await store.get(ids[3]))["state"] == "created"
    assert await store.claim(ids[3], **_binding(message_id=67, actor_id=111)) is None
    assert await store.bind(ids[3], 68)
    second_ban = await store.claim(ids[3], **_binding(message_id=68, actor_id=222))
    assert second_ban["approved_by"] == 222
    assert first_ban["execution_token"] != second_ban["execution_token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state_name", ["failed", "uncertain", "rejected", "expired"])
async def test_non_success_cancels_entire_tail_and_purges_all_private_speech(state, state_name):
    store, _, clock = state
    docs = await store.create_plan([_data(), _data(), _data(), _data()])
    ids = [doc["request_id"] for doc in docs]
    assert await store.bind(ids[0], 66)
    if state_name == "rejected":
        assert await store.reject(ids[0], **_binding())
    elif state_name == "expired":
        clock.advance(300)
        await store.expire_pending()
    else:
        claimed = await store.claim(ids[0], **_binding())
        assert await store.finish(ids[0], claimed["execution_token"],
                                  state=state_name, public_result="Não concluído.")
    persisted = [await store.get(rid) for rid in ids]
    assert [doc["state"] for doc in persisted] == [state_name, "cancelled", "cancelled", "cancelled"]
    assert "cancelled" in TERMINAL_STATES
    assert all("text" not in doc["payload"] for doc in persisted)
    assert await store.recover_ready() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_automatic_audio_claim_is_once_bound_to_requester_and_requires_no_card(state, action):
    store, _, _ = state
    doc = await store.create(_data(action=action, ask_permission=False))
    rid = doc["request_id"]
    assert await store.claim_automatic(rid, guild_id=11, channel_id=22, actor_id=44) is None
    assert await store.arm_automatic(rid)
    assert not await store.arm_automatic(rid)
    assert await store.pending() == []
    assert await store.claim(rid, **_binding(message_id=0)) is None
    for wrong in ({"guild_id": 99}, {"channel_id": 99}, {"actor_id": 99}, {"actor_id": 0}):
        scope = {"guild_id": 11, "channel_id": 22, "actor_id": 44, **wrong}
        assert await store.claim_automatic(rid, **scope) is None
    claims = await asyncio.gather(*(
        store.claim_automatic(rid, guild_id=11, channel_id=22, actor_id=44)
        for _ in range(12)
    ))
    winners = [value for value in claims if value is not None]
    assert len(winners) == 1 and winners[0]["message_id"] == 0
    assert await store.finish(rid, winners[0]["execution_token"],
                              state="succeeded", public_result="Áudio enviado.")
    assert "text" not in (await store.get(rid))["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,asks", [("ban_member", False), ("join_voice", False),
                                        ("send_audio", True), ("speak_voice", True)])
async def test_automatic_claim_never_arms_privileged_or_permission_requested_actions(state, action, asks):
    store, _, _ = state
    doc = await store.create(_data(action=action, ask_permission=asks))
    assert not await store.arm_automatic(doc["request_id"])
    assert await store.claim_automatic(doc["request_id"], guild_id=11, channel_id=22,
                                       actor_id=44) is None


@pytest.mark.asyncio
async def test_restart_recovers_pending_auto_but_never_executing_or_completed(state):
    store, coll, clock = state
    doc = await store.create(_data(ask_permission=False))
    rid = doc["request_id"]
    assert await store.arm_automatic(rid)
    restored = ActionStore(coll, clock=clock)
    assert [doc["request_id"] for doc in await restored.recover_ready()] == [rid]
    claimed = await restored.claim_automatic(rid, guild_id=11, channel_id=22, actor_id=44)
    assert await ActionStore(coll, clock=clock).recover_ready() == []
    clock.advance(600)
    await restored.recover_stale_executing()
    assert await restored.recover_ready() == []
    assert not await store.finish(rid, claimed["execution_token"],
                                  state="succeeded", public_result="Retorno atrasado.")


@pytest.mark.asyncio
async def test_restart_repairs_only_successor_after_commit_before_advance(state, monkeypatch):
    store, coll, clock = state
    docs = await store.create_plan(_plan_data())
    ids = [doc["request_id"] for doc in docs]
    assert await store.bind(ids[0], 66)
    claimed = await store.claim(ids[0], **_binding())

    async def interrupted(doc):
        raise RuntimeError("Process stopped after recording success")

    monkeypatch.setattr(store, "_advance", interrupted)
    with pytest.raises(RuntimeError):
        await store.finish(ids[0], claimed["execution_token"],
                           state="succeeded", public_result="Entrei.")
    assert (await store.get(ids[1]))["state"] == "blocked"
    restored = ActionStore(coll, clock=clock)
    ready = await restored.recover_ready()
    assert [doc["request_id"] for doc in ready] == [ids[1]]
    assert (await restored.get(ids[2]))["state"] == "blocked"
    assert await restored.claim(ids[0], **_binding()) is None


@pytest.mark.asyncio
async def test_restart_repairs_interrupted_tail_cancellation(state, monkeypatch):
    store, coll, clock = state
    docs = await store.create_plan([_data(), _data(), _data()])
    assert await store.bind(docs[0]["request_id"], 66)

    async def interrupted(doc, **kwargs):
        raise RuntimeError("Process stopped after recording rejection")

    monkeypatch.setattr(store, "_cancel_descendants", interrupted)
    with pytest.raises(RuntimeError):
        await store.reject(docs[0]["request_id"], **_binding())
    restored = ActionStore(coll, clock=clock)
    assert await restored.recover_ready() == []
    for doc in docs[1:]:
        persisted = await restored.get(doc["request_id"])
        assert persisted["state"] == "cancelled" and "text" not in persisted["payload"]


@pytest.mark.asyncio
async def test_stale_execution_cancels_chain_without_retry_after_restart(state):
    store, coll, clock = state
    docs = await store.create_plan(_plan_data())
    assert await store.bind(docs[0]["request_id"], 66)
    await store.claim(docs[0]["request_id"], **_binding())
    clock.advance(600)
    restored = ActionStore(coll, clock=clock)
    changed = await restored.recover_stale_executing()
    assert [doc["state"] for doc in changed] == ["uncertain", "cancelled", "cancelled", "cancelled"]
    assert await restored.recover_ready() == []
    assert all("text" not in doc["payload"] for doc in changed)


@pytest.mark.asyncio
async def test_plan_limits_scope_validation_and_untrusted_plan_metadata(state):
    store, _, _ = state
    for data in ([], [_data()] * 5, [_data(), _data(requester_id=99)],
                 [_data(), _data(channel_id=99)], [_data(), _data(origin_message_id=99)]):
        with pytest.raises(ValueError):
            await store.create_plan(data)
    docs = await store.create_plan([_data(plan_id="attacker", step_index=3,
                                         depends_on=["made-up"], predecessor_id="made-up")])
    assert docs[0]["plan_id"] != "attacker"
    assert docs[0]["step_index"] == 0 and docs[0]["predecessor_id"] is None
    assert docs[0]["depends_on"] == []
    assert await store.recover_ready(limit=0) == []


@pytest.mark.asyncio
async def test_interrupted_partial_plan_never_becomes_executable_and_private_text_expires(state, monkeypatch):
    store, coll, clock = state
    original_insert = coll.insert_one

    async def partial_insert(doc):
        if len(coll.docs) == 1:
            raise RuntimeError("Stopped during plan insertion")
        await original_insert(doc)

    monkeypatch.setattr(coll, "insert_one", partial_insert)
    with pytest.raises(RuntimeError):
        await store.create_plan([_data(), _data(), _data()])
    restored = ActionStore(coll, clock=clock)
    assert len(coll.docs) == 1 and coll.docs[0]["state"] == "blocked"
    assert await restored.recover_ready() == []
    assert not await restored.bind(coll.docs[0]["request_id"], 66)
    clock.advance(300)
    changed = await restored.expire_pending()
    assert len(changed) == 1 and changed[0]["state"] == "expired"
    assert "text" not in changed[0]["payload"]


@pytest.mark.asyncio
async def test_publication_has_one_winner_and_source_reply_never_authorizes_card(state):
    store, _, _ = state
    doc = await store.create(_data(action="ban_member"))
    rid = doc["request_id"]
    assert await store.bind_context(rid, 70)
    publications = await asyncio.gather(*(store.claim_publication(rid) for _ in range(10)))
    winners = [doc for doc in publications if doc is not None]
    assert len(winners) == 1 and winners[0]["state"] == "publishing"
    assert winners[0]["publishing_token"]
    assert await store.recover_ready() == []
    assert await store.claim(rid, **_binding(message_id=70)) is None
    assert await store.bind(rid, 66)
    assert await store.claim(rid, **_binding(message_id=70)) is None
    claimed = await store.claim(rid, **_binding())
    assert claimed and claimed["source_reply_message_id"] == 70


@pytest.mark.asyncio
async def test_restart_of_uncertain_publication_never_sends_new_card_or_advances_chain(state):
    store, coll, clock = state
    docs = await store.create_plan([_data(action="ban_member"), _data(ask_permission=False)])
    rid = docs[0]["request_id"]
    assert await store.claim_publication(rid)
    clock.advance(300)
    restored = ActionStore(coll, clock=clock)
    changed = await restored.recover_stale_publishing()
    assert [doc["state"] for doc in changed] == ["uncertain", "cancelled"]
    assert await restored.claim_publication(rid) is None
    assert not await restored.bind(rid, 66)
    assert await restored.recover_ready() == []
    assert all("text" not in doc["payload"] for doc in changed)


@pytest.mark.asyncio
async def test_legacy_audio_approvals_are_cancelled_and_never_converted_to_auto(state):
    store, _, _ = state
    old_audio = await _pending(store)
    old_speech = await _pending(store, action="speak_voice")
    moderation = await _pending(store, action="ban_member")
    automatic = await store.create(_data(ask_permission=False))
    assert await store.arm_automatic(automatic["request_id"])
    docs = await store.create_plan([_data(), _data(action="ban_member")])
    changed = await store.cancel_legacy_audio_approvals()
    assert {doc["request_id"] for doc in changed} == {
        old_audio, old_speech, docs[0]["request_id"], docs[1]["request_id"],
    }
    assert all(doc["state"] == "cancelled" and "text" not in doc["payload"] for doc in changed)
    assert [doc["request_id"] for doc in await store.pending()] == [moderation]
    assert [doc["request_id"] for doc in await store.recover_ready()] == [automatic["request_id"]]
    assert await store.cancel_legacy_audio_approvals() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_pre_card_failure_immediately_terminalizes_request_and_dependents(state, uncertain):
    store, _, _ = state
    docs = await store.create_plan([_data(action="ban_member"), _data(ask_permission=False)])
    rid = docs[0]["request_id"]
    assert await store.claim_publication(rid)
    assert await store.fail_unpublished(rid, public_result="Não consegui publicar o pedido.",
                                        uncertain=uncertain)
    first = await store.get(rid)
    assert first["state"] == ("uncertain" if uncertain else "failed")
    assert "text" not in first["payload"]
    tail = await store.get(docs[1]["request_id"])
    assert tail["state"] == "cancelled" and "text" not in tail["payload"]
    assert not await store.fail_unpublished(rid, public_result="Não sobrescrever.")
    assert await store.recover_ready() == []


@pytest.mark.asyncio
async def test_pre_card_failure_never_changes_bound_or_executing_request(state):
    store, _, _ = state
    rid = await _pending(store)
    assert not await store.fail_unpublished(rid, public_result="Não sobrescrever.")
    assert (await store.get(rid))["state"] == "pending"
    await store.claim(rid, **_binding())
    assert not await store.fail_unpublished(rid, public_result="Não sobrescrever.")
    assert (await store.get(rid))["state"] == "executing"


@pytest.mark.asyncio
async def test_consumed_card_cleanup_is_persistent_scoped_and_keeps_auth_message_id(state):
    store, coll, clock = state
    pending = await _pending(store, action="ban_member")
    executing = await _pending(store, action="ban_member")
    rejected = await _pending(store, action="ban_member")
    await store.claim(executing, **_binding())
    assert await store.reject(rejected, **_binding())
    automatic = await store.create(_data(ask_permission=False))
    assert await store.arm_automatic(automatic["request_id"])
    await store.claim_automatic(automatic["request_id"], guild_id=11, channel_id=22, actor_id=44)
    # Pedidos legados sem o novo campo também são encontrados para remoção.
    for doc in coll.docs:
        if doc["request_id"] == rejected:
            doc.pop("card_removed")
    restored = ActionStore(coll, clock=clock)
    assert {doc["request_id"] for doc in await restored.cards_to_remove()} == {executing, rejected}
    assert len(await restored.cards_to_remove(limit=1)) == 1
    assert await restored.cards_to_remove(limit=0) == []
    assert not await restored.mark_card_removed(pending)
    assert not await restored.mark_card_removed(automatic["request_id"])
    assert await restored.mark_card_removed(executing)
    assert await restored.mark_card_removed(rejected)
    assert await restored.cards_to_remove() == []
    assert (await restored.get(executing))["message_id"] == 66
    assert await restored.claim(executing, **_binding()) is None


@pytest.mark.asyncio
async def test_pending_for_requester_is_scoped_bounded_and_contains_only_public_summaries(state):
    store, _, clock = state
    expired = await _pending(store, action="ban_member")
    clock.advance(300)
    docs = await store.create_plan(_plan_data())
    first = docs[0]["request_id"]
    assert await store.claim_publication(first)
    other_member = await store.create(_data(requester_id=99))
    other_channel = await store.create(_data(channel_id=99))
    other_guild = await store.create(_data(guild_id=99))
    cancelled = await _pending(store)
    assert await store.reject(cancelled, **_binding())
    summaries = await store.pending_for_requester(11, 22, 44)
    assert [doc["request_id"] for doc in summaries] == [doc["request_id"] for doc in docs]
    assert [doc["state"] for doc in summaries] == ["publishing", "blocked", "blocked", "blocked"]
    assert summaries[0]["target_id"] == 44 and summaries[0]["voice_channel_id"] == 77
    assert all("payload" not in doc and "base_reply" not in doc and "execution_token" not in doc
               and "publishing_token" not in doc and "text" not in doc for doc in summaries)
    assert "Fala privada" not in str(summaries)
    assert len(await store.pending_for_requester(11, 22, 44, limit=2)) == 2
    assert await store.pending_for_requester(11, 22, 44, limit=0) == []
    assert [doc["request_id"] for doc in await store.pending_for_requester(11, 22, 99)] == [other_member["request_id"]]
    assert [doc["request_id"] for doc in await store.pending_for_requester(11, 99, 44)] == [other_channel["request_id"]]
    assert [doc["request_id"] for doc in await store.pending_for_requester(99, 22, 44)] == [other_guild["request_id"]]
    assert expired not in {doc["request_id"] for doc in summaries}


@pytest.mark.asyncio
async def test_public_pending_summary_filters_private_structured_values_and_keeps_fixed_purge_count(state):
    store, _, _ = state
    doc = await store.create(_data(action="purge_messages", payload={
        "text": "PRIVATE_SENTINEL", "reason": {"text": "PRIVATE_SENTINEL"},
        "target_id": True, "role_id": {"text": "PRIVATE_SENTINEL"}, "duration_seconds": 60,
        "channel_id": 88, "message_ids": [101, 101, 102, True, -1, {"text": "PRIVATE_SENTINEL"}],
    }))
    summary, = await store.pending_for_requester(11, 22, 44)
    assert summary["request_id"] == doc["request_id"]
    assert summary["channel_id"] == 22 and summary["affected_channel_id"] == 88
    assert summary["message_count"] == 2 and summary["duration_seconds"] == 60
    assert "target_id" not in summary and "role_id" not in summary
    assert "PRIVATE_SENTINEL" not in str(summary)


@pytest.mark.asyncio
async def test_owner_can_cancel_own_staff_request_without_approving_it_and_purges_chain(state):
    store, _, _ = state
    docs = await store.create_plan([_data(action="ban_member"), _data(ask_permission=False)])
    rid = docs[0]["request_id"]
    assert await store.bind(rid, 66)
    for scope in ({"guild_id": 99}, {"channel_id": 99}, {"requester_id": 99}, {"requester_id": 0}):
        fields = {"guild_id": 11, "channel_id": 22, "requester_id": 44, **scope}
        assert not await store.cancel_own_pending(rid, **fields)
    assert await store.cancel_own_pending(rid, guild_id=11, channel_id=22, requester_id=44)
    assert not await store.cancel_own_pending(rid, guild_id=11, channel_id=22, requester_id=44)
    assert await store.claim(rid, **_binding(actor_id=111)) is None
    for doc in docs:
        updated = await store.get(doc["request_id"])
        assert updated["state"] == "cancelled" and "text" not in updated["payload"]
    assert await store.pending_for_requester(11, 22, 44) == []
    assert [doc["request_id"] for doc in await store.cards_to_remove()] == [rid]


@pytest.mark.asyncio
async def test_cancel_future_step_keeps_executing_predecessor_and_stops_later_steps(state):
    store, _, _ = state
    docs = await store.create_plan(_plan_data())
    assert await store.bind(docs[0]["request_id"], 66)
    first = await store.claim(docs[0]["request_id"], **_binding(actor_id=111))
    assert not await store.cancel_own_pending(first["request_id"], guild_id=11, channel_id=22, requester_id=44)
    assert await store.cancel_own_pending(docs[1]["request_id"], guild_id=11, channel_id=22, requester_id=44)
    assert (await store.get(first["request_id"]))["state"] == "executing"
    assert await store.finish(first["request_id"], first["execution_token"],
                              state="succeeded", public_result="Entrei.")
    assert await store.recover_ready() == []
    for doc in docs[1:]:
        assert (await store.get(doc["request_id"]))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_owner_cancellation_and_staff_approval_have_only_one_winner(state):
    store, _, _ = state
    rid = await _pending(store, action="ban_member")
    cancelled, claimed = await asyncio.gather(
        store.cancel_own_pending(rid, guild_id=11, channel_id=22, requester_id=44),
        store.claim(rid, **_binding(actor_id=111)),
    )
    assert int(cancelled) + int(claimed is not None) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "uncertain", "cancelled"])
async def test_terminal_steps_remove_original_user_text_after_delivery_or_cancellation(state, outcome):
    store, _, _ = state
    created = await store.create(_data(action="ban_member", original_user_text="pergunta privada original"))
    rid = created["request_id"]
    if outcome == "cancelled":
        assert await store.cancel_own_pending(rid, guild_id=11, channel_id=22, requester_id=44)
    else:
        assert await store.bind(rid, 66)
        claimed = await store.claim(rid, **_binding(actor_id=111))
        assert claimed["original_user_text"] == "pergunta privada original"
        assert await store.finish(rid, claimed["execution_token"], state=outcome, public_result="resultado")
    retained = await store.get(rid)
    assert "original_user_text" not in retained and "text" not in retained["payload"]
