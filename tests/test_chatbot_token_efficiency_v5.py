from copy import deepcopy

from cogs.chatbot.context_efficiency import TurnUsage, compact_closing_state
from cogs.chatbot.tool_registry import ToolRegistry, ToolSpec
from cogs.chatbot.tool_selection import DISCOVERY_TOOL, ToolSelection


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def _registry():
    registry = ToolRegistry()
    registry.register(ToolSpec(
        DISCOVERY_TOOL,
        "Carregue contratos ainda não presentes.",
        {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
         "required": ["names"], "additionalProperties": False},
    ))
    registry.register(ToolSpec("select_response_format", "Escolha texto ou áudio.", EMPTY))
    registry.register(ToolSpec("preparar_resposta", "Prepare a fala final.", EMPTY))
    for index in range(12):
        registry.register(ToolSpec(
            f"consultar_catalogo_{index}",
            ("Consulte espectroscopia nebulosa " if index < 8 else "Consulte jardinagem orquídeas ")
            + ("detalhes extensos " * 12),
            EMPTY,
        ))
    return registry


def test_runtime_capability_index_uses_only_omitted_relevant_hints():
    registry = _registry()
    selection = ToolSelection(registry, "preciso consultar espectroscopia nebulosa", max_initial=4, max_chars=1200)
    hints = selection.index_hints()
    assert hints
    runtime = registry.capability_index(exclude_names=selection.selected_names, detail_names=hints)
    legacy = registry.capability_index(exclude_names=selection.selected_names)
    assert len(runtime) < len(legacy)
    assert "enum names contém o catálogo" in runtime
    assert all(name in runtime for name in hints)
    # Ferramentas irrelevantes continuam descobríveis pelo enum de discovery,
    # sem repetir sua descrição no system prompt.
    assert "consultar_catalogo_11" not in runtime


def test_smalltalk_index_does_not_repeat_full_catalog_descriptions():
    registry = _registry()
    selection = ToolSelection(registry, "oi")
    assert selection.index_hints() == ()
    runtime = registry.capability_index(exclude_names=selection.selected_names, detail_names=selection.index_hints())
    assert len(runtime) < 260
    assert "consultar_catalogo_0" not in runtime


def test_closing_state_keeps_only_format_language_and_does_not_mutate_source():
    state = {
        "bot_name": "Osaka",
        "voice_state": {"bot": {"connected": True}, "author": {"channel_id": "123"}},
        "references": {"members": {"m1": {"name": "Pessoa", "voice": {"channel_id": "123"}}}},
        "action_draft": {"action": "timeout_member", "target_ref": "m1"},
        "preferences": {"mode": "audio", "effective_mode": "audio", "voice": "pt-BR-X", "language": "pt-BR"},
        "tools": {"unavailable": {"x": "fora"}},
    }
    original = deepcopy(state)
    reduced = compact_closing_state(state)
    assert state == original
    assert reduced == {"preferences": {"effective_mode": "audio", "mode": "audio", "language": "pt-BR"}}


def test_local_savings_are_character_counts_not_fake_token_estimates():
    usage = TurnUsage()
    usage.record_local_saving("tool_preface_history", 900)
    usage.record_local_saving("tool_preface_history", 100)
    usage.record_local_saving("closing_state", 450)
    usage.record_local_saving("ignored", 0)
    result = usage.result()
    assert result["local_savings_chars"] == {
        "tool_preface_history": 1000,
        "closing_state": 450,
    }
    assert "estimated_tokens_saved" not in result
