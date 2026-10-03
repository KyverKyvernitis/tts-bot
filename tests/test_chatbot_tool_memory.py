"""Lembretes explícitos não atravessam autor, canal, privacidade ou reset."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cogs.chatbot.memory import MemoryEpoch
from cogs.chatbot.tool_memory import FactStore, MAX_FACTS
from tests.test_chatbot_action_flow import _Cursor, _matches


class Collection:
    def __init__(self):
        self.docs = []

    def find(self, query):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def find_one(self, query):
        return next((deepcopy(doc) for doc in self.docs if _matches(doc, query)), None)

    async def insert_one(self, doc):
        self.docs.append(deepcopy(doc))

    async def delete_one(self, query):
        doc = next((doc for doc in self.docs if _matches(doc, query)), None)
        if doc is not None:
            self.docs.remove(doc)
        return SimpleNamespace(deleted_count=int(doc is not None))


@pytest.mark.asyncio
async def test_facts_persist_deduplicate_and_query_without_exposing_internal_scope():
    collection = Collection()
    store = FactStore(collection)
    scope = (10, 30, 1, "channel:30", MemoryEpoch(2, 3, 4))
    fact = await store.remember(*scope, content="Meu gato chama Pipoca")
    assert await store.remember(*scope, content="  Meu gato chama Pipoca  ") == fact
    assert len(collection.docs) == 1
    assert await FactStore(collection).list(*scope, query="PIPOCA") == [fact]
    assert await store.list(*scope, query="cachorro") == []
    assert set(fact) == {"ref", "content"}


@pytest.mark.asyncio
@pytest.mark.parametrize("other_scope", [
    (11, 30, 1, "channel:30", MemoryEpoch(2, 3, 4)),
    (10, 31, 1, "channel:30", MemoryEpoch(2, 3, 4)),
    (10, 30, 2, "channel:30", MemoryEpoch(2, 3, 4)),
    (10, 30, 1, "private:30:1", MemoryEpoch(2, 3, 4)),
    (10, 30, 1, "channel:30", MemoryEpoch(3, 3, 4)),
    (10, 30, 1, "channel:30", MemoryEpoch(2, 4, 4)),
    (10, 30, 1, "channel:30", MemoryEpoch(2, 3, 5)),
])
async def test_lookup_and_forget_cannot_cross_scope_or_epoch(other_scope):
    store = FactStore(Collection())
    scope = (10, 30, 1, "channel:30", MemoryEpoch(2, 3, 4))
    fact = await store.remember(*scope, content="lembrete pessoal")
    assert await store.list(*other_scope) == []
    assert not await store.forget(*other_scope, ref=fact["ref"])
    assert await store.list(*scope) == [fact]
    assert await store.forget(*scope, ref=fact["ref"])
    assert await store.list(*scope) == []


@pytest.mark.asyncio
async def test_fact_limit_preserves_existing_data_and_allows_new_fact_after_forget():
    store = FactStore(Collection())
    scope = (10, 30, 1, "channel:30", MemoryEpoch(2, 3, 4))
    facts = [await store.remember(*scope, content=f"lembrete {i}") for i in range(MAX_FACTS)]
    with pytest.raises(ValueError):
        await store.remember(*scope, content="excedente")
    assert len(store._coll.docs) == MAX_FACTS
    await store.forget(*scope, ref=facts[0]["ref"])
    await store.remember(*scope, content="novo")
    assert len(store._coll.docs) == MAX_FACTS


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [" ", "x" * 501, "texto\x00inválido"])
async def test_invalid_fact_content_is_rejected_before_any_write(content):
    store = FactStore(Collection())
    with pytest.raises(ValueError):
        await store.remember(10, 30, 1, "channel:30", MemoryEpoch(2, 3, 4), content=content)
    assert not store._coll.docs
