"""Offline repairs, private schema diagnostics and measured per-request usage."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from unittest.mock import Mock

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.action_protocol import TOOL_NAME, proposal_tool
from cogs.chatbot.tool_registry import ToolSpec
from cogs.chatbot.tool_runtime import build_tool_registry
from tests.test_chatbot_action_flow import world
from tests.test_chatbot_action_providers import _Response, _Session, _gemini, _groq


PRIVATE = "private spoken words never published"
UNKNOWN = "private_unknown_argument_name"
CALL_ID = "private_generated_call_identifier"
KEY = "offline-placeholder-key"


@pytest.fixture
def chains(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main",))
    monkeypatch.setattr(C, "GROQ_VISION_MODELS", ("groq-vision",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-main",))
    monkeypatch.setattr(C, "GEMINI_VISION_MODELS", ("gemini-vision",))


async def format_spec(w):
    # Read the operational schema the cog actually exports, rather than
    # reproducing the validator's implementation in a test-only schema.
    registry = await build_tool_registry(w.cog, w.message, w.config, epoch=w.epoch,
                                         visibility_scope="channel:30")
    return replace(registry.get("select_response_format"), handler=Mock())


def audio_spec():
    declaration = proposal_tool(("send_audio",), ("autor", "m1"))
    return ToolSpec(declaration["name"], declaration["description"], declaration["parameters"], handler=Mock())


def make_router(session, *, groq=True, gemini=False):
    return P.ProviderRouter(session, groq_key=KEY if groq else None,
                            gemini_key=KEY if gemini else None)


def synthetic_router(groq=None, gemini=None):
    instance = make_router(object(), groq=groq is not None, gemini=gemini is not None)
    instance._groq, instance._gemini = groq, gemini
    return instance


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0


class Client:
    def __init__(self, *results, clock=None, consume=None):
        self.results, self.requests = list(results), []
        self.clock, self.consume = clock, consume

    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        if self.clock and self.consume:
            self.clock.now += self.consume(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def invalid_error(spec, *, code="enum", path="$/mode", finish="tool_calls", **kwargs):
    return P.ProviderError(PRIVATE, kind="invalid_response", stage="output", finish_reason=finish,
                           diagnostic_code=code, diagnostic_path=path, diagnostic_tool=spec.name,
                           diagnostic_index=0, **kwargs)


def groq_data(spec, arguments, *, text=PRIVATE, finish="tool_calls", usage=None):
    data = _groq(text, [(spec.name, arguments)], finish=finish)
    data["choices"][0]["message"]["tool_calls"][0]["id"] = CALL_ID
    if usage is not None:
        data["usage"] = usage
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("arguments,code,path", [
    ({"mode": PRIVATE}, "enum", "$/mode"),
    ({}, "required", "$/mode"),
    ({"mode": True}, "type", "$/mode"),
    ({"mode": "text", UNKNOWN: PRIVATE}, "additional_properties", "$"),
])
async def test_native_errors_expose_only_schema_diagnostics(world, provider, arguments, code, path, caplog):
    spec = await format_spec(world)
    data = groq_data(spec, arguments) if provider == "groq" else _gemini(PRIVATE, [(spec.name, arguments)])
    session = _Session(_Response(data))
    client = (P._GroqClient if provider == "groq" else P._GeminiClient)(session, KEY)
    with pytest.raises(P.ProviderError) as failure:
        await client.chat(system="private system", messages=[], temperature=.8, model="test-model",
                          timeout_seconds=5, tool_specs=(spec,))
    error = failure.value
    assert (error.kind, error.stage) == ("invalid_response", "output")
    assert (error.diagnostic_code, error.diagnostic_path) == (code, path)
    assert error.diagnostic_tool == spec.name and error.diagnostic_index == 0
    assert PRIVATE not in str(error) and UNKNOWN not in str(error) and CALL_ID not in str(error)
    assert all(secret not in caplog.text for secret in (PRIVATE, UNKNOWN, CALL_ID, KEY))
    spec.handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_repair_reuses_original_history_with_safe_feedback_and_no_effects(world, chains, provider, caplog):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    spec = await format_spec(world)
    audio = audio_spec()
    invalid = (groq_data(spec, {"mode": PRIVATE, UNKNOWN: PRIVATE}) if provider == "groq"
               else _gemini(PRIVATE, [(spec.name, {"mode": PRIVATE, UNKNOWN: PRIVATE})]))
    valid = (_groq if provider == "groq" else _gemini)(PRIVATE, [(TOOL_NAME, {"action": "send_audio", "text": PRIVATE})])
    session = _Session(_Response(invalid), _Response(valid))
    instance = make_router(session, groq=provider == "groq", gemini=provider == "gemini")
    history = [P.ChatMessage("user", "manda áudio pra mim"), P.ChatMessage("assistant", "uma resposta anterior")]
    reply = await instance.chat(system="original system", messages=history, tool_specs=(spec, audio))
    assert reply.text == "" and reply.proposals[0].text == PRIVATE
    assert len(session.requests) == 2
    first, repaired = [request[1]["json"] for request in session.requests]
    if provider == "groq":
        assert first["model"] == repaired["model"] == "groq-main"
        assert first["messages"][1:] == repaired["messages"][1:]
        feedback = repaired["messages"][0]["content"]
    else:
        assert session.requests[0][0] == session.requests[1][0]
        assert first["contents"] == repaired["contents"]
        feedback = repaired["systemInstruction"]["parts"][0]["text"]
    assert feedback.startswith("original system\n\n") and "Diagnóstico seguro:" in feedback
    assert '"code": "additional_properties"' in feedback and f'"tool": "{spec.name}"' in feedback
    assert "O lote anterior deste pedido foi rejeitado. Nenhuma ferramenta desse lote foi executada." in feedback
    assert all(secret not in feedback for secret in (PRIVATE, UNKNOWN, CALL_ID, KEY))
    assert all(secret not in caplog.text for secret in (PRIVATE, UNKNOWN, CALL_ID, KEY))
    report = instance.diagnostics()["last_request"]
    assert [item["repair"] for item in report["attempts"]] == [False, True]
    assert report["repair_used"] is True and report["attempt_count"] == 2
    assert instance.snapshot()[f"{provider}/{provider}-main"]["available"]
    assert history[0].content == "manda áudio pra mim" and len(history) == 2
    spec.handler.assert_not_called()
    audio.handler.assert_not_called()


@pytest.mark.asyncio
async def test_only_one_repair_across_models_then_healthy_fallback(world, chains, monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main", "groq-small"))
    spec = await format_spec(world)
    groq = Client(invalid_error(spec), invalid_error(spec), invalid_error(spec))
    gemini = Client("fallback works")
    instance = synthetic_router(groq, gemini)
    reply = await instance.chat(system="s", messages=[P.ChatMessage("user", "original")], tool_specs=(spec,))
    assert reply.text == "fallback works"
    assert [request["model"] for request in groq.requests] == ["groq-main", "groq-main", "groq-small"]
    assert [item["repair"] for item in instance.diagnostics()["last_request"]["attempts"]] == [False, True, False, False]
    assert all(instance.snapshot()[f"groq/{model}"]["available"] for model in C.GROQ_MODELS)
    spec.handler.assert_not_called()


@pytest.mark.asyncio
async def test_shared_turn_repair_state_allows_only_one_repair_across_router_calls(world, chains):
    spec = await format_spec(world)
    repaired = _groq("first round ready", finish="stop")
    session = _Session(
        _Response(groq_data(spec, {"mode": PRIVATE})), _Response(repaired),
        _Response(groq_data(spec, {"mode": PRIVATE})), _Response(_gemini("second round fallback")),
    )
    instance = make_router(session, gemini=True)
    repair_state = {}
    history = [P.ChatMessage("user", "original request")]
    first = await instance.chat(system="original system", messages=history, tool_specs=(spec,), repair_state=repair_state)
    assert first.text == "first round ready" and repair_state == {"used": True}
    first_report = instance.diagnostics()["last_request"]
    assert [attempt["repair"] for attempt in first_report["attempts"]] == [False, True]

    second = await instance.chat(system="original system", messages=history, tool_specs=(spec,), repair_state=repair_state)
    assert second.text == "second round fallback" and repair_state == {"used": True}
    second_report = instance.diagnostics()["last_request"]
    assert [attempt["repair"] for attempt in second_report["attempts"]] == [False, False]
    assert [attempt["provider"] for attempt in second_report["attempts"]] == ["groq", "gemini"]
    assert len(session.requests) == 4
    initial, repair, next_round = [request[1]["json"] for request in session.requests[:3]]
    assert initial["messages"][1:] == repair["messages"][1:] == next_round["messages"][1:]
    assert next_round["messages"][0]["content"] == "original system"
    assert "Diagnóstico seguro:" in repair["messages"][0]["content"]
    assert all(secret not in repair["messages"][0]["content"] for secret in (PRIVATE, UNKNOWN, CALL_ID, KEY))
    spec.handler.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_tools", "tools_disabled", "no_code", "incomplete", "unexpected", "malformed", "truncated"])
async def test_repair_is_not_used_for_nonrepairable_or_final_attempts(world, chains, case):
    spec = await format_spec(world)
    code = {"no_code": "", "incomplete": "incomplete_calls", "unexpected": "unexpected_calls", "malformed": "malformed_response"}.get(case, "enum")
    finish = "MAX_TOKENS" if case == "truncated" else "tool_calls"
    groq = Client(invalid_error(spec, code=code, finish=finish))
    gemini = Client("fallback works")
    instance = synthetic_router(groq, gemini)
    kwargs = {"tool_specs": (spec,)} if case != "no_tools" else {}
    if case == "tools_disabled":
        kwargs["allow_tool_calls"] = False
    reply = await instance.chat(system="s", messages=[], **kwargs)
    assert (reply.text if isinstance(reply, P.ChatReply) else reply) == "fallback works"
    assert len(groq.requests) == len(gemini.requests) == 1
    assert all(not attempt["repair"] for attempt in instance.diagnostics()["last_request"]["attempts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [38.0, 1.0, .4])
async def test_repair_shares_provider_budget_and_preserves_fallback_time(world, chains, monkeypatch, budget):
    spec = await format_spec(world)
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    groq = Client(invalid_error(spec), invalid_error(spec), clock=clock,
                  consume=lambda kwargs: kwargs["timeout_seconds"] * .6)
    gemini = Client("healthy fallback")
    instance = synthetic_router(groq, gemini)
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,), budget_seconds=budget)).text == "healthy fallback"
    first, repair = groq.requests
    assert repair["timeout_seconds"] < first["timeout_seconds"]
    assert gemini.requests[0]["timeout_seconds"] > 0
    assert clock.now <= 100 + budget - min(10, budget * .5)
    assert len(groq.requests) == 2 and len(gemini.requests) == 1


@pytest.mark.asyncio
async def test_repair_429_stops_repair_without_replaying_then_uses_gemini(world, chains):
    spec = await format_spec(world)
    groq = Client(invalid_error(spec), P.RateLimitError(PRIVATE, retry_after=30))
    gemini = Client("fallback works")
    instance = synthetic_router(groq, gemini)
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,))).text == "fallback works"
    assert len(groq.requests) == 2 and len(gemini.requests) == 1
    assert instance.snapshot()["groq/groq-main"]["last_kind"] == "rate_limit"
    assert instance.diagnostics()["last_request"]["attempts"][1]["repair"] is True
    spec.handler.assert_not_called()


@pytest.mark.asyncio
async def test_repair_sanitizes_diagnostics_against_host_schema_before_feedback(world, chains, caplog):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    spec = await format_spec(world)
    error = P.ProviderError(PRIVATE, kind="invalid_response", stage="output", diagnostic_code="enum",
                            diagnostic_path="$/" + UNKNOWN, diagnostic_tool=UNKNOWN, diagnostic_index=1)
    groq = Client(error, "repaired")
    instance = synthetic_router(groq)
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,))).text == "repaired"
    feedback = groq.requests[1]["system"]
    assert '"path": "$"' in feedback and '"tool": ""' in feedback
    assert all(secret not in feedback and secret not in caplog.text for secret in (PRIVATE, UNKNOWN, CALL_ID, KEY))
    diagnostic = instance.diagnostics()["last_request"]["attempts"][0]
    assert diagnostic["diagnostic_tool"] == "" and diagnostic["diagnostic_path"] == "$"


@pytest.mark.asyncio
async def test_final_none_rejects_native_calls_then_falls_back_without_repair(world, chains):
    spec = await format_spec(world)
    session = _Session(_Response(groq_data(spec, {"mode": "audio"})), _Response(_gemini("ready")))
    instance = make_router(session, gemini=True)
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,), allow_tool_calls=False)).text == "ready"
    assert len(session.requests) == 2
    assert session.requests[0][1]["json"]["tool_choice"] == "none"
    assert session.requests[1][1]["json"]["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"
    report = instance.diagnostics()["last_request"]
    assert [item["repair"] for item in report["attempts"]] == [False, False]
    assert report["attempts"][0]["diagnostic_code"] == "unexpected_calls"
    spec.handler.assert_not_called()


@pytest.mark.asyncio
async def test_repair_is_not_started_after_provider_deadline(world, chains, monkeypatch):
    spec = await format_spec(world)
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    groq = Client(invalid_error(spec), clock=clock, consume=lambda kwargs: kwargs["timeout_seconds"])
    gemini = Client("healthy fallback")
    instance = synthetic_router(groq, gemini)
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,), budget_seconds=1)).text == "healthy fallback"
    assert len(groq.requests) == 1 and not instance.diagnostics()["last_request"].get("repair_used", False)


@pytest.mark.asyncio
async def test_invalid_output_and_repair_usage_are_measured_and_aggregated(world, chains, caplog):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    spec = await format_spec(world)
    first = groq_data(spec, {"mode": PRIVATE}, usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
    second = _groq("ready", finish="stop")
    second["usage"] = {"prompt_tokens": 13, "completion_tokens": 3, "total_tokens": 16}
    instance = make_router(_Session(_Response(first), _Response(second)))
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,))).text == "ready"
    report = instance.diagnostics()["last_request"]
    assert report["attempts"][0]["kind"] == "invalid_response"
    assert report["attempts"][0]["usage"] == {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    assert report["attempts"][1]["usage"] == {"input_tokens": 13, "output_tokens": 3, "total_tokens": 16}
    assert report["usage"] == {"input_tokens": 23, "output_tokens": 5, "total_tokens": 28}
    assert report["usage_attempt_count"] == 2
    assert PRIVATE not in caplog.text and KEY not in caplog.text


@pytest.mark.asyncio
async def test_groq_error_usage_and_gemini_usage_survive_fallback(world, chains):
    spec = await format_spec(world)
    invalid = groq_data(spec, {"mode": PRIVATE}, usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
    invalid_repair = groq_data(spec, {"mode": PRIVATE}, usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
    valid = _gemini("fallback")
    valid["usageMetadata"] = {"promptTokenCount": 20, "candidatesTokenCount": 4, "totalTokenCount": 24,
                              "thoughtsTokenCount": 6, "cachedContentTokenCount": 7}
    instance = make_router(_Session(_Response(invalid), _Response(invalid_repair), _Response(valid)), gemini=True)
    assert (await instance.chat(system="s", messages=[], tool_specs=(spec,))).text == "fallback"
    report = instance.diagnostics()["last_request"]
    # Gemini candidates exclude thoughts, whereas Groq completion includes it.
    # Keep the remote total unchanged, even for this deliberately inconsistent fixture.
    assert report["usage"] == {"input_tokens": 41, "output_tokens": 15, "total_tokens": 50,
                               "reasoning_tokens": 6, "cached_tokens": 7}
    assert report["usage_attempt_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_usage_accepts_only_api_integer_counts_and_never_invents_absent_fields(chains, provider):
    data = (_groq if provider == "groq" else _gemini)("ready", finish="stop" if provider == "groq" else "STOP")
    data["usage" if provider == "groq" else "usageMetadata"] = (
        {"prompt_tokens": 9, "completion_tokens": True, "total_tokens": "10", UNKNOWN: PRIVATE}
        if provider == "groq" else {"promptTokenCount": 9, "candidatesTokenCount": -1, "totalTokenCount": 1.5,
                                   "thoughtsTokenCount": float("inf"), "cachedContentTokenCount": 10**15, UNKNOWN: PRIVATE})
    instance = make_router(_Session(_Response(data)), groq=provider == "groq", gemini=provider == "gemini")
    assert await instance.chat(system="s", messages=[]) == "ready"
    report = instance.diagnostics()["last_request"]
    assert report["usage"] == report["attempts"][0]["usage"] == {"input_tokens": 9}
    assert report["usage_attempt_count"] == 1
    assert "output_tokens" not in report["usage"] and UNKNOWN not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_absent_usage_is_empty_and_not_a_claim_of_zero_consumption(chains, provider):
    data = (_groq if provider == "groq" else _gemini)("ready", finish="stop" if provider == "groq" else "STOP")
    instance = make_router(_Session(_Response(data)), groq=provider == "groq", gemini=provider == "gemini")
    assert await instance.chat(system="s", messages=[]) == "ready"
    report = instance.diagnostics()["last_request"]
    assert report["usage"] == {} and report["attempts"][0]["usage"] == {}
    assert report["usage_attempt_count"] == 0


@pytest.mark.asyncio
async def test_usage_attached_to_provider_error_is_kept_even_without_response_body(chains):
    groq = Client(P.ProviderError(PRIVATE, kind="network", usage={"input_tokens": 17, "output_tokens": True}))
    gemini = Client("fallback")
    instance = synthetic_router(groq, gemini)
    assert await instance.chat(system="s", messages=[]) == "fallback"
    report = instance.diagnostics()["last_request"]
    assert report["attempts"][0]["usage"] == report["usage"] == {"input_tokens": 17}
    assert report["usage_attempt_count"] == 1


@pytest.mark.asyncio
async def test_usage_on_exhaustion_is_reported_without_claiming_unmeasured_counts(chains):
    groq = Client(P.ProviderError(PRIVATE, kind="network", usage={"input_tokens": 17}))
    gemini = Client(P.ProviderError(PRIVATE, kind="network", usage={"input_tokens": 23, "output_tokens": 0}))
    instance = synthetic_router(groq, gemini)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[])
    assert failure.value.usage == {"input_tokens": 40, "output_tokens": 0}
    report = instance.diagnostics()["last_request"]
    assert report["usage"] == failure.value.usage and report["usage_attempt_count"] == 2
    assert "total_tokens" not in report["usage"]


@pytest.mark.asyncio
async def test_concurrent_http_requests_keep_context_usage_separate(chains):
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedBody:
        def __init__(self, data, delay):
            self.data, self.delay = json.dumps(data).encode(), delay

        async def iter_chunked(self, size):
            if self.delay:
                started.set()
                await release.wait()
            yield self.data

    class Session:
        def post(self, url, **kwargs):
            first = kwargs["json"]["messages"][-1]["content"] == "first"
            data = _groq("first result" if first else "second result", finish="stop")
            data["usage"] = {"prompt_tokens": 11 if first else 22, "completion_tokens": 1 if first else 2}
            response = _Response(data)
            response.content = DelayedBody(data, first)
            return response

    instance = make_router(Session())
    first = asyncio.create_task(instance.chat(system="s", messages=[P.ChatMessage("user", "first")]))
    await started.wait()
    assert await instance.chat(system="s", messages=[P.ChatMessage("user", "second")]) == "second result"
    second_report = instance.diagnostics()["last_request"]
    release.set()
    assert await first == "first result"
    # The externally visible last-request snapshot belongs to the latest
    # request, even if an earlier HTTP operation finishes afterwards.
    report = instance.diagnostics()["last_request"]
    assert second_report["usage"] == report["usage"] == {"input_tokens": 22, "output_tokens": 2}
    assert second_report["attempts"][0]["usage"] == {"input_tokens": 22, "output_tokens": 2}


@pytest.mark.asyncio
async def test_mixed_exhaustion_retains_native_failure_and_all_safe_causes(world, chains, monkeypatch):
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-main", "gemini-small"))
    spec = await format_spec(world)
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    groq = Client(invalid_error(spec), invalid_error(spec))
    gemini = Client(P.RateLimitError(PRIVATE, retry_after=22 * 3600), P.ProviderError(PRIVATE, kind="model", status=404))
    instance = synthetic_router(groq, gemini)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="private system", messages=[], tool_specs=(spec,))
    error = failure.value
    assert error.kind == "invalid_response" and error.stage == "output"
    assert error.diagnostic_code == "enum" and error.diagnostic_path == "$/mode"
    assert error.diagnostic_tool == spec.name and error.diagnostic_index == 0
    assert [cause["kind"] for cause in error.causes] == ["invalid_response", "invalid_response", "rate_limit", "model"]
    assert error.causes[2]["retry_after"] == 22 * 3600
    # Native validation did not open Groq's circuit, so a fresh local retry
    # remains eligible. Gemini's long quota must not become a global wait.
    assert error.earliest_retry_seconds == 0.0
    assert all(secret not in json.dumps(error.causes) for secret in (PRIVATE, KEY, UNKNOWN, CALL_ID))
    assert instance.diagnostics()["last_request"]["kind"] == "invalid_response"
    assert instance.diagnostics()["earliest_retry_seconds"] == 0.0
    spec.handler.assert_not_called()


@pytest.mark.asyncio
async def test_quota_then_404_uses_quota_wait_instead_of_model_config_error(chains, monkeypatch):
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-main", "gemini-small"))
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    gemini = Client(P.RateLimitError(PRIVATE, retry_after=22 * 3600), P.ProviderError(PRIVATE, kind="model", status=404))
    instance = synthetic_router(gemini=gemini)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[])
    assert failure.value.kind == "rate_limit" and failure.value.status == 429
    assert failure.value.retry_after == failure.value.earliest_retry_seconds == 22 * 3600
    assert [cause["kind"] for cause in failure.value.causes] == ["rate_limit", "model"]
    assert instance.diagnostics()["last_request"]["retry_after"] == 22 * 3600


@pytest.mark.asyncio
async def test_auth_wait_is_excluded_from_viable_quota_retry(chains, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    groq = Client(P.RateLimitError(PRIVATE, retry_after=22 * 3600))
    gemini = Client(P.ProviderError(PRIVATE, kind="auth", status=401))
    instance = synthetic_router(groq, gemini)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[])
    assert failure.value.kind == "rate_limit"
    assert failure.value.retry_after == failure.value.earliest_retry_seconds == 22 * 3600
    assert [cause["kind"] for cause in failure.value.causes] == ["rate_limit", "auth"]
    assert instance.snapshot()["gemini/gemini-main"]["cooldown_seconds"] == 900
    assert instance.diagnostics()["earliest_retry_seconds"] == 22 * 3600
    with pytest.raises(P.AllProvidersExhausted) as cooldown:
        await instance.chat(system="s", messages=[])
    assert cooldown.value.kind == "cooldown" and cooldown.value.cause_kind == "rate_limit"
    assert cooldown.value.retry_after == 22 * 3600
    assert len(groq.requests) == len(gemini.requests) == 1
