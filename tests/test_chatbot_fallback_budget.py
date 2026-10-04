"""Offline quota, deadlines and native-history regressions; no API credentials."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.action_protocol import NativeToolCall, TOOL_NAME, proposal_tool
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Response, _Session, _gemini, _groq


class Clock:
    def __init__(self):
        self.now = 100.0
        self.wall = 1_700_000_000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall


class Client:
    def __init__(self, *results, clock=None, consume_budget=False):
        self.results = list(results)
        self.requests = []
        self.clock = clock
        self.consume_budget = consume_budget

    async def chat(self, **kwargs):
        self.requests.append(kwargs)
        if self.consume_budget:
            self.clock.now += kwargs["timeout_seconds"]
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def chains(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main", "groq-small"))
    monkeypatch.setattr(C, "GROQ_VISION_MODELS", ("groq-vision",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-main", "gemini-small"))
    monkeypatch.setattr(C, "GEMINI_VISION_MODELS", ("gemini-vision",))


def router(groq=None, gemini=None):
    instance = P.ProviderRouter(object(), groq_key="test-placeholder" if groq else None,
                                gemini_key="test-placeholder" if gemini else None)
    instance._groq, instance._gemini = groq, gemini
    return instance


@pytest.mark.asyncio
async def test_unknown_429_is_model_scoped_and_healthy_groq_sibling_wins(chains):
    groq, gemini = Client(P.RateLimitError("private quota body", retry_after=19), "sibling works"), Client("unused")
    instance = router(groq, gemini)
    assert await instance.chat(system="private instructions", messages=[]) == "sibling works"
    assert [item["model"] for item in groq.requests] == ["groq-main", "groq-small"]
    assert not gemini.requests
    assert instance.snapshot()["groq/groq-main"]["last_kind"] == "rate_limit"
    assert instance.snapshot()["groq/groq-small"]["available"]
    assert "groq/groq-vision" not in instance.snapshot()


@pytest.mark.asyncio
async def test_unknown_429_keeps_gemini_lite_eligible(chains):
    groq = Client(P.RateLimitError("private", retry_after=10), P.RateLimitError("private", retry_after=10))
    gemini = Client(P.RateLimitError("private", retry_after=20), "Gemini sibling works")
    instance = router(groq, gemini)
    assert await instance.chat(system="s", messages=[]) == "Gemini sibling works"
    assert [item["model"] for item in gemini.requests] == ["gemini-main", "gemini-small"]
    assert instance.snapshot()["gemini/gemini-main"]["last_status"] == 429
    assert instance.snapshot()["gemini/gemini-small"]["available"]


@pytest.mark.asyncio
async def test_proven_account_quota_skips_siblings_and_all_modes(chains):
    groq = Client(P.RateLimitError("private account response", retry_after=123, quota_scope="account"))
    gemini = Client("fallback works")
    instance = router(groq, gemini)
    assert await instance.chat(system="s", messages=[]) == "fallback works"
    assert [item["model"] for item in groq.requests] == ["groq-main"]
    states = instance.snapshot()
    assert all(not states[f"groq/{name}"]["available"] for name in ("groq-main", "groq-small", "groq-vision"))
    assert all(states[f"groq/{name}"]["quota_scope"] == "account" for name in ("groq-main", "groq-small", "groq-vision"))


@pytest.mark.asyncio
@pytest.mark.parametrize("error,wait", [
    (P.RateLimitError("private quota", retry_after=30, quota_scope="account"), 30),
    (P.ProviderError("private auth", kind="auth", status=401), 900),
    (P.ProviderError("private network", kind="network", status=503), 20),
])
async def test_older_concurrent_success_cannot_clear_newer_global_failure(chains, monkeypatch, error, wait):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    started, release = asyncio.Event(), asyncio.Event()

    class ConcurrentClient:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                started.set()
                await release.wait()
                return "older valid response"
            if len(self.requests) == 2:
                raise error
            return "fresh response after cooldown"

    client = ConcurrentClient()
    instance = router(client)
    older = asyncio.create_task(instance.chat(system="s", messages=[]))
    await started.wait()
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[])
    assert failure.value.kind == error.kind
    assert not instance.snapshot()["groq/groq-main"]["available"]
    release.set()
    assert await older == "older valid response"
    state = instance.snapshot()["groq/groq-main"]
    assert not state["available"]
    assert state["cooldown_seconds"] == wait and state["last_status"] == error.status
    clock.now += wait
    assert await instance.chat(system="s", messages=[]) == "fresh response after cooldown"
    state = instance.snapshot()["groq/groq-main"]
    assert state["available"] and state["failures"] == 0 and state["last_status"] == 0
    assert instance._state("groq", "groq-main").failure_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("newer,older,wait,kind", [
    (P.RateLimitError("newer account quota", retry_after=30, quota_scope="account"),
     P.RateLimitError("older model quota", retry_after=5), 30, "rate_limit"),
    (P.ProviderError("newer auth failure", kind="auth", status=401),
     P.ProviderError("older network failure", kind="network", status=503), 900, "auth"),
])
async def test_older_concurrent_failure_cannot_shorten_or_downgrade_newer_circuit(chains, monkeypatch, newer, older, wait, kind):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    started, release = asyncio.Event(), asyncio.Event()

    class ConcurrentClient:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                started.set()
                await release.wait()
                raise older
            raise newer

    client = ConcurrentClient()
    instance = router(client)
    earlier_task = asyncio.create_task(instance.chat(system="s", messages=[]))
    await started.wait()
    with pytest.raises(P.AllProvidersExhausted):
        await instance.chat(system="s", messages=[])
    before = instance.snapshot()
    release.set()
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await earlier_task
    after = instance.snapshot()
    assert after == before
    assert after["groq/groq-main"]["cooldown_seconds"] == wait
    assert after["groq/groq-main"]["last_kind"] == kind
    assert after["groq/groq-main"]["last_status"] == newer.status
    assert failure.value.kind == kind and failure.value.status == newer.status
    assert failure.value.quota_scope == newer.quota_scope
    if kind == "rate_limit":
        assert after["groq/groq-main"]["quota_scope"] == "account"
        assert failure.value.retry_after == 30
    assert all(instance._state("groq", name).failure_generation == 1 for name in C.GROQ_MODELS)
    assert len(client.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [38.0, 12.0, 2.0, .2])
async def test_two_slow_groq_outputs_cannot_starve_gemini(chains, monkeypatch, budget):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    groq = Client(P.ProviderError("private invalid", kind="invalid_response", stage="output"),
                  P.ProviderError("private invalid", kind="invalid_response", stage="output"),
                  clock=clock, consume_budget=True)
    gemini = Client("fallback works")
    instance = router(groq, gemini)
    assert await instance.chat(system="s", messages=[], budget_seconds=budget) == "fallback works"
    assert [item["model"] for item in groq.requests] == ["groq-main", "groq-small"]
    assert len(gemini.requests) == 1
    assert all(0 < item["timeout_seconds"] <= 25 for item in [*groq.requests, *gemini.requests])
    remaining = budget - (clock.now - 100)
    assert remaining >= min(10, budget * .5) - 1e-8
    assert 0 < gemini.requests[0]["timeout_seconds"] <= remaining
    assert clock.now <= 100 + budget


@pytest.mark.asyncio
async def test_no_reserve_for_unconfigured_fallback(chains, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main",))
    groq = Client("works")
    assert await router(groq).chat(system="s", messages=[], budget_seconds=12) == "works"
    assert groq.requests[0]["timeout_seconds"] == 12


@pytest.mark.asyncio
async def test_real_attempt_timeout_cancels_request_then_tries_gemini(chains, monkeypatch):
    monkeypatch.setattr(C, "PROVIDER_ROUTER_TIMEOUT_SECONDS", .12)
    cancelled = asyncio.Event()

    class SlowClient:
        requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    groq, gemini = SlowClient(), Client("fallback works")
    instance = router(groq, gemini)
    assert await instance.chat(system="s", messages=[]) == "fallback works"
    assert cancelled.is_set()
    assert len(groq.requests) == len(gemini.requests) == 1
    assert instance.snapshot()["groq/groq-main"]["last_kind"] == "timeout"


@pytest.mark.asyncio
async def test_zero_budget_does_not_start_any_request(chains):
    groq, gemini = Client("unused"), Client("unused")
    instance = router(groq, gemini)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await instance.chat(system="s", messages=[], budget_seconds=0)
    assert failure.value.kind == "deadline"
    assert not groq.requests and not gemini.requests
    assert instance.diagnostics()["last_request"]["attempt_count"] == 0


@pytest.mark.asyncio
async def test_all_cooldown_preserves_cause_retry_and_safe_diagnostics(chains, monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    groq = Client(P.RateLimitError("private HTTP body", retry_after=12), P.RateLimitError("private HTTP body", retry_after=15))
    gemini = Client(P.RateLimitError("private HTTP body", retry_after=23), P.RateLimitError("private HTTP body", retry_after=30))
    instance = router(groq, gemini)
    with pytest.raises(P.AllProvidersExhausted) as first:
        await instance.chat(system="private prompt", messages=[P.ChatMessage("user", "private message")])
    assert first.value.kind == first.value.cause_kind == "rate_limit"
    assert first.value.retry_after == 12
    with pytest.raises(P.AllProvidersExhausted) as second:
        await instance.chat(system="private prompt", messages=[])
    assert second.value.kind == "cooldown"
    assert second.value.cause_kind == "rate_limit" and second.value.status == 429
    assert second.value.retry_after == 12
    diagnostics = instance.diagnostics()
    assert diagnostics["configured"] == {"groq": True, "gemini": True, "mistral": False, "cloudflare": False}
    assert diagnostics["earliest_retry_seconds"] == 12
    assert diagnostics["last_request"]["attempt_count"] == 0
    assert len(diagnostics["last_request"]["skips"]) == 4
    assert all(item["reason"] == "cooldown" for item in diagnostics["last_request"]["skips"])
    assert len(groq.requests) == len(gemini.requests) == 2
    assert "private" not in caplog.text and "test-placeholder" not in caplog.text
    assert "private" not in json.dumps(diagnostics) and "test-placeholder" not in json.dumps(diagnostics)


def test_explicit_retry_after_is_not_multiplied_and_is_bounded(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    state = P._ProviderState()
    for _ in range(3):
        state.mark_failure(123, kind="rate_limit", status=429, explicit_retry=True)
        assert state.next_allowed_monotonic == 223
    assert P.RateLimitError("x", retry_after=float("nan")).retry_after is None
    assert P.RateLimitError("x", retry_after=float("inf")).retry_after is None
    assert P.RateLimitError("x", retry_after=-1).retry_after is None
    assert P.RateLimitError("x", retry_after=10**20).retry_after == 86400


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,expected", [("123s", 123), ("1.250s", 1.25), ({"seconds": "12", "nanos": 500000000}, 12.5)])
async def test_google_retry_info_and_model_dimensions_are_read(duration, expected):
    response = _Response({"error": {"message": "private account details", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": duration},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generate_content_free_tier_requests",
            "quotaId": "GenerateContentRequestsPerDayPerProjectPerModel",
            "quotaDimensions": {"project": "private-project", "model": "private-model"},
        }]},
    ]}}, status=429)
    failure = await P._http_error(response)
    assert isinstance(failure, P.RateLimitError)
    assert failure.retry_after == expected and failure.quota_scope == "model"
    assert "private" not in str(failure)


@pytest.mark.asyncio
async def test_google_account_quota_requires_structured_evidence():
    failure = await P._http_error(_Response({"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateContentRequestsPerMinutePerProject", "quotaDimensions": {"project": "private-project"},
        }],
    }]}}, status=429))
    assert failure.quota_scope == "account"
    ambiguous = await P._http_error(_Response({"error": {
        "message": "rate limit for private project and organization", "code": "insufficient_quota",
    }}, status=429))
    assert ambiguous.quota_scope == "model"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_mixed_quota_failure_keeps_independent_account_evidence(reverse):
    violations = [
        {"quotaId": "RequestsPerMinutePerProject", "quotaDimensions": {"project": "private-project"}},
        {"quotaId": "TokensPerMinutePerProjectPerModel", "quotaDimensions": {"project": "private-project", "model": "private-model"}},
    ]
    failure = await P._http_error(_Response({"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": list(reversed(violations)) if reverse else violations,
    }]}}, status=429))
    assert failure.quota_scope == "account"


@pytest.mark.asyncio
async def test_retry_after_http_date_and_header_body_hints_are_combined(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(P, "time", clock)
    response = _Response({"error": {"message": "Please try again in 1m30.5s."}}, status=429)
    response.headers = {
        "Retry-After": format_datetime(datetime.fromtimestamp(clock.wall + 60, tz=timezone.utc), usegmt=True),
        "x-ratelimit-reset-requests": "45s",
    }
    assert P._retry_after(response) == 60
    assert (await P._http_error(response)).retry_after == 90.5


@pytest.mark.asyncio
async def test_healthy_bucket_reset_does_not_override_explicit_retry_after():
    response = _Response({"error": {"message": "Tokens per minute exceeded. Please try again in 9s."}}, status=429)
    response.headers = {"Retry-After": "9", "x-ratelimit-remaining-requests": "999",
                        "x-ratelimit-remaining-tokens": "0",
                        "x-ratelimit-reset-requests": "1h", "x-ratelimit-reset-tokens": "9s"}
    assert (await P._http_error(response)).retry_after == 9


@pytest.mark.asyncio
async def test_reset_only_uses_exhausted_bucket_when_explicit_retry_is_missing():
    response = _Response({"error": {"message": "rate limit exceeded"}}, status=429)
    response.headers = {"x-ratelimit-remaining-requests": "999", "x-ratelimit-remaining-tokens": "0",
                        "x-ratelimit-reset-requests": "1h", "x-ratelimit-reset-tokens": "9s"}
    assert (await P._http_error(response)).retry_after == 9
    ambiguous = _Response({"error": {"message": "rate limit exceeded"}}, status=429)
    ambiguous.headers = {"x-ratelimit-reset-requests": "1h", "x-ratelimit-reset-tokens": "9s"}
    assert (await P._http_error(ambiguous)).retry_after is None


@pytest.mark.asyncio
@pytest.mark.parametrize("header,duration", [("NaN", "NaNs"), ("Infinity", "infs"), ("-2", "-3s"), ("wrong", {}), ("999999999999999999999999", "999999999999999999999999s")])
async def test_invalid_retry_hints_never_escape_safe_bounds(header, duration):
    response = _Response({"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": duration,
    }]}}, status=429)
    response.headers["Retry-After"] = header
    failure = await P._http_error(response)
    assert failure.retry_after is None or 1 <= failure.retry_after <= 86400


@pytest.mark.asyncio
async def test_google_http400_invalid_key_is_auth_not_payload_error(chains):
    response = _Response({"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "private message",
                                  "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}, status=400)
    failure = await P._http_error(response)
    assert failure.kind == "auth" and failure.status == 400
    gemini = Client(failure)
    groq = Client("fallback works")
    instance = router(groq, gemini)
    assert await instance.chat(system="s", messages=[], text_provider_order=("gemini", "groq")) == "fallback works"
    assert len(gemini.requests) == 1
    assert instance.snapshot()["gemini/gemini-small"]["last_kind"] == "auth"
    assert instance.snapshot()["gemini/gemini-vision"]["last_status"] == 400


@pytest.mark.asyncio
async def test_auth_error_400_is_preserved_in_next_cooldown(chains):
    instance = router(gemini=Client(P.ProviderError("private key error", kind="auth", status=400)))
    with pytest.raises(P.AllProvidersExhausted) as first:
        await instance.chat(system="s", messages=[])
    assert first.value.kind == "auth"
    with pytest.raises(P.AllProvidersExhausted) as second:
        await instance.chat(system="s", messages=[])
    assert second.value.kind == "cooldown" and second.value.cause_kind == "auth"
    assert second.value.status == 400 and 0 < second.value.retry_after <= 900


READ = ToolSpec("get_conversation_preferences", "Read host-confirmed preferences.",
                {"type": "object", "properties": {}, "additionalProperties": False})


@pytest.mark.asyncio
async def test_groq_tool_history_survives_gemini_fallback_without_replaying_effects(chains):
    calls = (NativeToolCall("groq_real_id", READ.name, {}),)
    session = _Session(_Response(_gemini("Preferência confirmada.")))
    instance = router(Client(P.RateLimitError("private", quota_scope="account")), P._GeminiClient(session, "test-placeholder"))
    messages = [
        P.ChatMessage("user", "Como prefiro as respostas?"),
        P.ChatMessage("assistant", "", tool_calls=calls),
        P.ChatMessage("tool", '{"ok":true,"mode":"audio"}', tool_call_id="groq_real_id", name=READ.name),
    ]
    reply = await instance.chat(system="s", messages=messages, tool_specs=(READ,))
    assert reply.provider == "gemini" and not reply.tool_calls
    payload = session.requests[0][1]["json"]
    assert payload["contents"][1]["parts"] == [{"functionCall": {"name": READ.name, "args": {}}}]
    assert payload["contents"][2]["parts"] == [{"functionResponse": {"name": READ.name, "response": {"ok": True, "mode": "audio"}}}]
    assert "groq_real_id" not in json.dumps(payload)
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_unsupported_text_fallback_never_resends_native_effect_history(chains):
    monkey_error = P.ProviderError("native tools not supported", kind="tools_unsupported", status=400)
    groq = Client(monkey_error, monkey_error)
    instance = router(groq)
    call = NativeToolCall("confirmed_effect_id", "propor_acao", {"action": "ban_member", "target_ref": "m1"})
    messages = [P.ChatMessage("assistant", "", tool_calls=(call,)),
                P.ChatMessage("tool", '{"ok":true,"status":"pending_approval"}',
                              tool_call_id=call.id, name=call.name)]
    for _ in range(2):
        with pytest.raises(P.AllProvidersExhausted) as failure:
            await instance.chat(system="s", messages=messages, tool_specs=(READ,))
        assert failure.value.kind == "tools_unsupported"
        assert failure.value.retry_after is None
    assert len(groq.requests) == 2
    assert all("tool_specs" in request for request in groq.requests)
    skips = instance.diagnostics()["last_request"]["skips"]
    assert sum(item["reason"] == "native_history_requires_tools" for item in skips) == 2
    assert instance.diagnostics()["last_request"]["attempt_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_final_no_tools_round_keeps_declarations_and_native_history(provider):
    signature = {"functionCall": {"name": READ.name, "args": {}, "id": "native_id"}, "thoughtSignature": "opaque-signature"}
    calls = (NativeToolCall("native_id", READ.name, {}, {"part": signature} if provider == "gemini" else {}),)
    data = (_groq if provider == "groq" else _gemini)("Resposta final.")
    session = _Session(_Response(data))
    client = (P._GroqClient if provider == "groq" else P._GeminiClient)(session, "test-placeholder")
    reply = await client.chat(system="s", messages=[
        P.ChatMessage("assistant", "", tool_calls=calls),
        P.ChatMessage("tool", '{"ok":true}', tool_call_id="native_id", name=READ.name),
    ], temperature=.8, model="test-model", timeout_seconds=5, tool_specs=(READ,), allow_tool_calls=False)
    payload = session.requests[0][1]["json"]
    assert reply.text == "Resposta final." and not reply.tool_calls
    if provider == "groq":
        assert payload["tool_choice"] == "none" and payload["tools"][0]["function"]["name"] == READ.name
        assert payload["messages"][-1]["tool_call_id"] == "native_id"
        assert payload["max_completion_tokens"] == C.MAX_RESPONSE_TOKENS
    else:
        assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"
        assert payload["tools"][0]["functionDeclarations"][0]["name"] == READ.name
        assert payload["contents"][0]["parts"] == [signature]
        assert payload["contents"][1]["parts"][0]["functionResponse"]["id"] == "native_id"
        assert payload["generationConfig"]["maxOutputTokens"] == C.MAX_RESPONSE_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_provider_ignoring_none_tool_choice_is_rejected(provider):
    data = (_groq if provider == "groq" else _gemini)("discard private preview", calls=[(READ.name, {})])
    client = (P._GroqClient if provider == "groq" else P._GeminiClient)(_Session(_Response(data)), "test-placeholder")
    with pytest.raises(P.ProviderError) as failure:
        await client.chat(system="s", messages=[], temperature=.8, model="test-model", timeout_seconds=5,
                          tool_specs=(READ,), allow_tool_calls=False)
    assert failure.value.kind == "invalid_response"
    assert "private" not in str(failure.value)


@pytest.mark.asyncio
async def test_gemini_schema_subset_preserves_host_bounds_and_privacy(chains):
    declaration = proposal_tool(("send_audio", "timeout_member"), ("autor", "m1"))
    spec = ToolSpec(declaration["name"], declaration["description"], declaration["parameters"])
    private = "private spoken content"
    session = _Session(_Response(_gemini(private, calls=[(TOOL_NAME, {"action": "send_audio", "text": private})])))
    client = P._GeminiClient(session, "test-placeholder")
    reply = await client.chat(system="s", messages=[], temperature=.8, model="gemini-2.5-flash",
                              timeout_seconds=5, tool_specs=(spec,))
    schema = session.requests[0][1]["json"]["tools"][0]["functionDeclarations"][0]["parameters"]
    encoded = json.dumps(schema)
    assert not any(key in encoded for key in ("additionalProperties", "maxLength", "minLength"))
    assert schema["properties"]["options"]["properties"]["duration_seconds"]["maximum"] == 2419200
    assert "máximo de caracteres: 800" in schema["properties"]["text"]["description"]
    assert spec.parameters["properties"]["text"]["maxLength"] == 800
    assert spec.parameters["additionalProperties"] is False
    assert reply.text == "" and reply.proposals[0].text == private


def test_diagnostics_config_missing_does_not_initialize_model_circuits():
    instance = P.ProviderRouter(object(), groq_key="test-placeholder")
    assert instance.snapshot() == {}
    assert instance.diagnostics()["configured"] == {"groq": True, "gemini": False, "mistral": False, "cloudflare": False}
    assert instance.diagnostics()["circuits"] == {}
