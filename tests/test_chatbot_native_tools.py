"""Chamadas, resultados e fallback nativo preservam IDs sem executar efeitos."""
import json
import logging

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.action_protocol import NativeToolCall, TOOL_NAME, proposal_tool, parse_proposal
from cogs.chatbot.providers import ChatMessage, ChatReply, ProviderError, ProviderRouter, _GeminiClient, _GroqClient
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Session, _Response, _groq, _gemini


READ = ToolSpec("get_conversation_preferences", "Consulte preferências reais do autor.",
                {"type": "object", "properties": {}, "additionalProperties": False})
SET = ToolSpec("set_conversation_preferences", "Defina o modo de resposta da conversa própria.",
               {"type": "object", "properties": {"mode": {"type": "string", "enum": ["auto", "audio", "text"]}},
                "required": ["mode"], "additionalProperties": False}, permission="self")


async def call(provider, data, *, specs=(READ, SET), messages=()):
    session = _Session(_Response(data))
    client = (_GroqClient if provider == "groq" else _GeminiClient)(session, "private-key")
    reply = await client.chat(system="private system", messages=list(messages), temperature=.8,
                              model="test-model", timeout_seconds=5, tool_specs=specs)
    return reply, session.requests[0][1]["json"]


@pytest.mark.asyncio
async def test_groq_preserves_native_ids_and_serializes_tool_roundtrip():
    data = _groq(calls=[(READ.name, {})])
    data["choices"][0]["message"]["tool_calls"][0]["id"] = "call_real_id"
    first, payload = await call("groq", data)
    assert first.tool_calls == (NativeToolCall("call_real_id", READ.name, {}),)
    assert first.proposals == ()
    assert payload["max_completion_tokens"] == C.MAX_TOOL_RESPONSE_TOKENS
    second, payload = await call("groq", _groq("agora eu sei", finish="stop"), messages=[
        ChatMessage("user", "lembra como gosto?"),
        ChatMessage("assistant", first.text, tool_calls=first.tool_calls),
        ChatMessage("tool", '{"ok":true,"mode":"audio"}', tool_call_id="call_real_id", name=READ.name),
    ])
    assistant, result = payload["messages"][-2:]
    assert assistant["role"] == "assistant" and assistant["content"] is None
    assert assistant["tool_calls"][0]["id"] == "call_real_id"
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    assert result == {"role": "tool", "content": '{"ok":true,"mode":"audio"}',
                      "tool_call_id": "call_real_id", "name": READ.name}
    assert second.text == "agora eu sei" and second.tool_calls == ()


@pytest.mark.asyncio
async def test_gemini_preserves_function_part_signature_and_matching_result():
    part = {"functionCall": {"id": "gemini_real_id", "name": READ.name, "args": {}},
            "thoughtSignature": "opaque-native-signature"}
    first, payload = await call("gemini", {"candidates": [{"content": {"parts": [part]}, "finishReason": "STOP"}]})
    assert first.tool_calls[0].id == "gemini_real_id"
    assert first.tool_calls[0].provider_data == {"part": part}
    assert payload["generationConfig"]["maxOutputTokens"] == C.MAX_TOOL_RESPONSE_TOKENS
    _, payload = await call("gemini", _gemini("pronto"), messages=[
        ChatMessage("assistant", "", tool_calls=first.tool_calls),
        ChatMessage("tool", '{"ok":true,"mode":"text"}', tool_call_id=first.tool_calls[0].id, name=READ.name),
    ])
    assert payload["contents"][0] == {"role": "model", "parts": [part]}
    assert payload["contents"][1] == {"role": "user", "parts": [{"functionResponse": {
        "id": "gemini_real_id", "name": READ.name, "response": {"ok": True, "mode": "text"},
    }}]}
    assert READ.parameters["additionalProperties"] is False


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_tool_only_reply_has_id_when_provider_omits_it(provider):
    reply, _ = await call(provider, (_groq if provider == "groq" else _gemini)(calls=[(READ.name, {})]))
    assert reply.text == "" and reply.tool_calls[0].id.startswith("call_")


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("bad", [{"mode": "unknown"}, {"mode": "audio", "target": "anotheruser"}, [], None])
@pytest.mark.asyncio
async def test_invalid_generic_batch_is_atomic_and_hides_private_audio(provider, bad, caplog):
    declaration = proposal_tool(("send_audio",))
    action_spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    secret = "private spoken transcript"
    data = (_groq if provider == "groq" else _gemini)(secret, calls=[
        (TOOL_NAME, {"action": "send_audio", "text": secret}), (SET.name, bad),
    ])
    with pytest.raises(ProviderError) as error:
        await call(provider, data, specs=(action_spec, SET))
    assert error.value.kind == "invalid_response" and error.value.stage == "output"
    assert secret not in str(error.value) and secret not in caplog.text


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_truncated_generic_calls_never_become_executable(provider):
    data = _groq(calls=[(READ.name, {})], finish="length") if provider == "groq" else _gemini(calls=[(READ.name, {})], finish="MAX_TOKENS")
    with pytest.raises(ProviderError) as error:
        await call(provider, data)
    assert error.value.kind == "invalid_response"


