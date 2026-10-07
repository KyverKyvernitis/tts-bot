"""Contratos streaming reais do ProviderRouter, sem realizar chamadas de rede."""
from __future__ import annotations

import asyncio
import json

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.providers import (
    AllProvidersExhausted, ChatMessage, ProviderError, ProviderRouter,
    _GeminiClient, _GroqClient,
)


class _FakeLines:
    def __init__(self, events):
        self.lines = []
        for item in events:
            self.lines.append(b"data: " + (json.dumps(item).encode() if isinstance(item, dict) else item.encode()) + b"\n")
            self.lines.append(b"\n")

    async def __aiter__(self):
        for line in self.lines:
            await asyncio.sleep(0)
            yield line


class _FakeRequest:
    status = 200
    headers = {}

    def __init__(self, events):
        self.content = _FakeLines(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def __init__(self, events):
        self.events = events
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return _FakeRequest(self.events)


@pytest.mark.asyncio
async def test_groq_uses_upstream_sse_and_forwards_visible_fragments():
    sess = _FakeSession([
        {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "Olá"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "!"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}},
        "[DONE]",
    ])
    out = []

    async def on_delta(chunk):
        out.append(chunk)

    result = await _GroqClient(sess, "secret").chat(
        system="Responda curto", messages=[ChatMessage(role="user", content="oi")],
        model=C.GROQ_MODELS[0], temperature=0.5, timeout_seconds=10,
        allow_tool_calls=False, on_text_delta=on_delta,
    )
    assert result == "Olá!"
    assert out == ["Olá", "!"]
    assert sess.requests[0][1]["json"]["stream"] is True
    assert sess.requests[0][1]["json"]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_gemini_streaming_endpoint_and_chunks():
    sess = _FakeSession([
        {"candidates": [{"content": {"parts": [{"text": "Oi"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}},
    ])
    out = []

    async def on_delta(chunk):
        out.append(chunk)

    result = await _GeminiClient(sess, "secret").chat(
        system="Responda curto", messages=[ChatMessage(role="user", content="oi")],
        model=C.GEMINI_MODELS[0], temperature=0.5, timeout_seconds=10,
        allow_tool_calls=False, on_text_delta=on_delta,
    )
    assert result == "Oi!"
    assert out == ["Oi", "!"]
    assert ":streamGenerateContent?alt=sse" in sess.requests[0][0]


@pytest.mark.asyncio
async def test_fallback_before_first_delta_keeps_router_behavior():
    router = ProviderRouter(None, groq_key="x", gemini_key="y")
    forwards = []
    called = []

    class Groq:
        async def chat(self, **kwargs):
            called.append("groq")
            raise ProviderError("modelo indisponível", kind="model", status=404)

    class Gemini:
        async def chat(self, **kwargs):
            called.append("gemini")
            await kwargs["on_text_delta"]("Gemini OK")
            return "Gemini OK"

    router._groq, router._gemini = Groq(), Gemini()

    async def on_delta(chunk):
        forwards.append(chunk)

    result = await router.chat(system="", messages=[ChatMessage(role="user", content="Oi")],
                               allow_tool_calls=False, on_text_delta=on_delta, budget_seconds=20)
    assert result.text == "Gemini OK" if hasattr(result, "text") else result == "Gemini OK"
    assert forwards == ["Gemini OK"]
    assert "groq" in called and called[-1] == "gemini"


@pytest.mark.asyncio
async def test_router_does_not_fallback_after_first_delta():
    router = ProviderRouter(None, groq_key="x", gemini_key="y")
    forwards, called = [], []

    class Groq:
        async def chat(self, **kwargs):
            called.append("groq")
            await kwargs["on_text_delta"]("resposta parcial")
            raise ProviderError("socket caiu", kind="network", status=502)

    class Gemini:
        async def chat(self, **kwargs):
            called.append("gemini")
            return "Isto não deve ser enviado"

    router._groq, router._gemini = Groq(), Gemini()

    async def on_delta(chunk):
        forwards.append(chunk)

    with pytest.raises(AllProvidersExhausted):
        await router.chat(system="", messages=[ChatMessage(role="user", content="Oi")],
                          allow_tool_calls=False, on_text_delta=on_delta, budget_seconds=20)
    assert forwards == ["resposta parcial"]
    assert called == ["groq"]


@pytest.mark.asyncio
async def test_cloudflare_thinking_is_not_exposed_in_sse():
    from cogs.chatbot.providers import _openai_stream_result

    sess = _FakeSession([
        {"choices": [{"delta": {"content": "<thi"}}]},
        {"choices": [{"delta": {"content": "nk>hidden thoughts"}}]},
        {"choices": [{"delta": {"content": "</th"}}]},
        {"choices": [{"delta": {"content": "ink>Resposta"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ])
    shown = []

    async def emit(text):
        shown.append(text)

    data = await _openai_stream_result(_FakeRequest(sess.events), emit, cloudflare=True)
    assert shown == ["Resposta"]
    assert "hidden thoughts" in data["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_truncated_upstream_sse_does_not_succeed():
    from cogs.chatbot.providers import _openai_stream_result

    async def emit(_text):
        pass

    with pytest.raises(ProviderError, match="stream SSE terminou"):
        await _openai_stream_result(_FakeRequest([
            {"choices": [{"delta": {"content": "incompleto"}, "finish_reason": None}]},
        ]), emit)
