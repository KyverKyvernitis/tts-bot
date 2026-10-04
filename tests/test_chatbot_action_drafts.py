"""Rascunhos são dados parciais isolados; nunca autorização nem ação pendente."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pymongo.errors import DuplicateKeyError

from cogs.chatbot.action_drafts import (
    ActionDraftStore, DOC_TYPE_ACTION_DRAFT, DRAFT_TTL_SECONDS,
    DraftConflict, DraftStale, InvalidDraft, missing_fields,
)
from cogs.chatbot.memory import MemoryEpoch


class Collection:
    """Simula as operações atômicas do Mongo, incluindo conflito de chave."""
    def __init__(self):
        self.docs, self.indexes = {}, []
        self.before_write = self.before_delete = self.before_read = None

    @staticmethod
    def match(doc, query):
        for key, value in query.items():
            if isinstance(value, dict):
                if "$exists" in value and (key in doc) != value["$exists"]:
                    return False
                if "$gt" in value and (key not in doc or not doc[key] > value["$gt"]):
                    return False
            elif doc.get(key) != value:
                return False
        return True

    async def create_index(self, keys, **kwargs):
        self.indexes.append((deepcopy(keys), deepcopy(kwargs)))

    async def find_one(self, query):
        if self.before_read:
            callback, self.before_read = self.before_read, None
            await callback()
        doc = self.docs.get(query["_id"])
        return deepcopy(doc) if doc and self.match(doc, query) else None

    async def _write_hook(self):
        if self.before_write:
            callback, self.before_write = self.before_write, None
            await callback()

    async def insert_one(self, doc):
        await self._write_hook()
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("duplicate scoped draft")
        self.docs[doc["_id"]] = deepcopy(doc)

    async def find_one_and_replace(self, query, doc, **kwargs):
        await self._write_hook()
        previous = self.docs.get(query["_id"])
        if previous is None or not self.match(previous, query):
            return None
        self.docs[doc["_id"]] = deepcopy(doc)
        return deepcopy(doc)

    async def find_one_and_update(self, query, update, **kwargs):
        await self._write_hook()
        doc = self.docs.get(query["_id"])
        if doc is None or not self.match(doc, query):
            return None
        doc.update(deepcopy(update.get("$set", {})))
        for key, amount in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + amount
        return deepcopy(doc)

    async def delete_one(self, query):
        if self.before_delete:
            callback, self.before_delete = self.before_delete, None
            await callback()
        doc = self.docs.get(query["_id"])
        exists = doc is not None and self.match(doc, query)
        if exists:
            self.docs.pop(query["_id"])
        return SimpleNamespace(deleted_count=int(exists))


@pytest.fixture
def world():
    epoch = MemoryEpoch(1, 2, 3)
    collection = Collection()
    memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=epoch))
    clock = SimpleNamespace(now=1000.0)
    store = ActionDraftStore(collection, memory=memory, clock=lambda: clock.now)
    return SimpleNamespace(collection=collection, memory=memory, epoch=epoch, clock=clock, store=store,
                           scope=(10, 20, 30, epoch))


@pytest.mark.asyncio
async def test_partial_draft_survives_restart_and_never_creates_approval(world):
    first = await world.store.save(*world.scope, action="timeout_member", target_id=40)
    assert first["missing_fields"] == ["reason", "options.duration_seconds"]
    assert first["revision"] == 1 and first["expires_at"] == world.clock.now + DRAFT_TTL_SECONDS
    restarted = ActionDraftStore(world.collection, memory=world.memory, clock=lambda: world.clock.now)
    assert await restarted.get_current(*world.scope) == first
    second = await restarted.update(*world.scope, reason="flood repetido", options={"duration_seconds": 600},
                                    expected_draft_id=first["draft_id"], expected_revision=first["revision"])
    assert second["draft_id"] == first["draft_id"] and second["revision"] == 2
    assert second["target_id"] == 40 and second["missing_fields"] == []
    assert second["reason"] == "flood repetido" and second["options"] == {"duration_seconds": 600}
    assert len(world.collection.docs) == 1
    persisted = next(iter(world.collection.docs.values()))
    assert persisted["type"] == DOC_TYPE_ACTION_DRAFT
    assert persisted["expires_on"] == datetime.fromtimestamp(second["expires_at"], timezone.utc)
    assert not {"request_id", "state", "approved_by", "ask_permission", "payload"} & set(persisted)


@pytest.mark.asyncio
async def test_nested_partial_updates_preserve_exact_requested_channel_changes(world):
    draft = await world.store.save(*world.scope, action="edit_channel", reason="organizar o canal",
                                  options={"channel_ref": "20", "channel_changes": {"name": "geral"}})
    updated = await world.store.save(*world.scope, options={"channel_changes": {"topic": "conversa"}})
    assert updated["draft_id"] == draft["draft_id"]
    assert updated["options"] == {"channel_ref": "20", "channel_changes": {"name": "geral", "topic": "conversa"}}
    assert updated["missing_fields"] == []


@pytest.mark.asyncio
async def test_changing_action_resets_incompatible_fields_in_same_single_draft(world):
    first = await world.store.save(*world.scope, action="timeout_member", target_id=40,
                                  reason="flood", options={"duration_seconds": 60})
    changed = await world.store.save(*world.scope, action="join_voice", target_id=50)
    assert changed["draft_id"] == first["draft_id"] and changed["revision"] == 2
    assert changed["target_id"] == 50 and changed["reason"] == "" and changed["options"] == {}
    assert changed["missing_fields"] == [] and len(world.collection.docs) == 1


@pytest.mark.asyncio
async def test_changing_member_does_not_inherit_another_members_reason_or_duration(world):
    first = await world.store.save(*world.scope, action="timeout_member", target_id=40,
                                  reason="flood de outro membro", options={"duration_seconds": 60})
    changed = await world.store.save(*world.scope, target_id=50,
                                     expected_draft_id=first["draft_id"], expected_revision=first["revision"])
    assert changed["target_id"] == 50 and changed["reason"] == "" and changed["options"] == {}
    assert changed["missing_fields"] == ["reason", "options.duration_seconds"]
    assert changed["draft_id"] == first["draft_id"] and changed["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("other_scope", [(11, 20, 30), (10, 21, 30), (10, 20, 31)])
async def test_draft_get_and_cancel_cannot_cross_server_channel_or_requester(world, other_scope):
    saved = await world.store.save(*world.scope, action="ban_member", target_id=40)
    assert await world.store.get_current(*other_scope, world.epoch) is None
    assert not await world.store.cancel(*other_scope, world.epoch, expected_draft_id=saved["draft_id"])
    assert await world.store.get_current(*world.scope) == saved


@pytest.mark.asyncio
async def test_expiry_is_enforced_before_mongo_ttl_cleanup_and_draft_does_not_revive(world):
    first = await world.store.save(*world.scope, action="ban_member", target_id=40)
    world.clock.now += DRAFT_TTL_SECONDS
    assert await world.store.get_current(*world.scope) is None
    assert not await world.store.cancel(*world.scope)
    assert len(world.collection.docs) == 1  # O monitor TTL ainda não apagou.
    with pytest.raises(DraftConflict):
        await world.store.save(*world.scope, reason="late", expected_draft_id=first["draft_id"])
    new = await world.store.save(*world.scope, action="join_voice", target_id=40)
    assert new["draft_id"] != first["draft_id"] and new["revision"] == 1
    assert len(world.collection.docs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("new_epoch", [MemoryEpoch(2, 2, 3), MemoryEpoch(1, 3, 3), MemoryEpoch(1, 2, 4)])
async def test_reset_hides_old_draft_and_rejects_late_update(world, new_epoch):
    old = await world.store.save(*world.scope, action="ban_member", target_id=40)
    world.memory.capture_epoch.return_value = new_epoch
    assert await world.store.get_current(10, 20, 30, new_epoch) is None
    with pytest.raises(DraftStale):
        await world.store.save(*world.scope, reason="late")
    new = await world.store.save(10, 20, 30, new_epoch, action="ban_member", target_id=50)
    assert new["draft_id"] != old["draft_id"] and len(world.collection.docs) == 1


@pytest.mark.asyncio
async def test_reset_during_write_cannot_overwrite_new_epoch_draft(world):
    await world.store.save(*world.scope, action="ban_member", target_id=40)
    current = MemoryEpoch(1, 2, 4)
    new_draft = None

    async def reset_and_save():
        nonlocal new_draft
        world.memory.capture_epoch.return_value = current
        new_draft = await world.store.save(10, 20, 30, current, action="ban_member", target_id=50, reason="novo pedido")

    world.collection.before_write = reset_and_save
    with pytest.raises(DraftStale):
        await world.store.save(*world.scope, reason="atrasado")
    assert await world.store.get_current(10, 20, 30, current) == new_draft


@pytest.mark.asyncio
async def test_epoch_changed_just_after_insert_removes_only_its_stale_draft(world):
    async def reset():
        world.memory.capture_epoch.return_value = MemoryEpoch(1, 2, 4)
    world.collection.before_write = reset
    with pytest.raises(DraftStale):
        await world.store.save(*world.scope, action="ban_member", target_id=40)
    assert not world.collection.docs


@pytest.mark.asyncio
async def test_concurrent_updates_use_cas_even_without_explicit_revision(world):
    first = await world.store.save(*world.scope, action="timeout_member", target_id=40)

    async def newer_change():
        await world.store.save(*world.scope, reason="flood confirmado")

    world.collection.before_write = newer_change
    with pytest.raises(DraftConflict):
        await world.store.save(*world.scope, options={"duration_seconds": 60})
    current = await world.store.get_current(*world.scope)
    assert current["revision"] == 2 and current["reason"] == "flood confirmado" and current["options"] == {}
    with pytest.raises(DraftConflict):
        await world.store.cancel(*world.scope, expected_revision=first["revision"])


@pytest.mark.asyncio
async def test_concurrent_creation_keeps_exactly_one_draft(world):
    async def other_creation():
        await world.store.save(*world.scope, action="join_voice", target_id=50)
    world.collection.before_write = other_creation
    with pytest.raises(DraftConflict):
        await world.store.save(*world.scope, action="ban_member", target_id=40)
    assert len(world.collection.docs) == 1
    assert (await world.store.get_current(*world.scope))["action"] == "join_voice"


@pytest.mark.asyncio
async def test_cancel_cas_does_not_delete_concurrent_update(world):
    saved = await world.store.save(*world.scope, action="ban_member", target_id=40)
    async def update_before_delete():
        await world.store.save(*world.scope, reason="motivo informado")
    world.collection.before_delete = update_before_delete
    assert not await world.store.cancel(*world.scope, expected_draft_id=saved["draft_id"], expected_revision=1)
    current = await world.store.get_current(*world.scope)
    assert current["revision"] == 2
    assert await world.store.cancel(*world.scope, expected_draft_id=current["draft_id"], expected_revision=2)
    assert await world.store.get_current(*world.scope) is None


@pytest.mark.asyncio
async def test_draft_consumption_is_once_and_hidden_before_any_action_plan_exists(world):
    draft = await world.store.save(*world.scope, action="ban_member", target_id=40, reason="spam")
    plan_id = str(uuid4())
    assert await world.store.consume(*world.scope, expected_draft_id=draft["draft_id"],
                                     expected_revision=draft["revision"], plan_id=plan_id)
    assert await world.store.get_current(*world.scope) is None
    assert not await world.store.cancel(*world.scope)
    for candidate in (plan_id, str(uuid4())):
        assert not await world.store.consume(*world.scope, expected_draft_id=draft["draft_id"],
                                             expected_revision=draft["revision"], plan_id=candidate)
    stored = next(iter(world.collection.docs.values()))
    assert stored["consumed_plan_id"] == plan_id and stored["consumed_at"] == world.clock.now
    assert stored["revision"] == draft["revision"] + 1
    assert stored["type"] == DOC_TYPE_ACTION_DRAFT and "request_id" not in stored


@pytest.mark.asyncio
async def test_draft_consume_conflict_expiry_and_foreign_scope_never_marks_current_draft(world):
    first = await world.store.save(*world.scope, action="ban_member", target_id=40)
    latest = await world.store.save(*world.scope, reason="spam")
    args = {"expected_draft_id": first["draft_id"], "expected_revision": first["revision"], "plan_id": str(uuid4())}
    assert not await world.store.consume(*world.scope, **args)
    args["expected_revision"] = latest["revision"]
    assert not await world.store.consume(10, 20, 31, world.epoch, **args)
    assert await world.store.get_current(*world.scope) == latest
    world.clock.now = latest["expires_at"]
    assert not await world.store.consume(*world.scope, **args)
    assert "consumed_plan_id" not in next(iter(world.collection.docs.values()))


@pytest.mark.asyncio
async def test_new_explicit_save_after_consumption_uses_new_id_not_old_draft(world):
    old = await world.store.save(*world.scope, action="timeout_member", target_id=40,
                                reason="spam", options={"duration_seconds": 60})
    assert await world.store.consume(*world.scope, expected_draft_id=old["draft_id"],
                                     expected_revision=old["revision"], plan_id=str(uuid4()))
    with pytest.raises(InvalidDraft):
        await world.store.save(*world.scope, options={"duration_seconds": 120})
    with pytest.raises(DraftConflict):
        await world.store.save(*world.scope, action="ban_member", expected_draft_id=old["draft_id"])
    new = await world.store.save(*world.scope, action="ban_member", target_id=50)
    assert new["draft_id"] != old["draft_id"] and new["revision"] == 1
    assert new["reason"] == "" and new["options"] == {}
    assert len(world.collection.docs) == 1
    assert "consumed_plan_id" not in next(iter(world.collection.docs.values()))


@pytest.mark.asyncio
async def test_consumption_wins_against_inflight_update_without_reactivating_old_draft(world):
    old = await world.store.save(*world.scope, action="timeout_member", target_id=40, reason="spam")
    async def consume_before_replace():
        assert await world.store.consume(*world.scope, expected_draft_id=old["draft_id"],
                                         expected_revision=old["revision"], plan_id=str(uuid4()))
    world.collection.before_write = consume_before_replace
    with pytest.raises(DraftConflict):
        await world.store.save(*world.scope, options={"duration_seconds": 60})
    assert await world.store.get_current(*world.scope) is None
    assert "consumed_plan_id" in next(iter(world.collection.docs.values()))


@pytest.mark.asyncio
async def test_reset_between_consume_validation_and_write_cannot_consume_new_draft(world):
    old = await world.store.save(*world.scope, action="ban_member", target_id=40)
    next_epoch = MemoryEpoch(1, 2, 4)
    async def replace_after_reset():
        world.memory.capture_epoch.return_value = next_epoch
        await world.store.save(10, 20, 30, next_epoch, action="ban_member", target_id=50)
    world.collection.before_write = replace_after_reset
    with pytest.raises(DraftStale):
        await world.store.consume(*world.scope, expected_draft_id=old["draft_id"],
                                  expected_revision=old["revision"], plan_id=str(uuid4()))
    current = await world.store.get_current(10, 20, 30, next_epoch)
    assert current["target_id"] == 50 and current["draft_id"] != old["draft_id"]
    assert "consumed_plan_id" not in next(iter(world.collection.docs.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), float("-inf")])
async def test_consume_rejects_nonfinite_stored_expiration(world, expires_at):
    draft = await world.store.save(*world.scope, action="ban_member", target_id=40)
    doc = next(iter(world.collection.docs.values()))
    doc["expires_at"] = expires_at
    assert not await world.store.consume(*world.scope, expected_draft_id=draft["draft_id"],
                                         expected_revision=draft["revision"], plan_id=str(uuid4()))
    assert "consumed_plan_id" not in doc


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"action": "delete_server"}, {"action": "ban_member", "target_id": "m1"},
    {"action": "ban_member", "target_id": True}, {"action": "ban_member", "target_id": 0},
    {"action": "ban_member", "reason": "x" * 501},
    {"action": "edit_channel", "reason": "x" * 151},
    {"action": "ban_member", "text": "fala indevida"},
    {"action": "send_audio", "text": "x" * 801}, {"action": "send_audio", "text": "\ud800"},
    {"action": "timeout_member", "options": {"duration_seconds": True}},
    {"action": "timeout_member", "options": {"duration_seconds": 2419201}},
    {"action": "assign_role", "options": {"role_ref": "r1"}},
    {"action": "assign_role", "options": {"role_ref": "<@&123>"}},
    {"action": "edit_channel", "options": {"channel_ref": "c1"}},
    {"action": "purge_messages", "options": {"message_refs": ["msg1"]}},
    {"action": "edit_channel", "options": {"channel_changes": {"delete": True}}},
    {"action": "ban_member", "options": {"duration_seconds": 60}},
    {"action": "change_nickname", "options": {"nickname": "nome\nruim"}},
])
async def test_invalid_or_unresolved_fields_are_rejected_without_storing(world, arguments):
    with pytest.raises(InvalidDraft):
        await world.store.save(*world.scope, **arguments)
    assert not world.collection.docs


@pytest.mark.parametrize("action,expected", [
    ("join_voice", ("target_id",)), ("move_voice", ("target_id",)), ("leave_voice", ()),
    ("ban_member", ("target_id", "reason")),
    ("timeout_member", ("target_id", "reason", "options.duration_seconds")),
    ("assign_role", ("target_id", "reason", "options.role_ref")),
    ("change_nickname", ("target_id", "reason", "options.nickname")),
    ("edit_channel", ("reason", "options.channel_ref", "options.channel_changes")),
    ("purge_messages", ("reason", "options.message_refs")), ("send_audio", ("text",)),
])
def test_missing_fields_are_typed_and_never_create_fake_reason(action, expected):
    assert missing_fields(action) == expected
    assert missing_fields("timeout_member", 40, reason="motivo", options={"duration_seconds": True}) == ("options.duration_seconds",)
    assert missing_fields("change_nickname", 40, reason="motivo", options={"nickname": ""}) == ()


@pytest.mark.asyncio
async def test_unique_scope_and_real_datetime_ttl_indexes(world):
    await world.store.initialize()
    unique, ttl = world.collection.indexes
    assert unique[0] == [("type", 1), ("guild_id", 1), ("channel_id", 1), ("user_id", 1)]
    assert unique[1]["unique"] and unique[1]["partialFilterExpression"] == {"type": DOC_TYPE_ACTION_DRAFT}
    assert ttl[0] == [("expires_on", 1)] and ttl[1]["expireAfterSeconds"] == 0


@pytest.mark.asyncio
async def test_corrupt_expiration_or_unresolved_persisted_resource_is_not_restored(world):
    saved = await world.store.save(*world.scope, action="assign_role", target_id=40, options={"role_ref": "123"})
    doc = next(iter(world.collection.docs.values()))
    doc["expires_at"] = float("nan")
    assert await world.store.get_current(*world.scope) is None
    doc["expires_at"] = saved["expires_at"]
    doc["options"]["role_ref"] = "r1"
    assert await world.store.get_current(*world.scope) is None
