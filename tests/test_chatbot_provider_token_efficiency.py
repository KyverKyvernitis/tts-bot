"""Measured token accounting, request isolation, and model-specific reasoning."""
from __future__ import annotations

import asyncio
import json

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.action_protocol import NativeToolCall
from cogs.chatbot.action_protocol import TOOL_NAME, proposal_tool
from cogs.chatbot.media import PreparedImage
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Response, _Session, _gemini, _groq
from tests.test_chatbot_action_flow import world


def provider_client(provider, session):
    if provider == "cloudflare":
        return P._CloudflareClient(session, "offline-key", "a" * 32)
    return (P._GroqClient if provider == "groq" else P._GeminiClient)(session, "offline-key")


def output_cap(provider, payload):
    return payload["generationConfig"]["maxOutputTokens"] if provider == "gemini" else payload[
        "max_tokens" if provider == "cloudflare" else "max_completion_tokens"]


def exported_declarations(provider, payload):
    return payload["tools"][0]["functionDeclarations"] if provider == "gemini" else [
        item["function"] for item in payload["tools"]]


@pytest.fixture
def chains(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("openai/gpt-oss-120b",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-2.5-flash",))


@pytest.mark.asyncio
@pytest.mark.parametrize("model,effort", [("openai/gpt-oss-120b", "low"), ("openai/gpt-oss-20b", "low"),
                                       ("qwen/qwen3.8-27b", "none"), ("arbitrary-model", None),
                                       ("openai/gpt-oss-safeguard-20b", None)])
async def test_reasoning_option_only_for_documented_groq_models(model, effort):
    session = _Session(_Response(_groq("pronto", finish="stop")))
    await P._GroqClient(session, "offline-key").chat(system="s", messages=[], temperature=.8, model=model, timeout_seconds=5)
    payload = session.requests[0][1]["json"]
    if effort is None:
        assert "reasoning_effort" not in payload and "include_reasoning" not in payload
    else:
        assert payload["reasoning_effort"] == effort and payload["include_reasoning"] is False


@pytest.mark.asyncio
async def test_cache_and_reasoning_subdivisions_do_not_inflate_remote_totals(chains):
    data = _groq("oi", finish="stop")
    data["usage"] = {"prompt_tokens": 1000, "completion_tokens": 80, "total_tokens": 1080,
                     "prompt_tokens_details": {"cached_tokens": 700},
                     "completion_tokens_details": {"reasoning_tokens": 20}}
    report = {}
    instance = P.ProviderRouter(_Session(_Response(data)), groq_key="offline-key")
    await instance.chat(system="fixed prefix", messages=[], request_report=report)
    assert report["usage"] == {"input_tokens": 1000, "output_tokens": 80, "total_tokens": 1080,
                               "cached_tokens": 700, "reasoning_tokens": 20}
    assert report["usage_complete"] is True and report["usage_attempt_count"] == 1
    assert report["request_count"] == report["generation_attempt_count"] == 1
    assert report["usage_field_attempts"] == {name: 1 for name in report["usage"]}
    clone = instance.get_request_report()
    clone["usage"]["total_tokens"] = 1
    assert report["usage"]["total_tokens"] == 1080


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [True, -1, "12", 1.2, 1001, float("inf")])
async def test_invalid_cache_counts_are_omitted_not_zeroed(chains, detail):
    data = _groq("oi", finish="stop")
    data["usage"] = {"prompt_tokens": 1000, "completion_tokens": 80, "total_tokens": 1080,
                     "prompt_tokens_details": {"cached_tokens": detail}}
    report = {}
    await P.ProviderRouter(_Session(_Response(data)), groq_key="offline-key").chat(system="s", messages=[], request_report=report)
    assert "cached_tokens" not in report["usage"]


@pytest.mark.asyncio
async def test_gemini_thoughts_add_to_output_but_not_remote_total(chains):
    data = _gemini("oi")
    data["usageMetadata"] = {"promptTokenCount": 100, "candidatesTokenCount": 30,
                             "thoughtsTokenCount": 20, "totalTokenCount": 150, "cachedContentTokenCount": 70}
    report = {}
    await P.ProviderRouter(_Session(_Response(data)), gemini_key="offline-key").chat(system="s", messages=[], request_report=report)
    assert report["usage"] == {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20,
                               "total_tokens": 150, "cached_tokens": 70}


@pytest.mark.asyncio
async def test_missing_gemini_candidates_does_not_invent_output_from_thoughts(chains):
    data = _gemini("oi")
    data["usageMetadata"] = {"thoughtsTokenCount": 20}
    report = {}
    await P.ProviderRouter(_Session(_Response(data)), gemini_key="offline-key").chat(system="s", messages=[], request_report=report)
    assert report["usage"] == {"reasoning_tokens": 20} and report["usage_complete"] is False


@pytest.mark.asyncio
async def test_fallback_reports_coverage_without_fabricating_missing_usage(chains):
    invalid = _groq("", finish="stop")
    invalid["usage"] = {"prompt_tokens": 90}
    valid = _gemini("fallback")
    valid["usageMetadata"] = {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120}
    report = {}
    instance = P.ProviderRouter(_Session(_Response(invalid), _Response(valid)), groq_key="offline-key", gemini_key="offline-key")
    assert await instance.chat(system="s", messages=[], request_report=report) == "fallback"
    assert report["usage"] == {"input_tokens": 190, "output_tokens": 20, "total_tokens": 120}
    assert report["usage_field_attempts"] == {"input_tokens": 2, "output_tokens": 1, "total_tokens": 1}
    assert report["request_count"] == 2 and report["usage_complete"] is False


@pytest.mark.asyncio
async def test_concurrent_request_reports_keep_usage_and_context_separate(chains):
    started, release = asyncio.Event(), asyncio.Event()
    class Body:
        def __init__(self, data, delayed):
            self.data, self.delayed = json.dumps(data).encode(), delayed
        async def iter_chunked(self, size):
            if self.delayed:
                started.set()
                await release.wait()
            yield self.data
    class Session:
        def post(self, url, **options):
            delayed = options["json"]["messages"][-1]["content"] == "first"
            data = _groq("first reply" if delayed else "second reply", finish="stop")
            data["usage"] = {"prompt_tokens": 10 if delayed else 20, "completion_tokens": 1, "total_tokens": 11 if delayed else 21}
            response = _Response(data)
            response.content = Body(data, delayed)
            return response
    instance = P.ProviderRouter(Session(), groq_key="offline-key")
    first_report, second_report = {}, {}
    async def turn(text, report):
        reply = await instance.chat(system="s", messages=[P.ChatMessage("user", text)], request_report=report)
        return reply, instance.get_request_report()
    first = asyncio.create_task(turn("first", first_report))
    await started.wait()
    second_reply, second_local = await turn("second", second_report)
    release.set()
    first_reply, first_local = await first
    assert (first_reply, second_reply) == ("first reply", "second reply")
    assert first_report == first_local and second_report == second_local
    assert first_report["usage"]["input_tokens"] == 10 and second_report["usage"]["input_tokens"] == 20
    assert first_report["request_count"] == second_report["request_count"] == 1


@pytest.mark.asyncio
async def test_catalog_http_requests_are_counted_separately(chains):
    class Session(_Session):
        def get(self, url, **options):
            self.requests.append((url, options))
            return _Response({"models": [{"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]}]})
    valid = _gemini("descoberto")
    valid["usageMetadata"] = {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12}
    session = Session(_Response({"error": {}}, 404), _Response(valid))
    report = {}
    assert await P.ProviderRouter(session, gemini_key="offline-key").chat(system="s", messages=[], request_report=report) == "descoberto"
    assert report["request_count"] == 3 and report["generation_attempt_count"] == 2
    assert report["discovery_request_count"] == 1 and report["usage_attempt_count"] == 1


@pytest.mark.asyncio
async def test_same_prefix_and_native_history_survive_economical_reasoning(chains):
    data = _groq("done", finish="stop")
    session = _Session(_Response(data))
    call = NativeToolCall("native-id", "read", {})
    history = [P.ChatMessage("assistant", "", tool_calls=(call,)),
               P.ChatMessage("tool", "{}", tool_call_id=call.id, name=call.name)]
    spec = ToolSpec("read", "Read.", {"type": "object", "properties": {}, "additionalProperties": False})
    instance = P.ProviderRouter(session, groq_key="offline-key")
    result = await instance.chat(system="exact stable prefix", messages=history, tool_specs=(spec,), allow_tool_calls=False)
    payload = session.requests[0][1]["json"]
    assert result.text == "done" and payload["messages"][0]["content"] == "exact stable prefix"
    assert payload["messages"][1:] == [message.to_openai_payload() for message in history]
    assert payload["tool_choice"] == "none" and payload["max_completion_tokens"] == C.MAX_RESPONSE_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini", "cloudflare"])
@pytest.mark.parametrize("stage,expected", [("text", 500), ("read", 1000), ("actions", 2000), ("closing", 500)])
async def test_output_budget_follows_generation_stage_without_removing_declarations(provider, stage, expected):
    spec = ToolSpec("consultar_ferramentas", "Descobre ferramentas.",
                    {"type": "object", "properties": {}, "additionalProperties": False})
    action = proposal_tool(("join_voice", "send_audio"), ("autor",))
    action_spec = ToolSpec(action["name"], action["description"], action["parameters"])
    specs = () if stage == "text" else (spec, action_spec) if stage in {"actions", "closing"} else (spec,)
    data = (_gemini if provider == "gemini" else _groq)("pronto", finish="STOP" if provider == "gemini" else "stop")
    session = _Session(_Response(data))
    await provider_client(provider, session).chat(system="s", messages=[], temperature=.8,
          model=C.CLOUDFLARE_MODELS[0] if provider == "cloudflare" else "test-model", timeout_seconds=5,
          tool_specs=specs, allow_tool_calls=stage != "closing")
    payload = session.requests[0][1]["json"]
    assert output_cap(provider, payload) == expected
    if specs:
        assert [entry["name"] for entry in exported_declarations(provider, payload)] == [item.name for item in specs]
    if stage == "closing":
        assert (payload["toolConfig"]["functionCallingConfig"]["mode"] if provider == "gemini" else payload["tool_choice"]) in {"NONE", "none"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("stage,expected", [("vision", 1000), ("read", 1000), ("actions", 2000), ("closing", 1000)])
async def test_vision_and_final_response_budgets_remain_unchanged(provider, stage, expected):
    declaration = proposal_tool(("send_audio",), ("autor",))
    spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    read = ToolSpec("read", "Read.", {"type": "object", "properties": {}, "additionalProperties": False})
    specs = () if stage == "vision" else (read,) if stage == "read" else (spec,)
    data = (_gemini if provider == "gemini" else _groq)("imagem", finish="STOP" if provider == "gemini" else "stop")
    session = _Session(_Response(data))
    await provider_client(provider, session).chat(system="s", messages=[P.ChatMessage("user", "olhe", images=[PreparedImage("image/png", b"image bytes", "x.png")])],
        temperature=.8, model="test-model", timeout_seconds=5, tool_specs=specs, allow_tool_calls=stage != "closing")
    assert output_cap(provider, session.requests[0][1]["json"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini", "cloudflare"])
async def test_legacy_action_declarations_retain_2000_token_budget(provider):
    session = _Session(_Response((_gemini if provider == "gemini" else _groq)("oi")))
    await provider_client(provider, session).chat(system="s", messages=[], temperature=.8,
        model=C.CLOUDFLARE_MODELS[0] if provider == "cloudflare" else "test-model", timeout_seconds=5,
        actions=("send_audio", "join_voice"), target_refs=("autor",))
    assert output_cap(provider, session.requests[0][1]["json"]) == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini", "cloudflare"])
@pytest.mark.parametrize("truncated", [False, True])
async def test_four_proposal_sequence_keeps_full_contract_and_rejects_truncated_valid_prefix(provider, truncated):
    actions = ("join_voice", "send_audio", "timeout_member", "ban_member")
    declaration = proposal_tool(actions, ("autor", "m1", "m2"))
    spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    speech = ("Aí sim, agora tô na call também. Bora conversar, que eu queria te contar essa história. " * 10)[:800]
    arguments = [{"action": "join_voice"}, {"action": "send_audio", "text": speech},
                 {"action": "timeout_member", "target_ref": "m1", "reason": "spam relatado pelo usuário", "options": {"duration_seconds": 27}},
                 {"action": "ban_member", "target_ref": "m2", "reason": "spam relatado pelo usuário"}]
    finish = ("MAX_TOKENS" if truncated else "STOP") if provider == "gemini" else ("length" if truncated else "tool_calls")
    data = (_gemini if provider == "gemini" else _groq)(calls=[(TOOL_NAME, value) for value in arguments], finish=finish)
    session = _Session(_Response(data))
    options = dict(system="s", messages=[], temperature=.8,
                   model=C.CLOUDFLARE_MODELS[0] if provider == "cloudflare" else "test-model", timeout_seconds=5, tool_specs=(spec,))
    if truncated:
        with pytest.raises(P.ProviderError) as failure:
            await provider_client(provider, session).chat(**options)
        assert failure.value.diagnostic_code == "incomplete_calls"
    else:
        reply = await provider_client(provider, session).chat(**options)
        assert [item.action for item in reply.proposals] == list(actions)
        assert reply.proposals[1].text == speech.strip() and reply.text == "" and len(reply.tool_calls) == 4
        assert reply.tool_calls[1].arguments["text"] == speech
    payload = session.requests[0][1]["json"]
    assert output_cap(provider, payload) == 2000
    contract = exported_declarations(provider, payload)[0]
    assert contract["parameters"]["properties"]["action"]["enum"] == list(actions)
    assert speech not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini", "cloudflare"])
async def test_prepared_portuguese_response_preserves_2000_characters_with_tool_stage_cap(world, provider):
    from cogs.chatbot.tool_runtime import build_tool_registry
    registry = await build_tool_registry(world.cog, world.message, world.config, epoch=world.epoch,
                                         visibility_scope="channel:30")
    spec = registry.get("preparar_resposta")
    text = ("Tá tranquilo! Eu entendi o que você quis dizer; dá pra conversar assim, sem aquele jeito formal. " * 24)[:2000]
    assert len(text) == spec.parameters["properties"]["text"]["maxLength"] == 2000
    data = (_gemini if provider == "gemini" else _groq)(calls=[(spec.name, {"text": text})])
    session = _Session(_Response(data))
    reply = await provider_client(provider, session).chat(system="s", messages=[], temperature=.8,
        model=C.CLOUDFLARE_MODELS[0] if provider == "cloudflare" else "test-model", timeout_seconds=5, tool_specs=(spec,))
    assert reply.tool_calls[0].arguments["text"] == text and len(reply.tool_calls) == 1
    assert output_cap(provider, session.requests[0][1]["json"]) == 1000
