"""Seleção do catálogo, leitura delimitada e recuperação sem nova chamada de IA."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.chatbot.action_protocol import NativeToolCall
from cogs.chatbot.memory import MemoryEpoch
from cogs.chatbot.tool_memory import FactStore, rank_relevant
from cogs.chatbot.tool_registry import ToolRegistry, ToolSpec
from cogs.chatbot.tool_runtime import auto_retrieve_facts, compact_tool_result, safe_provider_state
from cogs.chatbot.tool_selection import DISCOVERY_TOOL, ToolSelection, text_terms
from tests.test_chatbot_action_flow import world
from tests.test_chatbot_tool_memory import Collection
from tests.test_chatbot_tool_runtime import registry_for, call


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def catalog():
    return ToolRegistry((
        ToolSpec(DISCOVERY_TOOL, "Carregue outros contratos do índice.", EMPTY),
        ToolSpec("select_response_format", "Escolha o formato da resposta.", EMPTY),
        ToolSpec("preparar_resposta", "Prepare uma resposta com ajustes independentes.", EMPTY),
        ToolSpec("medir_nebulosa", "Consulte a nebulosa com espectroscopia astronômica.", EMPTY),
        ToolSpec("cultivar_orquideas", "Consulte o cultivo de orquídeas e irrigação.", EMPTY),
        *(ToolSpec(f"catalogo_{index}", "Informação declarada sobre outros recursos. " * 30, EMPTY)
          for index in range(20)),
    ))


def test_local_selection_uses_catalog_metadata_not_command_names_and_reduces_schema_size():
    registry = catalog()
    selection = ToolSelection(registry, "Qual espectroscopia serve para essa nebulosa?")
    assert "medir_nebulosa" in selection.selected_names
    assert "cultivar_orquideas" not in selection.selected_names
    metrics = selection.metrics()
    assert metrics["catalog_tools"] == 25
    assert metrics["loaded_tools"] <= 5
    assert metrics["loaded_schema_chars"] < metrics["catalog_schema_chars"] / 4
    # O catálogo integral continua ensinando até a função não selecionada.
    assert "cultivar_orquideas" in registry.capability_index()


def test_ordinary_smalltalk_only_loads_control_tools_and_does_not_execute_handlers():
    registry = catalog()
    selection = ToolSelection(registry, "oi")
    assert set(selection.selected_names) == {DISCOVERY_TOOL, "select_response_format", "preparar_resposta"}
    assert selection.load(["medir_nebulosa"])["loaded"] == ["medir_nebulosa"]
    assert "medir_nebulosa" in selection.selected_names


def test_initial_schema_budget_does_not_hide_discovery_or_full_capability_index():
    registry = catalog()
    selection = ToolSelection(registry, "nebulosa orquídeas recursos", max_chars=1, max_initial=1)
    assert set(selection.selected_names) == {DISCOVERY_TOOL, "select_response_format", "preparar_resposta"}
    assert "cultivar_orquideas" in registry.capability_index()
    assert selection.load(["cultivar_orquideas"])["loaded"] == ["cultivar_orquideas"]




def test_capability_index_omits_tools_already_described_by_native_schemas():
    registry = catalog()
    selection = ToolSelection(registry, "Qual espectroscopia serve para essa nebulosa?")
    compact = registry.capability_index(exclude_names=selection.selected_names)
    assert "medir_nebulosa" not in compact
    assert "cultivar_orquideas" in compact
    assert len(compact) < len(registry.capability_index())


def test_followup_prunes_only_speculative_schemas_and_keeps_used_or_explicit_tools():
    registry = catalog()
    selection = ToolSelection(registry, "nebulosa orquídeas")
    speculative = {name for name in selection.selected_names if name not in {DISCOVERY_TOOL, "select_response_format", "preparar_resposta"}}
    assert speculative
    used = sorted(speculative)[0]
    selection.mark_used([used])
    explicit = "catalogo_19"
    selection.load([explicit])
    removed = set(selection.prune_speculative())
    assert used in selection.selected_names and explicit in selection.selected_names
    assert removed == speculative - {used}
    assert removed.isdisjoint(selection.selected_names)

def test_recent_context_can_restore_tools_for_a_short_followup():
    registry = catalog()
    selection = ToolSelection(registry, "e amanhã?", recent_context="A irrigação dessas orquídeas")
    assert "cultivar_orquideas" in selection.selected_names


def test_numeric_ids_and_target_enums_do_not_select_an_unrelated_function():
    registry = catalog()
    registry.register(ToolSpec("dados_alvo", "Consulte informações gerais.", {
        "type": "object", "properties": {"target_ref": {"type": "string", "enum": ["segredomembro", "1555555555"]}},
        "additionalProperties": False,
    }))
    selection = ToolSelection(registry, "segredomembro 1555555555")
    assert "dados_alvo" not in selection.selected_names


def test_stable_index_does_not_evaluate_availability_or_change_with_operational_state():
    enabled = [True]
    registry = catalog()
    registry.register(ToolSpec("efeito_delimitado", "Faça o efeito permitido. Contrato longo adicional.", EMPTY,
                               permission="staff", capabilities=["ban_member", "ban_member"],
                               availability=lambda _context: (enabled[0], "Backend ausente.")))
    before = registry.capability_index()
    enabled[0] = False
    assert registry.capability_index() == before
    assert "ban_member" in before and "Backend ausente." not in before
    selection = ToolSelection(registry)
    state = selection.availability_state()
    assert "efeito_delimitado" not in state.get("unavailable", {})
    assert "efeito_delimitado_actions" not in state


def test_used_schema_stays_in_native_history_but_never_restores_execution_availability():
    enabled = [True]
    registry = catalog()
    spec = ToolSpec("consulta_mutavel", "Consulte o recurso presente.", EMPTY,
                    availability=lambda _context: (enabled[0], "Recurso removido."))
    registry.register(spec)
    selection = ToolSelection(registry)
    selection.load([spec.name])
    selection.mark_used([spec.name])
    enabled[0] = False
    assert spec.name in selection.selected_names
    assert not next(item for item in registry.snapshot() if item.name == spec.name).available
    assert selection.load([spec.name]) == {"loaded": [], "unavailable": {spec.name: "Recurso removido."}}
    assert selection.availability_state()["unavailable"][spec.name] == "Recurso removido."


def test_unavailable_unused_schema_is_excluded_and_legacy_registry_keeps_all_declarations():
    enabled = [True]
    registry = catalog()
    registry.register(ToolSpec("consulta_mutavel", "Consulte um recurso.", EMPTY,
                               availability=lambda _context: enabled[0]))
    selection = ToolSelection(registry)
    selection.load(["consulta_mutavel"])
    enabled[0] = False
    assert "consulta_mutavel" not in selection.selected_names
    legacy = ToolRegistry((ToolSpec("consulta", "Consulta antiga.", EMPTY),))
    assert ToolSelection(legacy, "oi").get_specs() == legacy.get_specs()


@pytest.mark.parametrize("invalid", ["ban_member", ["ação inválida"], [{}], None])
def test_catalog_capabilities_are_explicit_immutable_valid_names(invalid):
    with pytest.raises(ValueError):
        ToolSpec("consulta", "Consulta.", EMPTY, capabilities=invalid)


@pytest.mark.asyncio
async def test_discovery_only_loads_names_and_guarded_host_rejects_a_missing_backend(world):
    registry = await registry_for(world)
    selection = ToolSelection(registry, "oi")
    assert "query_own_memory" not in selection.selected_names
    result = await call(registry, DISCOVERY_TOOL, names=["resolve_member", "query_own_memory"])
    assert result["data"]["loaded"] == ["resolve_member"]
    assert result["data"]["unavailable"]["query_own_memory"]
    assert not world.collection.docs and not world.cog._supervisor.jobs
    assert not (await call(registry, DISCOVERY_TOOL, names=["comando_inventado"]))["ok"]


@pytest.mark.asyncio
async def test_retained_native_schema_does_not_execute_when_service_was_revoked(world):
    registry = await registry_for(world)
    selection = ToolSelection(registry, "call")
    selection.load(["propor_acao"])
    selection.mark_used(["propor_acao"])
    world.cog._actions.ready = False
    assert "propor_acao" in selection.selected_names
    result = await execute_retained(registry)
    assert result["status"] == "unavailable"
    assert not world.collection.docs and not world.cog._supervisor.jobs


async def execute_retained(registry):
    from cogs.chatbot.tool_runtime import execute_native_tool
    return await execute_native_tool(registry, NativeToolCall("real-id", "propor_acao", {"action": "join_voice"}))


def test_local_fact_ranking_tolerates_accents_extra_words_and_rejects_unrelated_terms():
    rows = [{"content": "Minha orquídea usa irrigação por gotejamento"},
            {"content": "O gato se chama Pipoca"}]
    assert rank_relevant(rows, "como funciona a irrigacao mesmo?") == rows[:1]
    assert rank_relevant(rows, "cachorro") == []
    assert text_terms("IRRIGAÇÃO 15555555") == {"irrigacao"}


@pytest.mark.asyncio
@pytest.mark.parametrize("other_scope", [
    (11, 30, 1, "channel:30", MemoryEpoch(2, 3, 4)),
    (10, 31, 1, "channel:30", MemoryEpoch(2, 3, 4)),
    (10, 30, 2, "channel:30", MemoryEpoch(2, 3, 4)),
    (10, 30, 1, "private:30", MemoryEpoch(2, 3, 4)),
    (10, 30, 1, "channel:30", MemoryEpoch(3, 3, 4)),
    (10, 30, 1, "channel:30", MemoryEpoch(2, 4, 4)),
    (10, 30, 1, "channel:30", MemoryEpoch(2, 3, 5)),
])
async def test_automatic_retrieval_cannot_cross_author_channel_privacy_or_reset(other_scope):
    store = FactStore(Collection())
    await store.remember(10, 30, 1, "channel:30", MemoryEpoch(2, 3, 4), content="Minha irrigação secreta")
    assert await store.retrieve(*other_scope, query="irrigação") == []


@pytest.mark.asyncio
async def test_fact_retrieval_budget_counts_json_refs_and_forgetting_is_immediately_visible():
    store = FactStore(Collection())
    scope = (10, 30, 1, "channel:30", MemoryEpoch(2, 3, 4))
    saved = await store.remember(*scope, content="Irrigação " + "x" * 120)
    assert await store.retrieve(*scope, query="irrigação", max_chars=130) == []
    rows = await store.retrieve(*scope, query="irrigação", max_chars=240)
    assert rows == [saved]
    assert len(json.dumps(rows, ensure_ascii=False, separators=(",", ":"))) <= 240
    assert await store.forget(*scope, ref=saved["ref"])
    assert await store.retrieve(*scope, query="irrigação", max_chars=240) == []


@pytest.mark.asyncio
async def test_fact_reset_during_retrieval_discards_result_before_model_receives_it(world):
    world.cog._memory._coll = Collection()
    registry = await registry_for(world)
    async def revoked(*args, **kwargs):
        world.cog._memory.capture_epoch.return_value = MemoryEpoch(2, 3, 5)
        return [{"ref": "safe", "content": "PRIVATE_FACT"}]
    registry.fact_store.retrieve = AsyncMock(side_effect=revoked)
    with pytest.raises(Exception, match="reiniciada"):
        await auto_retrieve_facts(registry, "assunto")
    world.cog._router.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_published_knowledge_search_uses_only_current_trusted_scope_and_bounded_query(world):
    world.cog._knowledge = SimpleNamespace(retrieve=AsyncMock(return_value=[
        {"ref": "entry", "title": "Servidor", "excerpt": "Dados publicados"},
    ]))
    registry = await registry_for(world)
    result = await call(registry, "query_published_knowledge", query="Servidor", limit=2)
    assert result["ok"] and result["data"]["untrusted"] is True
    world.cog._knowledge.retrieve.assert_awaited_once_with(10, 30, "channel:30", world.epoch,
                                                        query="Servidor", limit=2, max_chars=1200)
    assert not (await call(registry, "query_published_knowledge", query="Servidor", limit=4))["ok"]
    assert world.cog._knowledge.retrieve.await_count == 1


@pytest.mark.asyncio
async def test_published_knowledge_reset_during_io_never_returns_previous_data(world):
    async def retrieve(*args, **kwargs):
        world.cog._memory.capture_epoch.return_value = MemoryEpoch(3, 3, 4)
        return [{"excerpt": "PRIVATE_KNOWLEDGE"}]
    world.cog._knowledge = SimpleNamespace(retrieve=AsyncMock(side_effect=retrieve))
    registry = await registry_for(world)
    result = await call(registry, "query_published_knowledge", query="assunto")
    assert result["ok"] is False and "reiniciada" in result["reason"]
    assert "PRIVATE_KNOWLEDGE" not in json.dumps(result)


def test_cloudflare_metadata_is_visible_without_exposing_account_token_or_http_payload():
    router = SimpleNamespace(diagnostics=lambda: {
        "configured": {"groq": True, "gemini": False, "cloudflare": True, "token": "PRIVATE_TOKEN"},
        "availability": [{"provider": "cloudflare", "model": "@cf/qwen/qwen3-30b-a3b-fp8",
                          "available": False, "cause_kind": "rate_limit", "status": 429,
                          "quota_scope": "account", "cooldown_seconds": 300,
                          "account_id": "PRIVATE_ACCOUNT", "body": "PRIVATE_HTTP"},
                         {"provider": "cloudflare", "model": "@wrong/PRIVATE_MODEL"}],
    })
    result = safe_provider_state(router)
    assert result["configured"]["cloudflare"] is True
    assert len(result["availability"]) == 1
    assert result["availability"][0]["model"] == "@cf/qwen/qwen3-30b-a3b-fp8"
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.asyncio
async def test_voice_catalog_default_is_small_and_pagination_remains_explicit(world):
    world.tts.edge_voice_names = {f"pt-BR-Voz{i:02}Neural" for i in range(30)}
    world.tts.gtts_languages = {f"lang{i:02}": f"Idioma {i}" for i in range(30)}
    registry = await registry_for(world)
    first = (await call(registry, "list_tts_voices_languages"))["data"]
    assert len(first["voices"]) == len(first["languages"]) == 10
    assert first["more_voices"] and first["more_languages"]
    second = (await call(registry, "list_tts_voices_languages", offset=10, limit=20))["data"]
    assert len(second["voices"]) == 20 and not second["more_voices"]
    assert not set(first["voices"]) & set(second["voices"])


@pytest.mark.asyncio
async def test_accessible_channels_default_is_small_and_hidden_channels_do_not_appear(world):
    channels = []
    for index in range(12):
        item = SimpleNamespace(id=100 + index, name=f"canal{index}", type="text", guild=world.guild,
                               permissions_for=lambda member: SimpleNamespace(view_channel=True))
        channels.append(item)
    hidden = SimpleNamespace(id=9999, name="PRIVATE_CHANNEL", type="text", guild=world.guild,
                             permissions_for=lambda member: SimpleNamespace(view_channel=False))
    world.guild.channels = [hidden, *channels]
    registry = await registry_for(world)
    first = (await call(registry, "list_accessible_channels"))["data"]
    assert len(first["channels"]) == 6 and first["more"]
    second = (await call(registry, "list_accessible_channels", offset=6))["data"]
    assert len(second["channels"]) == 6 and not second["more"]
    assert "PRIVATE_CHANNEL" not in json.dumps(first) + json.dumps(second)


@pytest.mark.asyncio
async def test_operational_read_keeps_confirmed_action_status_and_full_public_receipt(world):
    receipt = "Resultado confirmado. " + "informação " * 120
    world.cog._actions.store.recent_results = AsyncMock(return_value=[
        {"action": "ban_member", "state": "executed", "public_result": receipt,
         "PRIVATE_INTERNAL": "PRIVATE_CONTENT"},
    ])
    registry = await registry_for(world)
    result = await call(registry, "get_operational_state")
    assert result["data"]["recent_action_results"] == [
        {"action": "ban_member", "state": "executed", "public_result": receipt},
    ]
    assert "PRIVATE_CONTENT" not in json.dumps(result)


@pytest.mark.parametrize("status", ["audio_sent", "image_sent", "reply_sent", "executed", "uncertain"])
def test_oversized_result_keeps_machine_confirmation_and_never_previews_private_json(status):
    result = {"ok": status != "uncertain", "status": status,
              "data": {"message_id": "1555555555555555", "chat_audio_sent": True,
                       "voice_status": "enqueued", "request_id": "confirmed-request",
                       "private_payload": "PRIVATE_AUDIO " * 4000}}
    compact = compact_tool_result(result)
    assert compact["ok"] == result["ok"] and compact["status"] == status
    assert compact["truncated"] is True
    assert compact["data"] == {"message_id": "1555555555555555", "chat_audio_sent": True,
                               "voice_status": "enqueued", "request_id": "confirmed-request"}
    assert "PRIVATE_AUDIO" not in json.dumps(compact) and "preview" not in json.dumps(compact)


def test_oversized_operational_read_preserves_full_terminal_receipt_without_unrelated_data():
    receipt = {"action": "ban_member", "state": "executed", "public_result": "Ação confirmada. " * 1000}
    result = {"ok": True, "status": "available", "data": {
        "recent_action_results": [receipt], "unused_details": "PRIVATE_JSON " * 4000,
    }}
    compact = compact_tool_result(result)
    assert compact["data"]["recent_action_results"] == [receipt]
    assert compact["status"] == "available" and compact["ok"]
    assert "PRIVATE_JSON" not in json.dumps(compact)


@pytest.mark.asyncio
async def test_runtime_oversized_confirmed_delivery_does_not_lose_status_or_request_a_replay(world):
    registry = await registry_for(world)
    delivery = {"ok": True, "status": "audio_sent", "data": {
        "message_id": "88", "chat_audio_sent": True, "voice_status": "enqueued",
        "private_debug": "PRIVATE_SPEECH " * 3000,
    }}
    registry.register(ToolSpec("confirm_delivery", "Retorne uma confirmação existente.", EMPTY,
                               permission="automatic_effect", handler=lambda _args: delivery))
    result = await call(registry, "confirm_delivery")
    assert result["ok"] and result["status"] == "audio_sent"
    assert result["data"]["message_id"] == "88" and result["data"]["voice_status"] == "enqueued"
    assert result["truncated"] and "PRIVATE_SPEECH" not in json.dumps(result)
