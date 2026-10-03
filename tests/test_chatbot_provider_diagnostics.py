"""Winning model diagnostics expose metadata, never chat or attachment content."""
from __future__ import annotations

import base64
import json
import logging
import re

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.media import PreparedImage
from cogs.chatbot.providers import (
    AllProvidersExhausted, ChatMessage, ProviderError, ProviderRouter, RateLimitError,
    _GeminiClient,
)


class _Client:
    def __init__(self, *results):
        self.results = list(results)
        self.models = []
        self.requests = []

    async def chat(self, **kwargs):
        self.models.append(kwargs["model"])
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def chains(monkeypatch):
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("groq", "gemini"), raising=False)
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-text",))
    monkeypatch.setattr(C, "GROQ_VISION_MODELS", ("groq-vision",))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-text-first", "gemini-text-second"))
    monkeypatch.setattr(C, "GEMINI_VISION_MODELS", ("gemini-vision-first", "gemini-vision-second"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "vision"])
async def test_success_identifies_actual_fallback_winner_without_private_content(chains, caplog, mode):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    router = ProviderRouter(object(), groq_key="private-groq-key", gemini_key="private-gemini-key")
    router._groq = _Client(ProviderError("private provider response body", status=404, kind="model"))
    router._gemini = _Client(
        ProviderError("private empty-output details", kind="empty", stage="output", finish_reason="MAX_TOKENS"),
        "private assistant reply",
    )
    images = [PreparedImage("image/png", b"private-image-bytes", "private-filename.png")] if mode == "vision" else []
    image_urls = ["https://cdn.discordapp.com/attachments/private.png?ex=private-signature"] if images else []
    result = await router.chat(
        system="private system instructions",
        messages=[
            ChatMessage("assistant", "private old message"),
            ChatMessage("user", "private user request", images=images, image_urls=image_urls),
        ],
    )

    assert result == "private assistant reply"
    assert router._groq.models == [f"groq-{mode}"]
    assert router._gemini.models == [f"gemini-{mode}-first", f"gemini-{mode}-second"]
    successes = [record.getMessage() for record in caplog.records if "result=success" in record.getMessage()]
    assert len(successes) == 1
    assert re.fullmatch(
        rf"chatbot: result=success provider=gemini model=gemini-{mode}-second mode={mode} elapsed_ms=\d+ message_count=2",
        successes[0],
    )
    assert "finish_reason=MAX_TOKENS" in caplog.text
    assert "private" not in caplog.text
    assert "attachments/" not in caplog.text
    assert "data:image" not in caplog.text
    assert "private" not in str(router.snapshot())


