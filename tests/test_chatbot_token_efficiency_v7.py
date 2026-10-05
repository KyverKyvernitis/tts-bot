import json

from cogs.chatbot.context_efficiency import compact_tool_evidence
from cogs.chatbot.tool_registry import ToolRegistry, ToolSpec
from cogs.chatbot.tool_selection import DISCOVERY_TOOL, ToolSelection, declaration_chars


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def registry():
    result = ToolRegistry()
    result.register(ToolSpec(
        DISCOVERY_TOOL,
        "Carregue contratos ainda não presentes.",
        {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
         "required": ["names"], "additionalProperties": False},
    ))
    result.register(ToolSpec("select_response_format", "Escolha texto, áudio ou voz.", EMPTY))
    result.register(ToolSpec(
        "preparar_resposta",
        "Prepare a resposta final depois de operações independentes.",
        {"type": "object", "properties": {"text": {"type": "string", "maxLength": 2000}},
         "required": ["text"], "additionalProperties": False},
    ))
    result.register(ToolSpec("consultar_musica", "Consulte estado da música atual.", EMPTY))
    return result


def test_prepare_response_is_discoverable_but_not_paid_in_every_initial_schema():
    catalog = registry()
    selection = ToolSelection(catalog, "oi")
    assert set(selection.selected_names) == {DISCOVERY_TOOL}
    initial = sum(declaration_chars(spec) for spec in selection.get_specs())
    with_prepare = initial + declaration_chars(catalog.get("preparar_resposta"))
    assert initial < with_prepare
    assert selection.load(["preparar_resposta"])["loaded"] == ["preparar_resposta"]
    assert "preparar_resposta" in selection.selected_names


def test_tool_evidence_uses_short_triplets_and_each_batch_is_independent():
    first_records = [{
        "tool": "consulta",
        "args": {"query": "x"},
        "result": {"ok": True, "status": "found", "data": {"value": 42}},
    }]
    second_records = [{
        "tool": "outra",
        "args": {},
        "result": {"ok": True, "status": "done"},
    }]
    first = compact_tool_evidence(first_records)
    second = compact_tool_evidence(second_records)
    first_payload = json.loads(first.split("\n", 2)[1])
    second_payload = json.loads(second.split("\n", 2)[1])
    assert first_payload == [["consulta", {"query": "x"}, {"ok": True, "status": "found", "data": {"value": 42}}]]
    assert second_payload == [["outra", {}, {"ok": True, "status": "done"}]]
    assert "consulta" not in second
    # O lote anterior pode permanecer byte a byte igual no histórico; não há
    # reserialização cumulativa que invalide o prefixo do provider.
    assert compact_tool_evidence(first_records) == first
    verbose = json.dumps(first_records, ensure_ascii=False, separators=(",", ":"))
    assert len(first_payload[0]) == 3
    assert '"tool"' not in first.split("\n", 2)[1]
    assert len(first.split("\n", 2)[1]) < len(verbose)


def test_trivial_fast_path_predicate_only_accepts_term_free_context_without_tool_affinity():
    catalog = registry()
    assert ToolSelection(catalog, "oi").trivial_text_only_candidate() is True
    assert ToolSelection(catalog, "sim").trivial_text_only_candidate() is True
    # Palavra lexical real continua no fluxo normal mesmo sem ferramenta
    # correspondente: descoberta/retrieval podem ser necessárias.
    assert ToolSelection(catalog, "astronomia").trivial_text_only_candidate() is False
    # Confirmação curta após assunto operacional herda a afinidade do contexto.
    assert ToolSelection(catalog, "sim", recent_context="consultar música agora").trivial_text_only_candidate() is False


def test_response_format_is_not_core_but_is_selected_when_user_mentions_audio():
    catalog = registry()
    smalltalk = ToolSelection(catalog, "oi")
    assert "select_response_format" not in smalltalk.selected_names
    audio = ToolSelection(catalog, "responda em áudio")
    voice = ToolSelection(catalog, "manda por voz")
    assert "select_response_format" in audio.selected_names
    assert "select_response_format" in voice.selected_names
