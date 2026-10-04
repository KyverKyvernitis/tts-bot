"""Diagnóstico sem valores privados e contrato de navegação do próprio bot."""
from types import SimpleNamespace

import pytest

from cogs.chatbot.action_policy import ActionDenied, prepare_action
from cogs.chatbot.action_protocol import (
    InvalidActionProposal, TOOL_NAME, parse_proposal, proposal_tool,
)
from cogs.chatbot.providers import ProviderError
from cogs.chatbot.tool_registry import (
    InvalidToolArguments, ToolSpec, tool_summary, validate_tool_arguments,
)
from tests.test_chatbot_action_policy import _expanded_voice, expanded, in_call, world
from tests.test_chatbot_action_providers import _gemini, _groq
from tests.test_chatbot_native_tools import call


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["options"],
    "properties": {
        "options": {
            "type": "object", "additionalProperties": False, "required": ["duration_seconds"],
            "properties": {
                "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 2419200},
                "mode": {"type": "string", "enum": ["audio", "text"]},
                "refs": {"type": "array", "minItems": 1, "maxItems": 2,
                         "items": {"type": "string", "minLength": 1, "maxLength": 10}},
            },
        },
    },
}


@pytest.mark.parametrize("arguments,code,path", [
    (None, "type", "$"),
    ({}, "required", "$/options"),
    ({"options": {}}, "required", "$/options/duration_seconds"),
    ({"options": {"duration_seconds": "PRIVATE 27"}}, "type", "$/options/duration_seconds"),
    ({"options": {"duration_seconds": True}}, "type", "$/options/duration_seconds"),
    ({"options": {"duration_seconds": 27.0}}, "type", "$/options/duration_seconds"),
    ({"options": {"duration_seconds": 0}}, "minimum", "$/options/duration_seconds"),
    ({"options": {"duration_seconds": 2419201}}, "maximum", "$/options/duration_seconds"),
    ({"options": {"duration_seconds": 27, "mode": "PRIVATE MODE"}}, "enum", "$/options/mode"),
    ({"options": {"duration_seconds": 27, "PRIVATE KEY": "PRIVATE VALUE"}}, "additional_properties", "$/options"),
    ({"options": {"duration_seconds": 27}, "PRIVATE KEY": "PRIVATE VALUE"}, "additional_properties", "$"),
    ({"options": {"duration_seconds": 27, "refs": []}}, "min_items", "$/options/refs"),
    ({"options": {"duration_seconds": 27, "refs": ["x"] * 3}}, "max_items", "$/options/refs"),
    ({"options": {"duration_seconds": 27, "refs": ["PRIVATE LONG REF"]}}, "max_length", "$/options/refs/*"),
    ({"options": {"duration_seconds": 27, "refs": [""]}}, "min_length", "$/options/refs/*"),
    ({"options": {"duration_seconds": 27, "refs": ["\x00"]}}, "null_character", "$/options/refs/*"),
    ({"options": {"duration_seconds": float("nan")}}, "serialization", "$"),
    ({"PRIVATE KEY": "x" * 8193}, "argument_size", "$"),
])
def test_schema_failure_reports_only_trusted_field_path(arguments, code, path):
    with pytest.raises(InvalidToolArguments) as error:
        validate_tool_arguments(arguments, SCHEMA)
    failure = error.value
    assert (failure.code, failure.path) == (code, path)
    assert str(failure) == "Argumentos inválidos."
    assert "PRIVATE" not in repr(failure) + failure.code + failure.path


def test_schema_pointer_escapes_only_host_declared_names_and_any_of_is_generic():
    schema = {"type": "object", "properties": {
        "trusted/name~": {"anyOf": [{"type": "integer"}, {"type": "boolean"}]},
    }}
    with pytest.raises(InvalidToolArguments) as error:
        validate_tool_arguments({"trusted/name~": "private value"}, schema)
    assert error.value.code == "any_of"
    assert error.value.path == "$/trusted~1name~0"
    assert "private" not in str(error.value)


