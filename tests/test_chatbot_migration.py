"""Migração com falhas de escrita/leitura e armazenamento Mongo em memória."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.db import ensure_indexes
from cogs.chatbot.migrations import (
    LEGACY_COLLECTION_NAME, MIGRATION_ID, run_migrations,
)


def _matches(doc, query):
    for key, value in query.items():
        current = doc.get(key)
        if isinstance(value, dict) and "$in" in value:
            if current not in value["$in"]:
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


class FakeCollection:
    def __init__(self, database, name):
        self.database = database
        self.name = name
        self.docs = []
        self.indexes = {"chatbot_profile_unique": {}, "chatbot_msg_map_ttl": {}}
        self.dropped_indexes = []
        self.fail_delete_once = False
        self.corrupt_read = False
        self.fail_index = False

    def find(self, query):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def find_one(self, query):
        for doc in self.docs:
            if _matches(doc, query):
                found = deepcopy(doc)
                if self.corrupt_read:
                    found["corrupted"] = True
                return found
        return None

    async def update_one(self, query, update, upsert=False):
        doc = next((doc for doc in self.docs if _matches(doc, query)), None)
        inserted = doc is None and upsert
        if inserted:
            doc = deepcopy(query)
            doc.setdefault("_id", f"{self.name}-{len(self.docs)}")
            doc.update(deepcopy(update.get("$setOnInsert", {})))
            self.docs.append(doc)
        if doc is None:
            return SimpleNamespace(modified_count=0, upserted_id=None)
        old = deepcopy(doc)
        doc.update(deepcopy(update.get("$set", {})))
        for key in update.get("$unset", {}):
            doc.pop(key, None)
        return SimpleNamespace(
            modified_count=int(doc != old), upserted_id=doc["_id"] if inserted else None,
        )

    async def delete_one(self, query):
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise OSError("simulated interruption")
        for i, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs.pop(i)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def count_documents(self, query):
        return sum(_matches(doc, query) for doc in self.docs)

    async def create_index(self, keys, **options):
        if self.fail_index:
            raise ValueError("database error with a private document")
        self.indexes[options["name"]] = {"keys": keys, **options}

    async def index_information(self):
        return deepcopy(self.indexes)

    async def drop_index(self, name):
        self.dropped_indexes.append(name)
        self.indexes.pop(name)


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection(self, name)
        return self.collections[name]


def _collection(docs):
    database = FakeDatabase()
    coll = database[C.CHATBOT_COLLECTION_NAME]
    coll.docs = deepcopy(docs)
    return coll, database[LEGACY_COLLECTION_NAME]


def _marker(coll):
    return next(doc for doc in coll.docs if doc.get("migration_id") == MIGRATION_ID)


@pytest.mark.asyncio
async def test_migration_archives_before_removal_and_preserves_disabled_state():
    originals = [
        {"_id": "profile", "type": "chatbot_profile", "guild_id": 1, "name": "Old"},
        {"_id": "memory", "type": "chatbot_memory_v2", "guild_id": 1, "turns": ["old"]},
        {"_id": "map", "type": "chatbot_msg_map", "message_id": 10},
        {"_id": "webhook", "type": "chatbot_webhook", "channel_id": 2},
        {"_id": "config", "type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 1,
         "enabled": False, "active_profile_id": "profile", "schema_version": 2},
        {"_id": "master", "type": C.DOC_TYPE_MASTER, "prompt": "Old character prompt",
         "config_guild_id": 77},
    ]
    epoch = {"_id": "epoch", "type": C.DOC_TYPE_MEMORY_EPOCH,
             "epoch_key": "global", "generation": 5}
    coll, archive = _collection([*originals, epoch])
    report = await run_migrations(coll)
    assert report.archived_documents == len(originals)
    assert sorted(archive.docs, key=lambda doc: doc["_id"]) == sorted(originals, key=lambda doc: doc["_id"])
    config = await coll.find_one({"_id": "config"})
    assert config["enabled"] is False
    assert "active_profile_id" not in config
    assert config["schema_version"] == 3
    assert config["spontaneous_enabled"] is False
    assert config["channel_ids"] == config["spontaneous_channel_ids"] == []
    assert not await coll.count_documents({"type": C.DOC_TYPE_MEMORY_V3})
    assert await coll.find_one({"_id": "epoch"}) == epoch
    master = await coll.find_one({"_id": "master"})
    assert master["prompt"] == C.DEFAULT_MASTER_PROMPT
    assert master["config_guild_id"] == 77
    assert _marker(coll)["status"] == "complete"
    assert "chatbot_profile_unique" in coll.dropped_indexes


@pytest.mark.asyncio
async def test_interruption_can_resume_without_duplicate_archive_documents():
    original = {"_id": "legacy", "type": "chatbot_memory", "entries": ["old"]}
    coll, archive = _collection([original])
    coll.fail_delete_once = True
    with pytest.raises(OSError):
        await run_migrations(coll)
    assert coll.docs[0] == archive.docs[0] == original
    assert _marker(coll)["status"] == "running"
    assert not coll.dropped_indexes
    await run_migrations(coll)
    assert archive.docs == [original]
    assert await coll.find_one({"_id": "legacy"}) is None
    assert _marker(coll)["status"] == "complete"


@pytest.mark.asyncio
async def test_archive_verification_failure_keeps_live_data_and_indexes():
    original = {"_id": "legacy", "type": "chatbot_profile", "name": "private"}
    coll, archive = _collection([original])
    archive.corrupt_read = True
    with pytest.raises(RuntimeError, match="não foi validada"):
        await run_migrations(coll)
    assert coll.docs[0] == original
    assert _marker(coll)["status"] == "running"
    assert not coll.dropped_indexes


@pytest.mark.asyncio
async def test_repeated_migration_preserves_new_prompt_and_server_settings():
    coll, archive = _collection([
        {"_id": "config", "type": C.DOC_TYPE_GUILD_CONFIG,
         "guild_id": 1, "enabled": True, "active_profile_id": "old"},
        {"_id": "master", "type": C.DOC_TYPE_MASTER,
         "prompt": "old", "config_guild_id": 7},
    ])
    await run_migrations(coll)
    await coll.update_one({"_id": "config"}, {"$set": {
        "channel_ids": [99], "spontaneous_enabled": True,
    }})
    await coll.update_one({"_id": "master"}, {"$set": {"prompt": "new custom instructions"}})
    saved = deepcopy(archive.docs)
    report = await run_migrations(coll)
    assert report.already_applied
    assert archive.docs == saved
    assert (await coll.find_one({"_id": "master"}))["prompt"] == "new custom instructions"
    assert (await coll.find_one({"_id": "config"}))["channel_ids"] == [99]


@pytest.mark.asyncio
async def test_completed_marker_still_archives_newly_reintroduced_legacy_data():
    coll, archive = _collection([])
    await run_migrations(coll)
    coll.docs.append({"_id": "late", "type": "chatbot_extrovert", "enabled": True})
    report = await run_migrations(coll)
    assert not report.already_applied
    assert archive.docs[0]["_id"] == "late"
    assert await coll.find_one({"_id": "late"}) is None


@pytest.mark.asyncio
async def test_new_unique_indexes_and_ttl_use_only_bot_v3_documents():
    coll, _ = _collection([])
    await ensure_indexes(coll)
    index = coll.indexes["chatbot_memory_v3_unique"]
    assert index["unique"]
    assert "profile_id" not in dict(index["keys"])
    assert index["partialFilterExpression"] == {"type": C.DOC_TYPE_MEMORY_V3}
    ttl = coll.indexes["chatbot_bot_message_ttl"]
    assert ttl["expireAfterSeconds"] == 0
    assert ttl["partialFilterExpression"] == {"type": "chatbot_bot_message"}


@pytest.mark.asyncio
async def test_index_failure_blocks_initialization_without_logging_document(caplog):
    coll, _ = _collection([])
    coll.fail_index = True
    with pytest.raises(RuntimeError):
        await ensure_indexes(coll)
    assert "private document" not in caplog.text


@pytest.mark.asyncio
async def test_v1_only_profiles_preserve_enabled_and_disabled_guilds():
    coll, _ = _collection([
        {"_id": "a", "type": "chatbot_profile", "guild_id": 1, "active": False},
        {"_id": "b", "type": "chatbot_profile", "guild_id": 1, "active": True},
        {"_id": "c", "type": "chatbot_profile", "guild_id": 2, "active": False},
    ])
    report = await run_migrations(coll)
    assert report.configs_created == 2
    enabled = await coll.find_one({"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 1})
    disabled = await coll.find_one({"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 2})
    assert enabled["enabled"] is True
    assert disabled["enabled"] is False
    assert enabled["spontaneous_enabled"] is False
    assert enabled["channel_ids"] == []


@pytest.mark.asyncio
async def test_resume_uses_archived_profiles_when_original_was_already_removed():
    coll, archive = _collection([])
    archive.docs.append({"_id": "already-copied", "type": "chatbot_profile", "guild_id": 1, "active": True})
    await run_migrations(coll)
    config = await coll.find_one({"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 1})
    assert config["enabled"] is True
    assert len(archive.docs) == 1
    assert _marker(coll)["legacy_documents_removed"] == 1


@pytest.mark.asyncio
async def test_explicit_disabled_v3_config_wins_over_archived_active_profile():
    config = {
        "_id": "config", "type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": 1,
        "schema_version": 3, "enabled": False, "channel_ids": [99],
        "spontaneous_enabled": True, "spontaneous_channel_ids": [100],
        "spontaneous_chance_percent": 7,
    }
    coll, archive = _collection([config])
    archive.docs.append({"_id": "old", "type": "chatbot_profile", "guild_id": 1, "active": True})
    await run_migrations(coll)
    assert await coll.find_one({"_id": "config"}) == config
