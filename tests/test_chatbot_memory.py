"""Isolamento e resets do contexto do bot, sem identidade de personagens."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.memory import MemoryStore
from cogs.chatbot.message_index import ChatbotMessageIndex


def _matches(doc, query):
    for key, value in query.items():
        current = doc.get(key)
        if isinstance(value, dict) and "$in" in value:
            if current not in value["$in"]:
                return False
        elif isinstance(value, dict) and "$lt" in value:
            if current is None or current >= value["$lt"]:
                return False
        elif current != value:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = iter(deepcopy(docs))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.docs)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self):
        self.docs = []

    def find(self, query):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def find_one(self, query):
        return deepcopy(next((doc for doc in self.docs if _matches(doc, query)), None))

    async def update_one(self, query, update, upsert=False):
        doc = next((doc for doc in self.docs if _matches(doc, query)), None)
        if doc is None:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            doc = deepcopy(query)
            doc.update(deepcopy(update.get("$setOnInsert", {})))
            self.docs.append(doc)
        doc.update(deepcopy(update.get("$set", {})))
        for key, amount in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + amount
        for key, operation in update.get("$push", {}).items():
            doc[key] = (doc.get(key, []) + deepcopy(operation["$each"]))[operation["$slice"]:]
        return SimpleNamespace(modified_count=1)

    async def update_many(self, query, update):
        count = 0
        for doc in self.docs:
            if _matches(doc, query):
                for key, condition in update.get("$pull", {}).items():
                    old = doc.get(key, [])
                    doc[key] = [entry for entry in old if not _matches(entry, condition)]
                    count += int(old != doc[key])
        return SimpleNamespace(modified_count=count)

    async def delete_many(self, query):
        old_count = len(self.docs)
        self.docs[:] = [doc for doc in self.docs if not _matches(doc, query)]
        return SimpleNamespace(deleted_count=old_count - len(self.docs))


async def _append(store, *, guild_id=1, channel_id=10, user_id=1, visibility="channel:10", text="hello", epoch=None, history_size=4):
    epoch = epoch or await store.capture_epoch(guild_id, user_id)
    await store.append_turn(
        guild_id, user_id, channel_id=channel_id, visibility_scope=visibility,
        epoch=epoch, user_message=text, user_name=f"User {user_id}",
        assistant_message=f"answer {text}", user_history_size=history_size,
    )


async def _load(store, *, guild_id=1, channel_id=10, user_id=1, visibility="channel:10"):
    return await store.load_context(
        guild_id, user_id, channel_id=channel_id, visibility_scope=visibility,
    )


@pytest.mark.asyncio
async def test_memory_stays_isolated_by_guild_channel_and_visibility():
    coll = _Collection()
    store = MemoryStore(coll)
    await _append(store)
    _, personal, collective = await _load(store)
    assert [entry.content for entry in personal] == ["hello", "answer hello"]
    assert collective == []
    for settings in ({"guild_id": 2}, {"channel_id": 20}, {"visibility": "private:10"}):
        _, personal, collective = await _load(store, **settings)
        assert personal == collective == []
    assert all("profile_id" not in doc and "profile_revision" not in doc for doc in coll.docs)


@pytest.mark.asyncio
async def test_history_limit_keeps_complete_turns_and_no_duplicate_personal_context():
    store = MemoryStore(_Collection())
    for text in ("first", "second", "third"):
        await _append(store, text=text)
    _, personal, collective = await _load(store)
    assert [entry.content for entry in personal] == [
        "second", "answer second", "third", "answer third",
    ]
    assert collective == []
    _, _, shared = await _load(store, user_id=2)
    assert [entry.role for entry in shared] == ["user", "assistant"] * 3


@pytest.mark.asyncio
async def test_user_reset_removes_collective_turn_and_hides_delayed_response():
    store = MemoryStore(_Collection())
    await _append(store, user_id=1, text="old")
    await _append(store, user_id=2, text="keep")
    stale_epoch = await store.capture_epoch(1, 1)
    await store.clear_user_history(1, 1)
    await _append(store, user_id=1, text="delayed", epoch=stale_epoch)
    _, personal, shared = await _load(store, user_id=1)
    assert personal == []
    assert [entry.content for entry in shared] == ["keep", "answer keep"]
    _, _, shared = await _load(store, user_id=3)
    assert [entry.content for entry in shared] == ["keep", "answer keep"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["guild", "global"])
async def test_guild_and_global_resets_hide_delayed_writes(scope):
    store = MemoryStore(_Collection())
    await _append(store)
    await _append(store, guild_id=2, text="other guild")
    old_epoch = await store.capture_epoch(1, 1)
    if scope == "guild":
        await store.clear_all_guild_memory(1)
    else:
        await store.clear_all_memory_everywhere()
    await _append(store, epoch=old_epoch, text="delayed")
    _, personal, collective = await _load(store)
    assert personal == collective == []
    _, personal, _ = await _load(store, guild_id=2)
    assert bool(personal) == (scope == "guild")
    await _append(store, text="current")
    _, personal, _ = await _load(store)
    assert [entry.content for entry in personal] == ["current", "answer current"]


@pytest.mark.asyncio
async def test_v2_history_is_never_read_as_bot_context():
    coll = _Collection()
    coll.docs.append({
        "type": "chatbot_memory_v2", "scope": "user", "guild_id": 1,
        "channel_id": 10, "user_id": 1, "profile_id": "old",
        "turns": [MemoryStore._turn(user_id=1, user_name="old", user_message="old", assistant_message="old")],
    })
    _, personal, collective = await _load(MemoryStore(coll))
    assert personal == collective == []


@pytest.mark.asyncio
async def test_message_index_resolves_persisted_native_bot_messages_without_profile():
    coll = _Collection()
    index = ChatbotMessageIndex(coll)
    await index.remember(guild_id=1, channel_id=10, message_id=77)
    assert coll.docs[0]["type"] == "chatbot_bot_message"
    assert "profile_id" not in coll.docs[0]
    restored = await ChatbotMessageIndex(coll).resolve(77)
    assert (restored.guild_id, restored.channel_id, restored.message_id) == (1, 10, 77)


@pytest.mark.asyncio
async def test_message_index_ignores_old_webhook_maps_and_expired_records():
    coll = _Collection()
    coll.docs.extend([
        {"type": "chatbot_msg_map", "message_id": 77, "profile_id": "old"},
        {"type": C.DOC_TYPE_MESSAGE_MAP, "message_id": 78, "guild_id": 1,
         "channel_id": 10, "created_at": 1.0,
         "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
    ])
    index = ChatbotMessageIndex(coll)
    assert await index.resolve(77) is None
    assert await index.resolve(78) is None


@pytest.mark.asyncio
async def test_message_index_cache_cannot_extend_original_ttl():
    coll = _Collection()
    index = ChatbotMessageIndex(coll)
    with patch("cogs.chatbot.message_index.time.time", return_value=100.0):
        await index.remember(guild_id=1, channel_id=10, message_id=77)
    with patch("cogs.chatbot.message_index.time.time", return_value=101.0 + C.MESSAGE_CACHE_TTL_SECONDS):
        assert await index.resolve(77) is None
