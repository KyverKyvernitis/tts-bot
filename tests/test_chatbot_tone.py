"""Persistência das instruções de conversa; não simula respostas de um LLM."""
from __future__ import annotations

import copy
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.master import MasterPrompt, MasterPromptStore


def _master_doc(prompt: str, **changes) -> dict:
    return {
        "_id": "master",
        "type": C.DOC_TYPE_MASTER,
        "schema_version": 3,
        "prompt": prompt,
        "config_guild_id": 12345,
        "updated_at": 100.0,
        "updated_by": 77,
        **changes,
    }


class _MasterCollection:
    def __init__(self, doc=None):
        self.doc = copy.deepcopy(doc)
        self.other_documents = [{"type": C.DOC_TYPE_MEMORY_V3, "content": "conversa antiga"}]
        self.reads = 0
        self.writes = []
        self.write_error = None
        self.before_write = None

    async def find_one(self, query):
        assert query == {"type": C.DOC_TYPE_MASTER}
        self.reads += 1
        return copy.deepcopy(self.doc)

    async def update_one(self, query, update, *, upsert=False):
        self.writes.append((copy.deepcopy(query), copy.deepcopy(update), upsert))
        if self.write_error:
            raise self.write_error
        if self.before_write:
            self.before_write(self)
            self.before_write = None
        matched = self.doc is not None and all(self.doc.get(k) == v for k, v in query.items())
        if matched:
            self.doc.update(update["$set"])
        return SimpleNamespace(matched_count=int(matched))


@pytest.mark.asyncio
@pytest.mark.parametrize("old_prompt", C.LEGACY_DEFAULT_MASTER_PROMPTS)
async def test_old_published_default_upgrade_preserves_ownership_metadata_and_memory(old_prompt):
    old_doc = _master_doc(old_prompt, operator_note="keep me")
    coll = _MasterCollection(old_doc)
    store = MasterPromptStore(coll)

    master = await store.get()

    assert master.prompt == C.DEFAULT_MASTER_PROMPT
    assert master.config_guild_id == 12345
    assert master.updated_by == 77
    assert master.updated_at == 100.0
    assert coll.doc == {**old_doc, "prompt": C.DEFAULT_MASTER_PROMPT}
    assert coll.other_documents == [{"type": C.DOC_TYPE_MEMORY_V3, "content": "conversa antiga"}]
    query, update, upsert = coll.writes[0]
    assert query == {"type": C.DOC_TYPE_MASTER, "prompt": old_prompt}
    assert set(update["$set"]) == {"schema_version", "prompt"}
    assert not upsert


@pytest.mark.asyncio
@pytest.mark.parametrize("old_prompt", C.LEGACY_DEFAULT_MASTER_PROMPTS)
async def test_default_upgrade_is_cached_and_idempotent_after_restart(old_prompt):
    coll = _MasterCollection(_master_doc(old_prompt))
    store = MasterPromptStore(coll)

    first = await store.get()
    assert await store.get() is first
    assert coll.reads == 1
    assert len(coll.writes) == 1

    refreshed = await MasterPromptStore(coll).get()
    assert refreshed.prompt == C.DEFAULT_MASTER_PROMPT
    assert len(coll.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [
    "Use as minhas instruções personalizadas.",
] + [old + "\nMais uma instrução minha." for old in C.LEGACY_DEFAULT_MASTER_PROMPTS]
  + [old + " " for old in C.LEGACY_DEFAULT_MASTER_PROMPTS])
async def test_custom_prompts_are_preserved_even_without_edit_metadata(custom):
    doc = _master_doc(custom, updated_at=0.0, updated_by=0)
    coll = _MasterCollection(doc)

    master = await MasterPromptStore(coll).get()

    assert master.prompt == custom
    assert coll.doc == doc
    assert not coll.writes


@pytest.mark.asyncio
async def test_concurrent_custom_edit_wins_over_default_upgrade():
    coll = _MasterCollection(_master_doc(C.LEGACY_DEFAULT_MASTER_PROMPTS[0]))

    def concurrent_edit(collection):
        collection.doc.update(prompt="Edição feita por outro operador.", config_guild_id=999)

    coll.before_write = concurrent_edit
    master = await MasterPromptStore(coll).get()

    assert master.prompt == "Edição feita por outro operador."
    assert master.config_guild_id == 999
    assert coll.doc["prompt"] == master.prompt
    assert coll.reads == 2


@pytest.mark.asyncio
async def test_failed_upgrade_keeps_existing_prompt_and_retries_after_cache_refresh(caplog):
    old = C.LEGACY_DEFAULT_MASTER_PROMPTS[0]
    coll = _MasterCollection(_master_doc(old))
    coll.write_error = RuntimeError("sensitive database failure details")
    store = MasterPromptStore(coll)

    master = await store.get()
    assert master.prompt == old
    assert coll.doc["prompt"] == old
    assert await store.get() is master
    assert len(coll.writes) == 1
    assert "RuntimeError" in caplog.text
    assert "sensitive database failure details" not in caplog.text

    coll.write_error = None
    store.invalidate_cache()
    assert (await store.get()).prompt == C.DEFAULT_MASTER_PROMPT
    assert len(coll.writes) == 2


@pytest.mark.asyncio
async def test_missing_document_uses_new_default_without_bootstrap_write():
    coll = _MasterCollection()
    master = await MasterPromptStore(coll).get()

    assert master == MasterPrompt.default()
    assert not coll.writes


@pytest.mark.asyncio
async def test_concurrent_removal_does_not_recreate_master_or_touch_memory():
    coll = _MasterCollection(_master_doc(C.LEGACY_DEFAULT_MASTER_PROMPTS[0]))
    coll.before_write = lambda collection: setattr(collection, "doc", None)

    master = await MasterPromptStore(coll).get()

    assert master == MasterPrompt.default()
    assert coll.doc is None
    assert coll.other_documents[0]["content"] == "conversa antiga"


@pytest.mark.asyncio
async def test_unrelated_master_update_does_not_trigger_default_migration():
    coll = AsyncMock()
    custom_doc = _master_doc("Tom personalizado.")
    coll.find_one.return_value = custom_doc

    master = await MasterPromptStore(coll).get()

    assert master.prompt == "Tom personalizado."
    coll.update_one.assert_not_awaited()


def test_groq_text_default_prefers_larger_model_without_changing_vision(monkeypatch):
    monkeypatch.delenv("CHATBOT_GROQ_MODELS", raising=False)
    monkeypatch.setenv("CHATBOT_GROQ_VISION_MODELS", "configured/vision-model")

    settings = runpy.run_path(C.__file__)

    assert settings["GROQ_MODELS"] == ("openai/gpt-oss-120b", "openai/gpt-oss-20b")
    assert settings["GROQ_VISION_MODELS"] == ("configured/vision-model",)


def test_explicit_groq_text_models_keep_operator_order(monkeypatch):
    monkeypatch.setenv("CHATBOT_GROQ_MODELS", " custom/first ,custom/second ")

    settings = runpy.run_path(C.__file__)

    assert settings["GROQ_MODELS"] == ("custom/first", "custom/second")