@pytest.mark.parametrize("arguments,code,path", [
    ({"action": "ban_member"}, "required", "$/target_ref"),
    ({"action": "ban_member", "target_ref": ""}, "required", "$/target_ref"),
    ({"action": "ban_member", "target_ref": "PRIVATE NAME"}, "target_reference", "$/target_ref"),
    ({"action": "send_audio", "text": "  "}, "required", "$/text"),
    ({"action": "leave_voice", "text": "PRIVATE SPEECH"}, "forbidden_field", "$/text"),
    ({"action": "join_voice", "options": None}, "type", "$/options"),
    ({"action": "join_voice", "options": {"duration_seconds": 27.0}}, "type", "$/options/duration_seconds"),
    ({"action": "join_voice", "options": {"PRIVATE KEY": "PRIVATE VALUE"}}, "additional_properties", "$/options"),
    ('{"action":"join_voice","action":"leave_voice"}', "duplicate_key", "$"),
    ('{"action":"join_voice","PRIVATE KEY":NaN}', "invalid_constant", "$"),
    ('{"PRIVATE KEY":broken}', "invalid_json", "$"),
])
def test_proposal_failure_preserves_safe_schema_or_semantic_diagnostics(arguments, code, path):
    with pytest.raises(InvalidActionProposal) as error:
        parse_proposal(TOOL_NAME, arguments, ("join_voice", "leave_voice", "ban_member", "send_audio"))
    assert (error.value.code, error.value.path) == (code, path)
    assert str(error.value) == "proposta inválida"
    assert "PRIVATE" not in error.value.code + error.value.path + str(error.value)


@pytest.mark.parametrize("action", ["join_voice", "move_voice", "send_audio", "speak_voice"])
@pytest.mark.parametrize("explicit_empty", [True, False])
def test_automatic_actions_can_omit_target_without_inventing_native_arguments(action, explicit_empty):
    declaration = proposal_tool((action,), ("autor", "m1"))
    arguments = {"action": action}
    if explicit_empty:
        arguments["target_ref"] = ""
    if action in {"send_audio", "speak_voice"}:
        arguments["text"] = "fala privada"
    validated = validate_tool_arguments(arguments, declaration["parameters"])
    parsed = parse_proposal(TOOL_NAME, validated, (action,))
    assert validated == arguments
    assert parsed.target_ref == ""
    with pytest.raises(InvalidToolArguments) as error:
        validate_tool_arguments(dict(arguments, target_ref="999999999"), declaration["parameters"])
    assert error.value.code == "enum" and error.value.path == "$/target_ref"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("explicit_empty", [True, False])
async def test_native_join_then_speech_accepts_author_omission_and_hides_speech(provider, explicit_empty):
    declaration = proposal_tool(("join_voice", "speak_voice"), ("autor", "m1"))
    spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    first = {"action": "join_voice"}
    second = {"action": "speak_voice", "text": "PRIVATE SPEECH"}
    if explicit_empty:
        first["target_ref"] = second["target_ref"] = ""
    factory = _groq if provider == "groq" else _gemini
    reply, _payload = await call(provider, factory("PRIVATE SPEECH", calls=[
        (TOOL_NAME, first), (TOOL_NAME, second),
    ]), specs=(spec,))
    assert reply.text == ""
    assert [proposal.action for proposal in reply.proposals] == ["join_voice", "speak_voice"]
    assert [proposal.target_ref for proposal in reply.proposals] == ["", ""]
    assert [native.arguments for native in reply.tool_calls] == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_native_moderation_still_requires_explicit_target(provider):
    declaration = proposal_tool(("ban_member",), ("autor", "m1"))
    spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    factory = _groq if provider == "groq" else _gemini
    with pytest.raises(ProviderError) as error:
        await call(provider, factory(calls=[(TOOL_NAME, {
            "action": "ban_member", "target_ref": "", "reason": "motivo fornecido",
        })]), specs=(spec,))
    assert error.value.kind == "invalid_response"