@pytest.mark.asyncio
async def test_gemini_merges_adjacent_user_parts_without_crossing_model_turn(caplog):
    class Body:
        async def iter_chunked(self, size):
            yield json.dumps({"candidates": [{
                "content": {"parts": [{"text": "private image reply"}]},
                "finishReason": "STOP",
            }]}).encode()

    class Response:
        status = 200
        headers = {}
        content = Body()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        request = None

        def post(self, url, **kwargs):
            self.request = kwargs
            return Response()

    session = Session()
    image_bytes = b"private prepared screenshot bytes"
    image = PreparedImage("image/png", image_bytes, "private screenshot.png")
    messages = [
        ChatMessage("user", "private historical user text"),
        ChatMessage("assistant", "private historical bot answer"),
        ChatMessage("user", "private quoted context"),
        ChatMessage("user", "private current image question", images=[image]),
    ]
    assert await _GeminiClient(session, "private-api-key").chat(
        system="private system instructions", messages=messages, temperature=.8,
        model="gemini-2.5-flash", timeout_seconds=5,
    ) == "private image reply"

    payload = session.request["json"]
    assert [content["role"] for content in payload["contents"]] == ["user", "model", "user"]
    assert payload["contents"][0]["parts"] == [{"text": "private historical user text"}]
    assert payload["contents"][1]["parts"] == [{"text": "private historical bot answer"}]
    parts = payload["contents"][2]["parts"]
    assert len(parts) == 3
    assert parts[:2] == [{"text": "private quoted context"}, {"text": "private current image question"}]
    assert parts[2]["inlineData"]["mimeType"] == "image/png"
    assert base64.b64decode(parts[2]["inlineData"]["data"]) == image_bytes
    assert payload["systemInstruction"] == {"parts": [{"text": "private system instructions"}]}
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_gemini_first_success_does_not_call_groq(chains, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("gemini", "groq"))
    router = ProviderRouter(object(), groq_key="private-groq-key", gemini_key="private-gemini-key")
    router._groq = _Client("must not be requested")
    router._gemini = _Client("private gemini reply")

    assert await router.chat(system="private prompt", messages=[ChatMessage("user", "private message")], text_provider_order=("gemini", "groq")) == "private gemini reply"
    assert router._gemini.models == ["gemini-text-first"]
    assert not router._groq.models
    assert "result=success provider=gemini model=gemini-text-first mode=text" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_gemini_priority_failure_preserves_context_in_groq_fallback(chains, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("gemini",))
    router = ProviderRouter(object(), groq_key="private-groq-key", gemini_key="private-gemini-key")
    router._gemini = _Client(ProviderError("private network error", kind="network", status=503))
    router._groq = _Client("private fallback reply")
    messages = [ChatMessage("assistant", "private earlier answer"), ChatMessage("user", "private follow-up")]

    assert await router.chat(system="private system", messages=messages, temperature=.65, text_provider_order=("gemini",)) == "private fallback reply"
    assert router._gemini.models == ["gemini-text-first"]
    assert router._groq.models == ["groq-text"]
    first, fallback = router._gemini.requests[0], router._groq.requests[0]
    assert first["messages"] is fallback["messages"]
    assert fallback["messages"] == messages
    assert first["system"] == fallback["system"] == "private system"
    assert first["temperature"] == fallback["temperature"] == .65
    assert "result=success provider=groq model=groq-text mode=text" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("priority,expected", [
    (("unknown", "gemini", "gemini", "groq", "groq"), ["gemini-text-first", "gemini-text-second", "groq-text"]),
    (("unknown", "unknown"), ["groq-text", "gemini-text-first", "gemini-text-second"]),
    ((), ["groq-text", "gemini-text-first", "gemini-text-second"]),
])
async def test_invalid_or_duplicate_priority_keeps_each_configured_attempt_once(chains, caplog, monkeypatch, priority, expected):
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", priority)
    router = ProviderRouter(object(), groq_key="private-groq-key", gemini_key="private-gemini-key")
    router._groq = _Client(ProviderError("private request error", kind="request", status=400))
    router._gemini = _Client(
        ProviderError("private request error", kind="request", status=400),
        ProviderError("private request error", kind="request", status=400),
    )

    with pytest.raises(AllProvidersExhausted):
        await router.chat(system="private system", messages=[], text_provider_order=priority)

    attempted = [
        re.search(r" model=(\S+)", record.getMessage()).group(1)
        for record in caplog.records if "kind=request" in record.getMessage()
    ]
    assert attempted == expected
    assert len(attempted) == len(set(attempted))
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_text_priority_does_not_change_vision_order(chains, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    monkeypatch.setattr(C, "TEXT_PROVIDER_ORDER", ("gemini", "gemini", "unknown", "groq"))
    router = ProviderRouter(object(), groq_key="private-groq-key", gemini_key="private-gemini-key")
    router._groq = _Client("private vision reply")
    router._gemini = _Client("must not be requested")
    message = ChatMessage("user", "private image question", images=[PreparedImage("image/png", b"private bytes")])

    assert await router.chat(system="private system", messages=[message]) == "private vision reply"
    assert router._groq.models == ["groq-vision"]
    assert not router._gemini.models
    assert "result=success provider=groq model=groq-vision mode=vision" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_rate_limit_keeps_fallback_and_circuit_behavior(chains, caplog):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    router = ProviderRouter(object(), groq_key="private-key", gemini_key="private-key")
    router._groq = _Client(RateLimitError("private quota response", retry_after=12))
    router._gemini = _Client("private reply")

    assert await router.chat(system="private prompt", messages=[]) == "private reply"
    state = router.snapshot()["groq/groq-text"]
    assert not state["available"]
    assert state["last_status"] == 429
    assert 0 < state["cooldown_seconds"] <= 12
    assert "kind=rate_limit status=429 finish_reason=none" in caplog.text
    assert "result=success provider=gemini model=gemini-text-first mode=text" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,diagnostic", [
    ("SAFETY", "SAFETY"),
    (None, "none"),
    ("private unexpected finish reason\nprivate response content", "other"),
])
async def test_finish_reason_is_safe_to_log_without_changing_typed_error(chains, caplog, reason, diagnostic):
    caplog.set_level(logging.INFO, logger="cogs.chatbot.providers")
    router = ProviderRouter(object(), groq_key="private-key", gemini_key="private-key")
    original = ProviderError(
        "private blocked-output details", status=400, kind="blocked",
        stage="output", finish_reason=reason,
    )
    router._groq = _Client(original)
    router._gemini = _Client("must never be requested")

    with pytest.raises(AllProvidersExhausted) as failure:
        await router.chat(system="private prompt", messages=[ChatMessage("user", "private request")])

    assert failure.value.kind == "blocked"
    assert failure.value.stage == "output"
    assert failure.value.status == 400
    assert failure.value.finish_reason == reason
    assert failure.value.__cause__ is original
    assert not router._gemini.models
    assert router.snapshot()["groq/groq-text"]["available"]
    assert f"finish_reason={diagnostic}" in caplog.text
    assert "result=success" not in caplog.text
    assert "private" not in caplog.text
