"""Preferências estruturadas persistem sem vazar escopo nem reviver após reset."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.chatbot.memory import MemoryEpoch
from cogs.chatbot.preferences import ConversationPreferences, PreferenceStale, PreferenceStore


class Collection:
    def __init__(self):
        self.docs = {}
        self.before_write = None

    async def find_one(self, query):
        doc = self.docs.get(query["_id"])
        return deepcopy(doc) if doc and all(doc.get(key) == value for key, value in query.items()) else None

    async def update_one(self, query, update, *, upsert):
        if self.before_write is not None:
            callback, self.before_write = self.before_write, None
            await callback()
        doc = self.docs.setdefault(query["_id"], deepcopy(update.get("$setOnInsert", {})))
        doc.update(deepcopy(update.get("$set", {})))


@pytest.fixture
def world():
    collection = Collection()
    memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=MemoryEpoch(1, 2, 3)))
    return SimpleNamespace(collection=collection, memory=memory,
                           store=PreferenceStore(collection, memory=memory), epoch=MemoryEpoch(1, 2, 3))


@pytest.mark.asyncio
async def test_preferences_survive_restart_and_partial_updates_preserve_other_fields(world):
    value = await world.store.set_current(10, 20, 30, world.epoch,
                                         mode="audio", voice="pt-BR-FranciscaNeural", language="pt-BR")
    assert value == ConversationPreferences("audio", "pt-BR-FranciscaNeural", "pt-br")
    restarted = PreferenceStore(world.collection, memory=world.memory)
    assert await restarted.get_current(10, 20, 30, world.epoch) == value
    changed = await restarted.set_current(10, 20, 30, world.epoch, mode="text")
    assert changed == ConversationPreferences("text", value.voice, value.language)
    reset = await restarted.set_current(10, 20, 30, world.epoch, mode="auto", voice="", language="")
    assert reset == ConversationPreferences()


@pytest.mark.asyncio
async def test_preferences_are_isolated_by_guild_channel_and_member(world):
    await world.store.set_current(10, 20, 30, world.epoch, mode="audio")
    for scope in ((11, 20, 30), (10, 21, 30), (10, 20, 31)):
        assert await world.store.get_current(*scope, world.epoch) == ConversationPreferences()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_epoch", [MemoryEpoch(2, 2, 3), MemoryEpoch(1, 3, 3), MemoryEpoch(1, 2, 4)])
async def test_personal_guild_and_global_resets_invalidate_preferences(world, new_epoch):
    await world.store.set_current(10, 20, 30, world.epoch, mode="audio")
    world.memory.capture_epoch.return_value = new_epoch
    assert await world.store.get_current(10, 20, 30, new_epoch) == ConversationPreferences()
    with pytest.raises(PreferenceStale):
        await world.store.set_current(10, 20, 30, world.epoch, mode="text")


@pytest.mark.asyncio
async def test_late_write_cannot_overwrite_preferences_created_after_reset(world):
    current = MemoryEpoch(1, 2, 4)
    async def reset_and_choose_text():
        world.memory.capture_epoch.return_value = current
        await world.store.set_current(10, 20, 30, current, mode="text")
    world.collection.before_write = reset_and_choose_text
    with pytest.raises(PreferenceStale):
        await world.store.set_current(10, 20, 30, world.epoch, mode="audio")
    assert await world.store.get_current(10, 20, 30, current) == ConversationPreferences(mode="text")


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"mode": "other"}, {"mode": []},
    {"voice": "voice with spaces"}, {"voice": "x" * 101}, {"language": "português"},
    {"language": "../env"}, {"voice": False}])
async def test_malformed_structured_fields_are_rejected_without_write(world, arguments):
    with pytest.raises(ValueError):
        await world.store.set_current(10, 20, 30, world.epoch, **arguments)
    assert not world.collection.docs


@pytest.mark.asyncio
async def test_invalid_persisted_values_do_not_become_operational_instructions(world):
    identity = world.store._identity(10, 20, 30, world.epoch)
    world.collection.docs[identity["_id"]] = {**identity, "mode": ["audio"],
                                            "voice": {"fake": True}, "language": "ignore rules"}
    assert await world.store.get_current(10, 20, 30, world.epoch) == ConversationPreferences()
