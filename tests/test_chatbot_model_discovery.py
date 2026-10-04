"""Offline Gemini catalog discovery, deadlines and safe native fallback."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.action_protocol import ChatReply
from cogs.chatbot.media import PreparedImage
from cogs.chatbot.tool_registry import ToolSpec


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0 + self.now


class _Body:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]


class _Response:
    def __init__(self, data, *, status=200, headers=None, entered=None,
                 release=None, clock=None, consume_seconds=0.0, delay=0.0):
        self.status = status
        self.headers = headers or {}
        self.content = _Body(data)
        self.entered, self.release = entered, release
        self.clock, self.consume_seconds, self.delay = clock, consume_seconds, delay

    async def __aenter__(self):
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.clock:
            self.clock.now += self.consume_seconds
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, *, gets=(), posts=()):
        self.get_responses, self.post_responses = list(gets), list(posts)
        self.get_requests, self.post_requests = [], []

    def get(self, url, **kwargs):
        self.get_requests.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_requests.append((url, kwargs))
        return self.post_responses.pop(0)


def _model(name, *, methods=("generateContent",), modalities=None):
    row = {"name": "models/" + name, "supportedGenerationMethods": list(methods)}
    if modalities is not None:
        row["inputModalities"] = modalities
    return row


def _catalog(*rows):
    return {"models": list(rows)}


def _text(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def _missing():
    return _Response({"error": {"code": 404, "status": "NOT_FOUND",
                                "message": "private missing-model response"}}, status=404)


def _client(session):
    return P._GeminiClient(session, "placeholder-secret-key")


@pytest.fixture
def chains(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main",))
    monkeypatch.setattr(C, "GROQ_VISION_MODELS", ("groq-vision",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-2.5-missing",))
    monkeypatch.setattr(C, "GEMINI_VISION_MODELS", ("gemini-2.5-missing-vision",))


@pytest.mark.asyncio
async def test_catalog_uses_header_key_bounded_pages_and_safe_conversation_models():
    accepted = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
                "gemini-3-flash-preview", "gemini-2.5-flash-latest"]
    rejected = ["gemini-2.5-flash-preview-tts", "gemini-2.5-flash-live",
                "gemini-2.5-flash-native-audio", "gemini-2.5-flash-image",
                "gemini-embedding-001", "gemini-2.5-deep-research",
                "gemini-2.5-computer-use-preview", "gemini-robotics-er-1.5-preview",
                "gemini-2.5-flash/../../private", "gemini-" + "x" * 101, "other-model"]
    data = _catalog(*[_model(name) for name in accepted + rejected],
                    _model("gemini-2.5-flash"), _model("gemini-2.5-unavailable", methods=("countTokens",)))
    data["nextPageToken"] = "private-pagination-token"
    session = _Session(gets=[_Response(data), _Response(_catalog())])
    client = _client(session)
    models = await client.discover_models(timeout_seconds=20)
    assert set(models) == set(accepted)
    assert models[0] == "gemini-2.5-flash"
    assert client.catalog_models() == models
    assert len(session.get_requests) == 2
    url, kwargs = session.get_requests[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models"
    assert kwargs["headers"]["x-goog-api-key"] == "placeholder-secret-key"
    assert kwargs["params"] == {"pageSize": 50}
    assert kwargs["timeout"].total <= 3
    assert "key=" not in url and "private-pagination-token" not in repr(kwargs)
    assert session.get_requests[1][1]["params"] == {
        "pageSize": 50, "pageToken": "private-pagination-token",
    }


@pytest.mark.asyncio
async def test_catalog_processes_only_first_fifty_rows_and_keeps_at_most_eight():
    first_fifty = [_model("gemini-2.5-flash-live") for _ in range(50)]
    session = _Session(gets=[_Response(_catalog(*first_fifty, _model("gemini-2.5-flash"))),
                             _Response(_catalog(*[_model(f"gemini-2.5-flash-{i}") for i in range(12)]))])
    client = _client(session)
    assert await client.discover_models(timeout_seconds=1) == ()
    # A new instance has no negative-cache entry from the first response.
    models = await _client(session).discover_models(timeout_seconds=1)
    assert len(models) == 8
    assert len(set(models)) == 8
    assert "gemini-2.5-flash" not in models


@pytest.mark.asyncio
async def test_catalog_follows_second_page_for_conversation_model_but_never_third(caplog):
    first = _catalog(*[_model("gemini-2.5-flash-live") for _ in range(50)])
    first["nextPageToken"] = "private-page-one"
    second = _catalog(_model("gemini-2.5-flash"))
    second["nextPageToken"] = "private-page-two"
    session = _Session(gets=[_Response(first), _Response(second)])
    client = _client(session)
    with caplog.at_level(logging.DEBUG):
        assert await client.discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)
    assert len(session.get_requests) == 2
    assert session.get_requests[1][1]["params"]["pageToken"] == "private-page-one"
    assert "private-page-one" not in caplog.text and "private-page-two" not in caplog.text


@pytest.mark.asyncio
async def test_catalog_pages_share_one_original_deadline(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(P, "time", clock)
    first = _catalog(_model("gemini-2.5-flash-live"))
    first["nextPageToken"] = "page-two"
    session = _Session(gets=[_Response(first, clock=clock, consume_seconds=.07),
                             _Response(_catalog(_model("gemini-2.5-flash")))])
    assert await _client(session).discover_models(timeout_seconds=.1) == ("gemini-2.5-flash",)
    assert session.get_requests[0][1]["timeout"].total <= .1001
    assert session.get_requests[1][1]["timeout"].total <= .0301


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["x" * 513, 123, None])
async def test_unbounded_or_malformed_next_token_does_not_add_catalog_request(token):
    data = _catalog(_model("gemini-2.5-flash"))
    data["nextPageToken"] = token
    session = _Session(gets=[_Response(data)])
    assert await _client(session).discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)
    assert len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_text_only_catalog_priority_does_not_evict_available_vision_candidate():
    rows = [_model(f"gemini-2.5-flash-{i}", modalities=["TEXT"]) for i in range(12)]
    rows.append(_model("gemini-2.5-pro", modalities=["TEXT", "IMAGE"]))
    session = _Session(gets=[_Response(_catalog(*rows))])
    client = _client(session)
    text_models = await client.discover_models(timeout_seconds=1)
    assert len(text_models) == 8 and "gemini-2.5-pro" not in text_models
    assert client.catalog_models(has_images=True) == ("gemini-2.5-pro",)
    assert await client.discover_models(timeout_seconds=1, has_images=True) == ("gemini-2.5-pro",)
    assert len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_image_candidates_respect_explicit_modalities_and_old_text_models():
    rows = [_model("gemini-2.5-flash", modalities=["TEXT", "IMAGE"]),
            _model("gemini-2.5-flash-lite", modalities=["TEXT"]),
            _model("gemini-2.0-flash"), _model("gemini-1.5-flash"),
            _model("gemini-1.0-pro")]
    session = _Session(gets=[_Response(_catalog(*rows))])
    client = _client(session)
    text_models = await client.discover_models(timeout_seconds=1)
    assert "gemini-2.5-flash-lite" in text_models
    images = client.catalog_models(has_images=True)
    assert set(images) == {"gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"}
    assert await client.discover_models(timeout_seconds=1, has_images=True) == images
    assert len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_successful_catalog_cache_expires_after_ten_minutes(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(P, "time", clock)
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash"))),
                             _Response(_catalog(_model("gemini-3-flash-preview")))])
    client = _client(session)
    assert await client.discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)
    clock.now += 599
    assert await client.discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)
    assert len(session.get_requests) == 1
    clock.now += 2
    assert client.catalog_models() == ()
    assert await client.discover_models(timeout_seconds=1) == ("gemini-3-flash-preview",)
    assert len(session.get_requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("data,status,kind,wait", [
    (b"invalid JSON private body", 200, "invalid_response", 60),
    ({"models": "private wrong shape"}, 200, "invalid_response", 60),
    ({"error": {"message": "private auth body"}}, 401, "auth", 900),
    ({"error": {"message": "private unavailable body"}}, 503, "network", 60),
])
async def test_catalog_failure_is_negative_cached_without_private_diagnostics(monkeypatch, caplog, data, status, kind, wait):
    clock = _Clock()
    monkeypatch.setattr(P, "time", clock)
    session = _Session(gets=[_Response(data, status=status),
                             _Response(_catalog(_model("gemini-2.5-flash")))])
    client = _client(session)
    with caplog.at_level(logging.DEBUG):
        assert await client.discover_models(timeout_seconds=1) == ()
        assert await client.discover_models(timeout_seconds=1) == ()
    diagnostics = client.discovery_diagnostics()
    assert diagnostics["kind"] == kind
    assert diagnostics["available_count"] == 0
    assert "private" not in json.dumps(diagnostics)
    assert "private" not in caplog.text and "placeholder-secret-key" not in caplog.text
    clock.now += wait + 1
    assert await client.discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)
    assert len(session.get_requests) == 2


@pytest.mark.asyncio
async def test_malformed_rows_do_not_qualify_as_supported_models_or_break_discovery():
    rows = [_model("gemini-2.5-flash"), {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": None},
            {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": "generateContent"},
            {"name": "models/gemini-2.5-other", "supportedGenerationMethods": 1}, None, "private wrong row"]
    session = _Session(gets=[_Response(_catalog(*rows))])
    assert await _client(session).discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)


@pytest.mark.asyncio
async def test_catalog_quota_honors_retry_info_beyond_negative_cache(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(P, "time", clock)
    error = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "123s"}]}}
    session = _Session(gets=[_Response(error, status=429),
                             _Response(_catalog(_model("gemini-2.5-flash")))])
    client = _client(session)
    assert await client.discover_models(timeout_seconds=1) == ()
    diagnostics = client.discovery_diagnostics()
    assert diagnostics["kind"] == "rate_limit" and diagnostics["status"] == 429
    assert diagnostics["retry_after"] == pytest.approx(123)
    clock.now += 120
    assert await client.discover_models(timeout_seconds=1) == ()
    assert len(session.get_requests) == 1
    clock.now += 4
    assert await client.discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)


@pytest.mark.asyncio
async def test_catalog_singleflight_survives_canceled_waiter():
    entered, release = asyncio.Event(), asyncio.Event()
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")),
                                      entered=entered, release=release)])
    client = _client(session)
    canceled = asyncio.create_task(client.discover_models(timeout_seconds=1))
    await asyncio.wait_for(entered.wait(), timeout=1)
    survivor = asyncio.create_task(client.discover_models(timeout_seconds=1))
    await asyncio.sleep(0)
    canceled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled
    release.set()
    assert await survivor == ("gemini-2.5-flash",)
    assert len(session.get_requests) == 1
    assert client.catalog_models() == ("gemini-2.5-flash",)


@pytest.mark.asyncio
async def test_catalog_request_stays_within_callers_small_timeout():
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")), delay=.05)])
    began = time.monotonic()
    client = _client(session)
    assert await client.discover_models(timeout_seconds=.01) == ()
    # A shielded waiter may return one event-loop turn before the shared task
    # records its own deadline. The task itself still must finish on time.
    await asyncio.wait_for(asyncio.shield(client._catalog_task), timeout=.1)
    assert time.monotonic() - began < .2
    assert session.get_requests[0][1]["timeout"].total <= .01
    assert client.discovery_diagnostics()["kind"] == "timeout"
    assert client.catalog_models() == ()


@pytest.mark.asyncio
async def test_shorter_singleflight_waiter_does_not_cancel_longer_valid_fetch():
    entered, release = asyncio.Event(), asyncio.Event()
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")),
                                      entered=entered, release=release)])
    client = _client(session)
    primary = asyncio.create_task(client.discover_models(timeout_seconds=1))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert await client.discover_models(timeout_seconds=.01) == ()
    assert not primary.done()
    release.set()
    assert await primary == ("gemini-2.5-flash",)
    assert client.discovery_diagnostics()["kind"] == "success"
    assert len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_legacy_session_without_get_gracefully_skips_catalog():
    class LegacySession:
        pass

    client = _client(LegacySession())
    assert await client.discover_models(timeout_seconds=1) == ()
    assert client.catalog_models() == ()


@pytest.mark.asyncio
async def test_router_after_missing_model_discovers_native_image_candidate_without_execution(chains):
    invoked = []
    spec = ToolSpec("ler_estado", "Leia o estado solicitado.", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, handler=lambda **kwargs: invoked.append(kwargs))
    part = {"functionCall": {"name": "ler_estado", "args": {"query": "estado"}, "id": "native-id"},
            "thoughtSignature": "private-opaque-signature"}
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")))],
                       posts=[_missing(), _Response({"candidates": [{"content": {"parts": [part]}, "finishReason": "STOP"}]})])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    image = PreparedImage("image/png", b"prepared private image bytes", "example.png")
    reply = await router.chat(system="private system", messages=[P.ChatMessage("user", "olhe", images=[image])],
                              tool_specs=(spec,), budget_seconds=1)
    assert isinstance(reply, ChatReply) and reply.model == "gemini-2.5-flash"
    assert reply.tool_calls[0].id == "native-id"
    assert reply.tool_calls[0].provider_data["part"] == part
    assert not invoked
    payload = session.post_requests[-1][1]["json"]
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "ler_estado"
    assert base64.b64decode(payload["contents"][-1]["parts"][1]["inlineData"]["data"]) == image.data
    assert len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_router_preserves_configured_healthy_priority_over_discovered_models(monkeypatch):
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-2.5-missing", "gemini-2.5-configured"))
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")))],
                       posts=[_missing(), _Response(_text("configured works")), _Response(_text("configured again"))])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    assert await router.chat(system="s", messages=[]) == "configured works"
    assert await router.chat(system="s", messages=[]) == "configured again"
    models = [url.split("/models/", 1)[1].split(":", 1)[0] for url, _ in session.post_requests]
    assert models == ["gemini-2.5-missing", "gemini-2.5-configured", "gemini-2.5-configured"]
    assert len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_router_caps_new_candidates_and_reuses_catalog_across_turns(chains):
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash"),
                                               _model("gemini-2.5-flash-lite"), _model("gemini-2.5-pro")))],
                       posts=[_missing(), _missing(), _Response(_text("lite works")), _Response(_text("lite again"))])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    assert await router.chat(system="s", messages=[]) == "lite works"
    assert await router.chat(system="s", messages=[]) == "lite again"
    models = [url.split("/models/", 1)[1].split(":", 1)[0] for url, _ in session.post_requests]
    assert models == ["gemini-2.5-missing", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash-lite"]
    assert len(session.get_requests) == 1
    assert all("gemini-2.5-pro" not in url for url, _ in session.post_requests)


@pytest.mark.asyncio
async def test_router_never_tries_a_third_new_candidate_after_two_missing_models(chains):
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash"),
                                               _model("gemini-2.5-flash-lite"), _model("gemini-2.5-pro")))],
                       posts=[_missing(), _missing(), _missing()])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[])
    assert failure.value.kind == "model"
    assert len(session.post_requests) == 3
    assert len(session.get_requests) == 1
    assert all("gemini-2.5-pro" not in url for url, _ in session.post_requests)


@pytest.mark.asyncio
async def test_router_never_discovers_on_quota_or_auth_failure(chains):
    for status in (429, 401):
        session = _Session(posts=[_Response({"error": {"message": "private API failure"}}, status=status)])
        router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
        with pytest.raises(P.AllProvidersExhausted):
            await router.chat(system="s", messages=[])
        assert not session.get_requests


@pytest.mark.asyncio
async def test_cached_candidates_respect_proven_account_quota(chains):
    quota = {"error": {"quota_scope": "account", "code": "account_quota_exceeded"}}
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")))],
                       posts=[_Response(quota, status=429, headers={"retry-after": "123"})])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    assert await router._gemini.discover_models(timeout_seconds=1) == ("gemini-2.5-flash",)
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[])
    assert failure.value.kind == "rate_limit"
    assert len(session.post_requests) == 1
    assert not router.snapshot()["gemini/gemini-2.5-flash"]["available"]
    with pytest.raises(P.AllProvidersExhausted):
        await router.chat(system="s", messages=[])
    assert len(session.post_requests) == 1 and len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_router_catalog_auth_is_not_a_new_model_candidate(chains):
    session = _Session(gets=[_Response({"error": {"message": "private invalid credential"}}, status=401)],
                       posts=[_missing()])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[])
    assert failure.value.kind == "auth"
    assert len(session.post_requests) == 1 and len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_router_catalog_quota_keeps_retry_and_avoids_polling(chains):
    error = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "123s"}]}}
    session = _Session(gets=[_Response(error, status=429)], posts=[_missing()])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[])
    assert failure.value.kind == "rate_limit"
    assert failure.value.retry_after == pytest.approx(123, abs=.1)
    assert all(state["quota_scope"] != "account" for state in router.snapshot().values())
    with pytest.raises(P.AllProvidersExhausted):
        await router.chat(system="s", messages=[])
    assert len(session.post_requests) == 1 and len(session.get_requests) == 1


@pytest.mark.asyncio
async def test_router_will_not_generate_after_catalog_spends_remaining_deadline(chains, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(P, "time", clock)
    session = _Session(gets=[_Response(_catalog(_model("gemini-2.5-flash")), clock=clock, consume_seconds=.2)],
                       posts=[_missing()])
    router = P.ProviderRouter(session, gemini_key="placeholder-secret-key")
    with pytest.raises(P.AllProvidersExhausted) as failure:
        await router.chat(system="s", messages=[], budget_seconds=.1)
    assert failure.value.kind == "deadline"
    assert len(session.post_requests) == 1
    assert session.get_requests[0][1]["timeout"].total <= .1001
