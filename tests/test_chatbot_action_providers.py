"""Propostas nativas são dados privados, nunca execução ou parsing de rodapé."""
from __future__ import annotations

import base64
import json
from dataclasses import FrozenInstanceError

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.action_protocol import (
    ActionProposal, ChatReply, InvalidActionProposal, TOOL_NAME,
    enabled_actions, parse_proposal, parse_proposals,
)
from cogs.chatbot.media import PreparedImage
from cogs.chatbot.providers import (
    AllProvidersExhausted, ChatMessage, ProviderError, ProviderRouter,
    _GeminiClient, _GroqClient,
)


class _Body:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    async def iter_chunked(self, size):
        yield self.data


class _Response:
    def __init__(self, data, status=200):
        self.status = status
        self.headers = {}
        self.content = _Body(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def _groq(text=None, calls=(), *, finish="tool_calls", refusal=None):
    message = {"content": text}
    if calls:
        message["tool_calls"] = [{"type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        }} for name, arguments in calls]
    if refusal is not None:
        message["refusal"] = refusal
    return {"choices": [{"message": message, "finish_reason": finish}]}


def _gemini(text=None, calls=(), *, finish="STOP"):
    parts = [{"text": text}] if text is not None else []
    parts.extend({"functionCall": {"name": name, "args": arguments}} for name, arguments in calls)
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}


async def _call(
    provider, data, *, actions=("send_audio", "join_voice", "ban_member"), images=(), target_refs=(),
):
    session = _Session(_Response(data))
    client = (_GroqClient if provider == "groq" else _GeminiClient)(session, "private-api-key")
    reply = await client.chat(
        system="private system", messages=[ChatMessage("user", "private message", images=list(images))],
        temperature=.8, model="test-model", timeout_seconds=5, actions=actions, target_refs=target_refs,
    )
    return reply, session.requests[0][1]["json"]


def test_protocol_frozen_and_action_allowlist():
    proposal = ActionProposal("send_audio", text="oi")
    with pytest.raises(FrozenInstanceError):
        proposal.text = "outro"
    with pytest.raises(FrozenInstanceError):
        ChatReply("oi").text = "outro"
    assert enabled_actions(("unknown", "ban_member", "ban_member", "send_audio")) == ("ban_member", "send_audio")


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_native_tool_schema_function_only_and_original_image_bytes(provider):
    factory = _groq if provider == "groq" else _gemini
    speech = "private speech sem prévia"
    image = PreparedImage("image/png", b"private prepared image bytes", "private.png")
    reply, payload = await _call(provider, factory(calls=[(TOOL_NAME, {
        "action": "send_audio", "target_ref": "autor", "text": speech,
    })]), images=[image])
    assert reply == ChatReply("", (ActionProposal("send_audio", "autor", speech),), provider, "test-model")
    if provider == "groq":
        tool = payload["tools"][0]["function"]
        encoded = payload["messages"][-1]["content"][1]["image_url"]["url"].split(",", 1)[1]
        assert payload["tool_choice"] == "auto"
    else:
        tool = payload["tools"][0]["functionDeclarations"][0]
        encoded = payload["contents"][-1]["parts"][1]["inlineData"]["data"]
        assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    assert base64.b64decode(encoded) == image.data
    assert tool["name"] == TOOL_NAME
    assert tool["parameters"]["properties"]["action"]["enum"] == ["send_audio", "join_voice", "ban_member"]
    assert "enum" not in tool["parameters"]["properties"]["target_ref"]
    assert "ask_permission" not in tool["parameters"]["properties"]
    assert "ask_permission" not in tool["description"]
    assert "No máximo uma proposta de áudio ou fala" in tool["description"]
    assert "não combine send_audio com speak_voice" in tool["description"]
    assert speech not in json.dumps(payload)
    assert "PRIVADO" in tool["description"]


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_native_schema_uses_only_concrete_host_target_refs_and_audio_omits_target(provider):
    factory = _groq if provider == "groq" else _gemini
    reply, payload = await _call(provider, factory(calls=[(TOOL_NAME, {
        "action": "send_audio", "text": "uma fala sem alvo escolhido pela IA",
    })]), target_refs=("autor", "m1", "m1", "m2"))
    tool = payload["tools"][0]["function"] if provider == "groq" else payload["tools"][0]["functionDeclarations"][0]
    target_schema = tool["parameters"]["properties"]["target_ref"]
    assert target_schema["enum"] == ["autor", "m1", "m2"]
    assert "omita target_ref" in tool["description"]
    assert "Entrar/mudar" not in tool["description"]
    assert "Omita em send_audio/speak_voice" in target_schema["description"]
    assert "target_ref" not in tool["parameters"]["required"]
    assert reply.proposals[0].target_ref == ""


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_legacy_asked_audio_becomes_automatic_without_repeating_private_reply(provider):
    factory = _groq if provider == "groq" else _gemini
    reply, _ = await _call(provider, factory("Vou dizer: fala privada", [(TOOL_NAME, {
        "action": "send_audio", "text": "fala privada", "ask_permission": True,
        "reason": "justificativa privada",
    })]))
    assert reply.text == ""
    assert reply.proposals[0].text == "fala privada"
    assert reply.proposals[0].reason == "justificativa privada"
    assert reply.proposals[0].ask_permission is False


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_plain_permission_footer_never_creates_action(provider):
    factory = _groq if provider == "groq" else _gemini
    text = "Pediu permissão: banir @usuario"
    reply, _ = await _call(provider, factory(text))
    assert reply.text == text
    assert reply.proposals == ()


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_disabled_actions_leave_old_string_api_and_payload_untouched(provider):
    factory = _groq if provider == "groq" else _gemini
    reply, payload = await _call(provider, factory("oi", [(TOOL_NAME, {"action": "ban_member", "target_ref": "m1"})]), actions=(), target_refs=("autor", "m1"))
    assert reply == "oi"
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "toolConfig" not in payload


@pytest.mark.parametrize("arguments", [
    {"action": "delete_server"},
    {"action": "ban_member", "target_ref": "<@&123>"},
    {"action": "ban_member", "target_ref": "0"},
    {"action": "ban_member"},
    {"action": "send_audio", "text": ""},
    {"action": "send_audio", "text": "x" * 801},
    {"action": "send_audio", "text": "x", "ask_permission": "true"},
    {"action": "send_audio", "text": "x", "reason": "x" * 501},
    {"action": "send_audio", "text": "x", "evil": True},
    {"action": "ban_member", "target_ref": "m1", "text": "unexpected speech"},
    {"action": "send_audio", "text": "\ud800"},
    [], None, '{"action":"send_audio","action":"ban_member"}',
    '{"action":"send_audio","text":NaN}', "x" * 8193,
])
def test_strict_bounded_proposal_arguments(arguments):
    with pytest.raises(InvalidActionProposal):
        parse_proposal(TOOL_NAME, arguments, ("send_audio", "ban_member"))


def test_unknown_function_disabled_action_and_too_many_calls_are_rejected():
    with pytest.raises(InvalidActionProposal):
        parse_proposal("execute", {"action": "send_audio", "text": "oi"}, ("send_audio",))
    with pytest.raises(InvalidActionProposal):
        parse_proposal(TOOL_NAME, {"action": "ban_member", "target_ref": "m1"}, ("send_audio",))
    call = (TOOL_NAME, {"action": "send_audio", "text": "oi"})
    with pytest.raises(InvalidActionProposal):
        parse_proposals([call] * 5, ("send_audio",))


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_two_valid_proposals_keep_order_without_performing_them(provider):
    factory = _groq if provider == "groq" else _gemini
    reply, _ = await _call(provider, factory("Vou solicitar.", [
        (TOOL_NAME, {"action": "join_voice", "target_ref": "autor", "reason": "pedido na conversa"}),
        (TOOL_NAME, {"action": "send_audio", "text": "fala privada"}),
    ]))
    assert [proposal.action for proposal in reply.proposals] == ["join_voice", "send_audio"]
    assert reply.text == ""  # Qualquer fala nativa fica fora da resposta pública.
    assert reply.proposals[0].ask_permission is False  # Staff approval is enforced by the host.


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_malformed_call_discards_text_and_private_arguments(provider, caplog):
    factory = _groq if provider == "groq" else _gemini
    with pytest.raises(ProviderError) as failure:
        await _call(provider, factory("texto normal", [("private wrong function", {
            "action": "send_audio", "text": "private argument",
        })]))
    assert failure.value.kind == "invalid_response"
    assert "private" not in caplog.text


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("invalid_second", [True, False])
@pytest.mark.asyncio
async def test_invalid_mixed_batch_cannot_leak_optional_audio_preview(provider, invalid_second, caplog):
    factory = _groq if provider == "groq" else _gemini
    secret = "FALA PRIVADA DO ÁUDIO"
    private_call = (TOOL_NAME, {"action": "send_audio", "text": secret, "ask_permission": True})
    calls = [private_call, ("unknown", {"private": secret})] if invalid_second else [private_call] * 5
    with pytest.raises(ProviderError) as failure:
        await _call(provider, factory(secret, calls))
    assert failure.value.kind == "invalid_response"
    assert secret not in str(failure.value) and secret not in caplog.text


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_four_native_proposals_preserve_chain_order_and_distinct_ban_targets(provider):
    factory = _groq if provider == "groq" else _gemini
    calls = [
        (TOOL_NAME, {"action": "join_voice", "target_ref": "autor"}),
        (TOOL_NAME, {"action": "speak_voice", "text": "Fala depois da entrada."}),
        (TOOL_NAME, {"action": "ban_member", "target_ref": "m1", "reason": "motivo para A"}),
        (TOOL_NAME, {"action": "ban_member", "target_ref": "m2", "reason": "motivo para B"}),
    ]
    reply, payload = await _call(
        provider, factory("Não publique a fala antes da execução.", calls),
        actions=("send_audio", "speak_voice", "join_voice", "ban_member"),
        target_refs=("autor", "m1", "m2"),
    )
    assert [(proposal.action, proposal.target_ref) for proposal in reply.proposals] == [
        ("join_voice", "autor"), ("speak_voice", ""), ("ban_member", "m1"), ("ban_member", "m2"),
    ]
    assert reply.text == ""
    tool = payload["tools"][0]["function"] if provider == "groq" else payload["tools"][0]["functionDeclarations"][0]
    assert "Até quatro propostas" in tool["description"]
    assert "join_voice antes de speak_voice" in tool["description"]
    assert "aprovação separada" in tool["description"]


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.asyncio
async def test_only_native_action_payload_receives_budget_for_four_proposals(provider, vision, native):
    factory = _groq if provider == "groq" else _gemini
    images = [PreparedImage("image/png", b"prepared image bytes")] if vision else []
    _, payload = await _call(provider, factory("texto"), images=images, actions=("send_audio",) if native else ())
    tokens = payload["max_completion_tokens"] if provider == "groq" else payload["generationConfig"]["maxOutputTokens"]
    assert tokens == (2000 if native else 1000 if vision else 500)


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_truncated_native_tools_never_return_even_a_valid_first_proposal(provider):
    factory = _groq if provider == "groq" else _gemini
    finish = "length" if provider == "groq" else "MAX_TOKENS"
    with pytest.raises(ProviderError) as failure:
        await _call(provider, factory(calls=[(TOOL_NAME, {
            "action": "ban_member", "target_ref": "m1", "reason": "motivo válido",
        })], finish=finish))
    assert failure.value.kind == "invalid_response" and failure.value.finish_reason == finish


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_malformed_function_only_is_safe_provider_error(provider):
    factory = _groq if provider == "groq" else _gemini
    with pytest.raises(ProviderError) as failure:
        await _call(provider, factory(calls=[("unknown", {"private": "payload"})]))
    assert failure.value.kind == "invalid_response"
    assert "payload" not in str(failure.value)


@pytest.mark.asyncio
async def test_explicit_refusal_is_terminal_text_without_attached_proposal():
    reply, _ = await _call("groq", _groq(refusal="Não posso fazer isso.", calls=[(TOOL_NAME, {
        "action": "ban_member", "target_ref": "m1",
    })]))
    assert reply.text == "Não posso fazer isso."
    assert not reply.proposals


@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.asyncio
async def test_structured_block_rejects_even_a_valid_proposal(provider):
    call = [(TOOL_NAME, {"action": "send_audio", "text": "private speech"})]
    data = _groq(calls=call, finish="content_filter") if provider == "groq" else _gemini(calls=call, finish="SAFETY")
    with pytest.raises(ProviderError) as failure:
        await _call(provider, data)
    assert failure.value.kind == "blocked"


@pytest.mark.asyncio
async def test_router_returns_only_successful_fallback_proposals(monkeypatch, caplog):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-model",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-model",))
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("groq", "gemini"))
    session = _Session(
        _Response(_groq(calls=[("private invalid tool", {"private": "first proposed data"})])),
        _Response(_gemini(calls=[(TOOL_NAME, {"action": "send_audio", "text": "fala final"})])),
    )
    reply = await ProviderRouter(session, groq_key="private", gemini_key="private").chat(
        system="private system", messages=[ChatMessage("user", "oi")], actions=("send_audio",),
    )
    assert reply == ChatReply("", (ActionProposal("send_audio", text="fala final"),), "gemini", "gemini-model")
    assert len(session.requests) == 2
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_router_preserves_host_target_enum_across_native_provider_fallback(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-model",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-model",))
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("groq", "gemini"))
    session = _Session(
        _Response({"error": {"code": "model_not_found"}}, status=404),
        _Response(_gemini(calls=[(TOOL_NAME, {"action": "ban_member", "target_ref": "m1", "reason": "spam"})])),
    )
    reply = await ProviderRouter(session, groq_key="key", gemini_key="key").chat(
        system="s", messages=[ChatMessage("user", "pedido")],
        actions=("ban_member",), target_refs=("autor", "m1"),
    )
    assert reply.proposals == (ActionProposal("ban_member", "m1", reason="spam"),)
    groq_tool = session.requests[0][1]["json"]["tools"][0]["function"]
    gemini_tool = session.requests[1][1]["json"]["tools"][0]["functionDeclarations"][0]
    assert groq_tool["parameters"]["properties"]["target_ref"]["enum"] == ["autor", "m1"]
    assert gemini_tool["parameters"]["properties"]["target_ref"]["enum"] == ["autor", "m1"]