@pytest.mark.parametrize("finish", ["MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"])
@pytest.mark.asyncio
async def test_gemini_invalid_function_finish_rejects_even_valid_prefix_call(finish):
    with pytest.raises(ProviderError) as error:
        await call("gemini", _gemini("não publicar", calls=[(READ.name, {})], finish=finish))
    assert error.value.kind == "invalid_response" and error.value.finish_reason == finish


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"wrong": "envelope"}, [{"type": "custom", "function": {"name": READ.name, "arguments": "{}"}}]])
async def test_groq_rejects_malformed_tool_envelope(bad):
    data = _groq("discard me")
    data["choices"][0]["message"]["tool_calls"] = bad
    with pytest.raises(ProviderError) as error:
        await call("groq", data)
    assert error.value.kind == "invalid_response"


@pytest.mark.asyncio
async def test_duplicate_native_call_ids_fail_before_any_effect():
    data = _groq(calls=[(READ.name, {}), (SET.name, {"mode": "audio"})])
    for item in data["choices"][0]["message"]["tool_calls"]:
        item["id"] = "same-id"
    with pytest.raises(ProviderError):
        await call("groq", data)


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_valid_audio_proposal_uses_current_catalog_and_keeps_speech_private(provider):
    declaration = proposal_tool(("send_audio", "timeout_member"), ("autor", "m1"))
    action_spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    secret = "A resposta toda em áudio, sem prévia pública."
    data = (_groq if provider == "groq" else _gemini)(secret, calls=[
        (TOOL_NAME, {"action": "send_audio", "text": secret, "ask_permission": True}),
        (TOOL_NAME, {"action": "timeout_member", "target_ref": "m1", "options": {"duration_seconds": 60}}),
    ])
    reply, payload = await call(provider, data, specs=(action_spec,))
    assert reply.text == "" and len(reply.tool_calls) == len(reply.proposals) == 2
    assert reply.proposals[0].text == secret and reply.proposals[0].ask_permission is False
    assert "ask_permission" not in reply.tool_calls[0].arguments
    assert reply.proposals[1].options == {"duration_seconds": 60}
    if provider == "gemini":
        assert "additionalProperties" not in json.dumps(payload["tools"])
    assert action_spec.parameters["properties"]["options"]["additionalProperties"] is False


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_action_requirements_and_partial_draft_guidance_keep_native_schema_compatible(provider):
    declaration = proposal_tool(("timeout_member", "join_voice", "move_voice", "leave_voice"), ("autor", "m1"))
    action_spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    _reply, payload = await call(provider, (_groq if provider == "groq" else _gemini)("Por quanto tempo?"), specs=(action_spec,))
    exported = payload["tools"][0]["function"] if provider == "groq" else payload["tools"][0]["functionDeclarations"][0]
    parameters = exported["parameters"]
    duration = parameters["properties"]["options"]["properties"]["duration_seconds"]
    assert duration["type"] == "integer" and duration["minimum"] == 1 and duration["maximum"] == 2419200
    assert "duration_seconds" not in parameters["properties"]
    assert "obrigatório" in parameters["properties"]["reason"]["description"]
    assert "Opcional para join_voice/move_voice/leave_voice" in parameters["properties"]["reason"]["description"]
    assert "nunca invente um motivo" in exported["description"]
    assert "Um rascunho nunca executa nem aprova" in exported["description"]
    assert not {"if", "then", "else", "oneOf", "allOf", "$ref"} & set(parameters)
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("options", [
    {"duration_seconds": True}, {"duration_seconds": 2419201},
    {"channel_changes": {"delete": True}}, {"channel_changes": {"slowmode_delay": 21601}},
    {"nickname": "x" * 33}, {"role_ref": ""}, {"message_refs": ["msg1"] * 26},
])
def test_action_options_are_bounded_and_never_export_arbitrary_changes(options):
    from cogs.chatbot.action_protocol import InvalidActionProposal
    with pytest.raises(InvalidActionProposal):
        parse_proposal(TOOL_NAME, {"action": "ban_member", "target_ref": "m1", "options": options}, ("ban_member",))