@pytest.mark.asyncio
async def test_omitted_join_target_uses_actual_author_call_and_never_another_member(world):
    targets = {"autor": world.members[1], "m1": world.members[3]}
    parsed = parse_proposal(TOOL_NAME, {"action": "join_voice"}, ("join_voice",))
    in_call(world, 3)
    with pytest.raises(ActionDenied):
        await prepare_action(world.bot, world.message, parsed, targets=targets, config=world.config)
    in_call(world, 1)
    prepared = await prepare_action(world.bot, world.message, parsed, targets=targets, config=world.config)
    assert prepared["payload"]["target_id"] == world.message.author.id
    assert prepared["payload"]["voice_channel_id"] == world.voice.id
    assert prepared["ask_permission"] is False
    world.voice.permissions_for.return_value = SimpleNamespace(view_channel=False, connect=True, speak=True)
    with pytest.raises(ActionDenied):
        await prepare_action(world.bot, world.message, parsed, targets=targets, config=world.config)
    world.tts.chatbot_join_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_omitted_move_target_does_not_choose_other_member_or_bypass_visibility(expanded):
    destination = _expanded_voice(expanded)
    proposal = parse_proposal(TOOL_NAME, {"action": "move_voice"}, ("move_voice",))
    # m1 está em outra call, mas autor continua na call atual: omissão não
    # escolhe o primeiro membro disponível nem permite uma movimentação vazia.
    with pytest.raises(ActionDenied):
        await prepare_action(expanded.bot, expanded.message, proposal, targets=expanded.targets,
                             resources=expanded.resources, config=expanded.config)
    expanded.message.author.voice = SimpleNamespace(channel=destination)
    prepared = await prepare_action(expanded.bot, expanded.message, proposal, targets=expanded.targets,
                                    resources=expanded.resources, config=expanded.config)
    assert prepared["payload"]["target_id"] == expanded.message.author.id
    assert prepared["payload"]["voice_channel_id"] == destination.id
    assert prepared["payload"]["source_voice_channel_id"] == expanded.voice.id
    assert prepared["ask_permission"] is False
    destination.permissions_for.return_value = SimpleNamespace(view_channel=False, connect=True, speak=True)
    with pytest.raises(ActionDenied):
        await prepare_action(expanded.bot, expanded.message, proposal, targets=expanded.targets,
                             resources=expanded.resources, config=expanded.config)
    expanded.tts.chatbot_move_voice.assert_not_awaited()


def test_channel_topic_schema_matches_host_limit_without_relaxing_other_types():
    declaration = proposal_tool(("edit_channel",))
    arguments = {"action": "edit_channel", "options": {"channel_changes": {"topic": "x" * 500}}}
    assert validate_tool_arguments(arguments, declaration["parameters"]) == arguments
    arguments["options"]["channel_changes"]["topic"] += "x"
    with pytest.raises(InvalidActionProposal) as error:
        parse_proposal(TOOL_NAME, arguments, ("edit_channel",))
    assert (error.value.code, error.value.path) == ("max_length", "$/options/channel_changes/topic")


def test_catalog_summary_keeps_operational_contract_without_long_description_duplication():
    declaration = proposal_tool(("join_voice", "speak_voice", "ban_member"), ("autor", "m1"))
    spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"], permission="staff para moderação")
    unavailable = ToolSpec("send_image", "Gere uma imagem.", {"type": "object"}, available=False, why="Adapter indisponível.")
    summary = tool_summary((spec, unavailable))
    assert "propor_acao: Propõe uma ação ao sistema; esta ferramenta não executa nada." in summary
    assert "staff para moderação" in summary and "Argumentos obrigatórios: action" in summary
    assert "send_image: indisponível. Adapter indisponível." in summary
    assert "text é PRIVADO" not in summary
    assert "text é PRIVADO" in spec.native_declaration()["description"]
    assert len(summary) < len(declaration["description"])