@pytest.mark.asyncio
async def test_router_does_not_forward_private_failed_output_to_fallback_or_public_reply(monkeypatch, caplog):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-model",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-model",))
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("groq", "gemini"))
    secret = "fala privada após autorização"
    session = _Session(
        _Response(_groq(secret, [
            (TOOL_NAME, {"action": "send_audio", "text": secret, "ask_permission": True}),
            ("unknown", {"private": secret}),
        ])),
        _Response(_gemini("resposta comum do fallback")),
    )
    reply = await ProviderRouter(session, groq_key="key", gemini_key="key").chat(
        system="s", messages=[ChatMessage("user", "oi")], actions=("send_audio",),
    )
    assert reply.text == "resposta comum do fallback" and not reply.proposals
    assert secret not in json.dumps(session.requests[1][1]["json"])
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_router_block_never_tries_next_model_or_provider(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("first", "second"))
    session = _Session(_Response(_groq(finish="content_filter")))
    router = ProviderRouter(session, groq_key="key", gemini_key="key")
    with pytest.raises(AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[], actions=("ban_member",))
    assert failure.value.kind == "blocked"
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_unsupported_tool_model_keeps_text_chat_and_remembers_capability(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("text-only-model",))
    session = _Session(
        _Response({"error": {"message": "This model does not support tool calling"}}, status=400),
        _Response(_groq("posso conversar", finish="stop")),
        _Response(_groq("continuando", finish="stop")),
    )
    router = ProviderRouter(session, groq_key="key")
    first = await router.chat(system="s", messages=[], actions=("send_audio",), target_refs=("autor",))
    second = await router.chat(system="s", messages=[], actions=("send_audio",), target_refs=("autor",))
    assert first == ChatReply("posso conversar", provider="groq", model="text-only-model")
    assert second == ChatReply("continuando", provider="groq", model="text-only-model")
    assert "tools" in session.requests[0][1]["json"]
    assert "tools" not in session.requests[1][1]["json"]
    assert "tools" not in session.requests[2][1]["json"]
    assert router.snapshot()["groq/text-only-model"]["failures"] == 0


@pytest.mark.asyncio
async def test_absent_actions_does_not_add_keyword_to_legacy_client(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("legacy",))

    class LegacyClient:
        async def chat(self, *, system, messages, temperature, model, timeout_seconds):
            return "legacy text"

    router = ProviderRouter(object(), groq_key="key")
    router._groq = LegacyClient()
    assert await router.chat(system="s", messages=[], target_refs=("autor", "m1")) == "legacy text"


@pytest.mark.asyncio
async def test_absent_target_refs_preserves_previous_action_client_keyword_contract(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("legacy-actions",))

    class ExistingActionClient:
        async def chat(self, *, system, messages, temperature, model, timeout_seconds, actions):
            assert actions == ("send_audio",)
            return ChatReply("resposta comum")

    router = ProviderRouter(object(), groq_key="key")
    router._groq = ExistingActionClient()
    assert (await router.chat(system="s", messages=[], actions=("send_audio",))).text == "resposta comum"