@pytest.mark.asyncio
async def test_other_native_groq_model_wins_before_text_degradation_or_gemini(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-no-tools", "groq-tools"))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-tools",))
    session = _Session(_Response({"error": {"message": "This model does not support tool calling"}}, status=400),
                       _Response(_groq(calls=[(READ.name, {})])))
    router = ProviderRouter(session, groq_key="private", gemini_key="private")
    reply = await router.chat(system="s", messages=[], tool_specs=(READ,))
    assert reply.provider == "groq" and reply.model == "groq-tools" and reply.tool_calls
    assert [request[1]["json"]["model"] for request in session.requests] == ["groq-no-tools", "groq-tools"]
    assert all("tools" in request[1]["json"] for request in session.requests)


@pytest.mark.asyncio
async def test_groq_default_ignores_legacy_env_priority_and_respects_remaining_budget(monkeypatch):
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("gemini", "groq"))
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main",))
    session = _Session(_Response(_groq("oi", finish="stop")))
    router = ProviderRouter(session, groq_key="private", gemini_key="private")
    assert await router.chat(system="s", messages=[], budget_seconds=1.5) == "oi"
    assert len(session.requests) == 1 and session.requests[0][1]["json"]["model"] == "groq-main"
    assert session.requests[0][1]["timeout"].total <= 1.5


@pytest.mark.asyncio
async def test_last_text_fallback_is_honest_and_logs_actual_winner(monkeypatch, caplog):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-no-tools",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-tools",))
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    session = _Session(_Response({"error": {"message": "This model does not support tool calling"}}, status=400),
                       _Response({"error": {"message": "model not found"}}, status=404),
                       _Response(_groq("posso conversar", finish="stop")))
    router = ProviderRouter(session, groq_key="private", gemini_key="private")
    reply = await router.chat(system="private system", messages=[], tool_specs=(READ,))
    assert reply == ChatReply("posso conversar", provider="groq", model="groq-no-tools")
    payload = session.requests[-1][1]["json"]
    assert "tools" not in payload
    assert "ferramentas estão indisponíveis" in payload["messages"][0]["content"]
    assert "result=success provider=groq model=groq-no-tools" in caplog.text
    assert "private" not in caplog.text


def test_protocol_accepts_canonical_mentions_but_does_not_authorize_them():
    for ref in ("123", "<@123>", "<@!123>"):
        proposal = parse_proposal(TOOL_NAME, {"action": "timeout_member", "target_ref": ref,
                                  "options": {"duration_seconds": 60}}, ("timeout_member",))
        assert proposal.target_ref == ref and proposal.options == {"duration_seconds": 60}
