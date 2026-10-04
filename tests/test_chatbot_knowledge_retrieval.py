"""Published knowledge uses local ranking and real Mongo query semantics."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from cogs.chatbot import constants as C
from cogs.chatbot.db import ensure_indexes
from cogs.chatbot.knowledge import KnowledgeStore, KNOWLEDGE_TYPE, MAX_KNOWLEDGE_ENTRIES
from cogs.chatbot.memory import MemoryEpoch, MemoryStore
from tests.test_chatbot_action_flow import _Cursor


def matches(doc, query):
    """Reject unknown operators rather than silently widening privacy queries."""
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(doc, branch) for branch in expected):
                return False
            continue
        if key.startswith("$"):
            raise AssertionError(f"Unsupported Mongo operator: {key}")
        actual = doc.get(key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$in":
                    if actual not in operand:
                        return False
                else:
                    raise AssertionError(f"Unsupported Mongo operator: {operator}")
        elif actual != expected:
            return False
    return True


class Collection:
    """In-memory Mongo with compound unique partial indexes and reset writes."""
    def __init__(self):
        self.docs = []
        self.indexes = {}
        self.queries = []
        self.before_insert = None
        self.before_update = None
        self.after_find = None
        self.yield_on_insert = False

    async def create_index(self, keys, **options):
        self.indexes[options["name"]] = {"keys": keys, **options}

    def find(self, query):
        self.queries.append(deepcopy(query))
        found = _Cursor([doc for doc in self.docs if matches(doc, query)])
        if self.after_find is not None and query.get("type") == KNOWLEDGE_TYPE:
            hook, self.after_find = self.after_find, None

            class RacingCursor:
                def sort(self, *args):
                    found.sort(*args)
                    return self

                def limit(self, *args):
                    found.limit(*args)
                    return self

                async def __aiter__(self):
                    await hook()
                    async for item in found:
                        yield item

            return RacingCursor()
        return found

    async def find_one(self, query):
        self.queries.append(deepcopy(query))
        return next((deepcopy(doc) for doc in self.docs if matches(doc, query)), None)

    async def insert_one(self, doc):
        if self.before_insert is not None:
            hook, self.before_insert = self.before_insert, None
            await hook()
        if self.yield_on_insert:
            await asyncio.sleep(0)
        for spec in self.indexes.values():
            if not spec.get("unique"):
                continue
            partial = spec.get("partialFilterExpression", {})
            if not matches(doc, partial):
                continue
            values = tuple(doc.get(key) for key, _ in spec["keys"])
            if any(matches(stored, partial) and
                   tuple(stored.get(key) for key, _ in spec["keys"]) == values
                   for stored in self.docs):
                raise DuplicateKeyError("duplicate compound identity")
        self.docs.append(deepcopy(doc))
        return SimpleNamespace(inserted_id=doc.get("_id"))

    async def update_one(self, query, update, upsert=False):
        if self.before_update is not None and query.get("type") == KNOWLEDGE_TYPE:
            hook, self.before_update = self.before_update, None
            await hook()
        doc = next((doc for doc in self.docs if matches(doc, query)), None)
        if doc is None:
            if not upsert:
                return SimpleNamespace(matched_count=0, modified_count=0)
            doc = deepcopy(query)
            doc.update(deepcopy(update.get("$setOnInsert", {})))
            self.docs.append(doc)
        doc.update(deepcopy(update.get("$set", {})))
        for key, amount in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + amount
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def update_many(self, query, update):
        modified = 0
        for doc in self.docs:
            if matches(doc, query):
                for key, condition in update.get("$pull", {}).items():
                    old = doc.get(key, [])
                    doc[key] = [item for item in old if not matches(item, condition)]
                    modified += int(old != doc[key])
        return SimpleNamespace(modified_count=modified)

    async def delete_one(self, query):
        doc = next((doc for doc in self.docs if matches(doc, query)), None)
        if doc is not None:
            self.docs.remove(doc)
        return SimpleNamespace(deleted_count=int(doc is not None))

    async def delete_many(self, query):
        old_count = len(self.docs)
        self.docs[:] = [doc for doc in self.docs if not matches(doc, query)]
        return SimpleNamespace(deleted_count=old_count - len(self.docs))


async def setup():
    collection = Collection()
    await ensure_indexes(collection)
    return collection, KnowledgeStore(collection), MemoryStore(collection)


async def publish(store, *, guild=10, channel=30, visibility="channel:30",
                  epoch=MemoryEpoch(), title="Regras da arena", content="A arena abre às 18 horas.",
                  tags="", public=False, ref=""):
    return await store.publish(guild, channel, visibility, epoch, title=title, content=content,
                               tags=tags, guild_public=public, ref=ref)


@pytest.mark.asyncio
async def test_staff_published_entries_persist_and_rank_by_title_tags_and_accents():
    collection, store, _ = await setup()
    generic = await publish(store, title="Calendário", content="Na sexta, a inscrição termina.")
    tag = await publish(store, title="Competição", content="Escolha uma equipe.", tags="inscrição, arena")
    title = await publish(store, title="Inscrição", content="Use /inscrever antes de sexta.")
    entries = await KnowledgeStore(collection).retrieve(10, 30, "channel:30", MemoryEpoch(),
                                                       query="Como faço minha INSCRICAO?")
    assert [entry["ref"] for entry in entries] == [title["ref"], tag["ref"], generic["ref"]]
    assert all(set(entry) == {"ref", "title", "excerpt"} for entry in entries)
    assert all("guild_id" not in json.dumps(entry) for entry in entries)
    assert await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="cachorro") == []


@pytest.mark.asyncio
async def test_retrieval_uses_at_most_three_excerpts_and_bounded_serialized_budget():
    _, store, _ = await setup()
    for i in range(6):
        await publish(store, title=f"Configuração {i} " + "x" * 60,
                      content="introdução " * 90 + "configuração especial " + "explicação " * 70)
    entries = await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="configuração especial",
                                   limit=100, max_chars=100000)
    assert 1 <= len(entries) <= 3
    assert len(json.dumps(entries, ensure_ascii=False)) <= 1200
    assert all(len(entry["excerpt"]) <= 700 for entry in entries)
    assert all("configuração" in entry["excerpt"] for entry in entries)
    small = await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="configuração", max_chars=200)
    assert len(json.dumps(small, ensure_ascii=False)) <= 200
    assert await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="configuração", max_chars=0) == []


@pytest.mark.asyncio
async def test_stop_words_and_empty_queries_do_not_read_or_inject_knowledge():
    collection, store, _ = await setup()
    await publish(store)
    collection.queries.clear()
    for query in ("", "Como você está?", "o a e de para", "the and is"):
        assert await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query=query) == []
    assert collection.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["channel:30", "private:30", "nsfw:30"])
async def test_channel_privacy_is_exact_and_only_explicit_server_public_is_shared(visibility):
    collection, store, _ = await setup()
    refs = {}
    for scope in ("channel:30", "private:30", "nsfw:30"):
        refs[scope] = (await publish(store, visibility=scope, content=f"Arena secreta {scope}"))["ref"]
    shared = await publish(store, content="Arena pública publicada explicitamente.", public=True)
    await publish(store, channel=31, visibility="channel:31", content="Arena de outro canal")
    await publish(store, guild=11, content="Arena de outro servidor", public=True)
    # Old rows can physically arrive after reset, but cannot join the query.
    stale = deepcopy(next(doc for doc in collection.docs if doc.get("knowledge_ref") == shared["ref"]))
    stale.update(knowledge_ref="k-aaaaaaaaaaaa", guild_generation=99, content="Arena antiga")
    collection.docs.append(stale)
    entries = await store.retrieve(10, 30, visibility, MemoryEpoch(), query="arena")
    assert {entry["ref"] for entry in entries} == {refs[visibility], shared["ref"]}
    listed = await store.list(10, 30, visibility, MemoryEpoch())
    assert {entry["ref"] for entry in listed} == {refs[visibility], shared["ref"]}
    assert next(item for item in listed if item["ref"] == shared["ref"])["scope"] == "guild"
    assert next(item for item in listed if item["ref"] == refs[visibility])["scope"] == "channel"
    other = next(ref for scope, ref in refs.items() if scope != visibility)
    assert not await store.forget(10, 30, visibility, MemoryEpoch(), ref=other)
    assert await store.forget(10, 30, visibility, MemoryEpoch(), ref=refs[visibility])
    assert await store.forget(10, 30, visibility, MemoryEpoch(), ref=shared["ref"])


@pytest.mark.asyncio
@pytest.mark.parametrize("guild,channel,visibility", [
    (0, 30, "channel:30"), (10, 0, "channel:0"), (10, 30, "channel:31"),
    (10, 30, "private:30:1"), (10, 30, "guild_public"), (10, 30, "other:30"),
])
async def test_forged_scope_is_rejected_before_database_access(guild, channel, visibility):
    collection, store, _ = await setup()
    for operation in (
        store.list(guild, channel, visibility, MemoryEpoch()),
        store.retrieve(guild, channel, visibility, MemoryEpoch(), query="arena"),
        publish(store, guild=guild, channel=channel, visibility=visibility),
        store.forget(guild, channel, visibility, MemoryEpoch(), ref="k-aaaaaaaaaaaa"),
    ):
        with pytest.raises(ValueError):
            await operation
    assert collection.queries == [] and collection.docs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"title": " "}, {"title": "x" * 81}, {"title": "inválido\x00"},
    {"content": " "}, {"content": "x" * 2001}, {"content": None},
    {"tags": "x" * 161}, {"tags": "x" * 33}, {"tags": ",".join(str(i) for i in range(9))},
    {"public": "true"}, {"ref": "$where"},
])
async def test_invalid_publication_is_rejected_without_writes(arguments):
    collection, store, _ = await setup()
    with pytest.raises(ValueError):
        await publish(store, **arguments)
    assert collection.docs == []


@pytest.mark.asyncio
async def test_updates_preserve_visibility_and_cannot_promote_existing_private_content():
    _, store, _ = await setup()
    local = await publish(store, visibility="private:30", tags="arena, arena, regra")
    updated = await publish(store, visibility="private:30", ref=local["ref"], content="Arena nova.")
    assert updated == local
    assert len(store._coll.docs) == 1
    assert (await store.list(10, 30, "private:30", MemoryEpoch()))[0]["excerpt"] == "Arena nova."
    with pytest.raises(ValueError, match="alcance"):
        await publish(store, visibility="private:30", ref=local["ref"], public=True)
    with pytest.raises(ValueError, match="disponível"):
        await publish(store, visibility="channel:30", ref=local["ref"])


@pytest.mark.asyncio
async def test_limit_counts_all_channel_scopes_and_update_or_delete_frees_no_extra_slot():
    collection, store, _ = await setup()
    saved = []
    for index in range(MAX_KNOWLEDGE_ENTRIES):
        channel = 30 + index % 2
        saved.append(await publish(store, channel=channel, visibility=f"channel:{channel}",
                                   title=f"Arena {index}"))
    assert len([doc for doc in collection.docs if doc["type"] == KNOWLEDGE_TYPE]) == 100
    with pytest.raises(ValueError, match="100 entradas"):
        await publish(store, public=True)
    await publish(store, ref=saved[0]["ref"], content="Arena atualizada")
    assert await store.forget(10, 30, "channel:30", MemoryEpoch(), ref=saved[0]["ref"])
    await publish(store, public=True)
    assert len(collection.docs) == 100
    assert len({doc["entry_slot"] for doc in collection.docs}) == 100


@pytest.mark.asyncio
async def test_unique_slots_keep_cross_process_concurrent_publications_within_limit():
    collection, store, _ = await setup()
    for index in range(99):
        await publish(store, title=f"Arena {index}")
    collection.yield_on_insert = True
    first, second = KnowledgeStore(collection), KnowledgeStore(collection)
    outcomes = await asyncio.gather(publish(first, title="Arena final A"),
                                    publish(second, title="Arena final B"), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert sum(isinstance(result, ValueError) for result in outcomes) == 1
    assert len(collection.docs) == 100
    assert len({doc["entry_slot"] for doc in collection.docs}) == 100


@pytest.mark.asyncio
async def test_user_reset_preserves_published_server_knowledge_and_other_user_epoch():
    _, store, memory = await setup()
    saved = await publish(store, public=True)
    assert await memory.clear_user_history(10, 7) == 0
    epoch = await memory.capture_epoch(10, 7)
    assert epoch.user_generation == 1
    assert (await store.retrieve(10, 30, "channel:30", epoch, query="arena"))[0]["ref"] == saved["ref"]
    assert await store.list(10, 31, "private:31", MemoryEpoch(user_generation=90))


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["guild", "global"])
async def test_guild_and_global_reset_remove_publications_and_reject_stale_reads_writes(reset):
    collection, store, memory = await setup()
    await publish(store, public=True)
    other = await publish(store, guild=11, public=True)
    if reset == "guild":
        assert await memory.clear_all_guild_memory(10) == 1
    else:
        assert await memory.clear_all_memory_everywhere() == 2
    assert await store.list(10, 30, "channel:30", MemoryEpoch()) == []
    assert await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="arena") == []
    with pytest.raises(ValueError, match="memória mudou"):
        await publish(store)
    epoch = await memory.capture_epoch(10, 7)
    await publish(store, epoch=epoch)
    remaining_other = [doc for doc in collection.docs if doc.get("knowledge_ref") == other["ref"]]
    assert bool(remaining_other) == (reset == "guild")


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["guild", "global"])
async def test_reset_during_retrieval_never_returns_old_document_snapshot(reset):
    collection, store, memory = await setup()
    await publish(store)
    collection.after_find = (lambda: memory.clear_all_guild_memory(10)) if reset == "guild" else memory.clear_all_memory_everywhere
    assert await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="arena") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["guild", "global"])
async def test_delayed_insert_after_reset_cannot_restore_old_generation(reset):
    collection, store, memory = await setup()
    collection.before_insert = (lambda: memory.clear_all_guild_memory(10)) if reset == "guild" else memory.clear_all_memory_everywhere
    with pytest.raises(ValueError, match="memória mudou"):
        await publish(store)
    assert not any(doc.get("type") == KNOWLEDGE_TYPE for doc in collection.docs)
    epoch = await memory.capture_epoch(10, 7)
    assert await store.retrieve(10, 30, "channel:30", epoch, query="arena") == []
    await publish(store, epoch=epoch)
    assert await store.retrieve(10, 30, "channel:30", epoch, query="arena")


@pytest.mark.asyncio
async def test_delayed_update_after_reset_does_not_upsert_or_restore_old_content():
    collection, store, memory = await setup()
    saved = await publish(store)
    collection.before_update = lambda: memory.clear_all_guild_memory(10)
    with pytest.raises(ValueError, match="memória mudou"):
        await publish(store, ref=saved["ref"], content="Arena não pode voltar")
    assert not any(doc.get("type") == KNOWLEDGE_TYPE for doc in collection.docs)


@pytest.mark.asyncio
async def test_published_indexes_are_partial_compound_and_do_not_change_other_doc_types():
    collection, _, _ = await setup()
    slot = collection.indexes["chatbot_knowledge_slot_unique"]
    assert slot["unique"]
    assert slot["partialFilterExpression"] == {"type": KNOWLEDGE_TYPE}
    assert [key for key, _ in slot["keys"]] == [
        "type", "guild_id", "global_generation", "guild_generation", "entry_slot",
    ]
    lookup = collection.indexes["chatbot_knowledge_scope_lookup"]
    assert lookup["partialFilterExpression"] == {"type": KNOWLEDGE_TYPE}
    assert {"channel_id", "visibility_scope"} <= {key for key, _ in lookup["keys"]}
    # Missing knowledge slots on conversation data must not enter this index.
    await collection.insert_one({"_id": "other-1", "type": "unrelated", "guild_id": 10})
    await collection.insert_one({"_id": "other-2", "type": "unrelated", "guild_id": 10})


@pytest.mark.asyncio
async def test_unavailable_collection_has_no_retrieval_and_explains_write_unavailability():
    store = KnowledgeStore(None)
    assert await store.list(10, 30, "channel:30", MemoryEpoch()) == []
    assert await store.retrieve(10, 30, "channel:30", MemoryEpoch(), query="arena") == []
    assert not await store.forget(10, 30, "channel:30", MemoryEpoch(), ref="k-aaaaaaaaaaaa")
    with pytest.raises(ValueError, match="não está pronta"):
        await publish(store)
