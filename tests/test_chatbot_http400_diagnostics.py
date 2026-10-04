"""Erros de payload não são cotas; diagnóstico nunca copia geração privada."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.providers import ChatReply
from tests.test_chatbot_action_providers import _Response, _Session, _groq
from tests.test_chatbot_audio_format import turn
from tests.test_chatbot_native_tools import READ
from tests.test_chatbot_tool_delivery_state import delivery, loop, call, reply_calls


PRIVATE = "PRIVATE_AUDIO_TRANSCRIPT_AND_API_KEY_DO_NOT_LOG"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,error,code", [
    ("gemini", {"code": 400, "status": "INVALID_ARGUMENT", "message":
                "GenerateContentRequest.tools[0].function_declarations[2].parameters.properties: "
                "should be non-empty for OBJECT type."}, "api_tool_schema"),
    ("gemini", {"status": "INVALID_ARGUMENT", "message":
                "function_response name must match a function_call in the previous turn"}, "api_tool_history"),
    ("groq", {"code": "invalid_request_error", "message":
              "An assistant message with tool_calls must be followed by tool messages responding to each tool_call_id"},
     "api_tool_history"),
    ("groq", {"code": "context_length_exceeded", "message": "Please reduce the length of the messages"},
     "api_context_limit"),
    ("gemini", {"status": "INVALID_ARGUMENT", "message":
                "The input token count exceeds the maximum number of tokens allowed"}, "api_context_limit"),
    ("groq", {"message": "reasoning_effort must be one of low, medium, high"}, "api_parameter"),
    ("gemini", {"status": "INVALID_ARGUMENT", "message":
                "Unknown name thinkingConfig at generationConfig"}, "api_parameter"),
    ("groq", {"code": "invalid_request_error", "message": "Invalid argument in request"}, "api_invalid_argument"),
])
async def test_structured_http400_has_safe_actionable_diagnostic_without_becoming_quota(provider, error, code):
    data = {"error": {**error, "failed_generation": PRIVATE,
                      "details": [{"private": PRIVATE}]}}
    failure = await P._http_error(_Response(data, status=400), provider=provider)
    assert failure.status == 400 and failure.kind == "request" and failure.stage == "api"
    assert failure.diagnostic_code == code
    assert failure.retry_after is None
    assert PRIVATE not in str(failure) and PRIVATE not in json.dumps(failure.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize("irrelevant", [
    "content_filter safety blocked prohibited_content",
    "model_not_found model has been decommissioned",
    "tools are not supported function calling is not supported",
    "invalid_api_key authentication_error",
    "context_length_exceeded tool_use_failed",
])
async def test_failed_generation_does_not_classify_api_failure(irrelevant):
    data = {"error": {"message": "Invalid argument in request", "code": "invalid_request_error",
                      "failed_generation": PRIVATE + irrelevant}, "prompt": PRIVATE + irrelevant}
    failure = await P._http_error(_Response(data, status=400), provider="groq")
    assert failure.kind == "request" and failure.diagnostic_code == "api_invalid_argument"
    assert PRIVATE not in str(failure) and PRIVATE not in json.dumps(failure.__dict__)


@pytest.mark.asyncio
async def test_groq_api_tool_validation_has_one_safe_repair_without_exposing_failed_generation(monkeypatch, caplog):
    monkeypatch.setattr(C, "GROQ_MODELS", ("groq-main",))
    monkeypatch.setattr(C, "GROQ_VISION_MODELS", ())
    monkeypatch.setattr(C, "GEMINI_MODELS", ())
    caplog.set_level(logging.INFO)
    session = _Session(
        _Response({"error": {"code": "tool_use_failed", "type": "invalid_request_error",
                            "message": "Failed to call a function. Please adjust your prompt.",
                            "failed_generation": PRIVATE + "content_filter model_not_found"}}, status=400),
        _Response(_groq("O resultado confirmado é 42.", finish="stop")),
    )
    router = P.ProviderRouter(session, groq_key="fake-key")
    repair_state = {"used": False}
    reply = await router.chat(system="system private", messages=[P.ChatMessage("user", "consulta")],
                              temperature=.8, tool_specs=(READ,), repair_state=repair_state)
    assert reply.text == "O resultado confirmado é 42." and repair_state["used"] is True
    assert len(session.requests) == 2
    first, repaired = (request[1]["json"] for request in session.requests)
    assert first["messages"][1:] == repaired["messages"][1:]
    assert first["tools"] == repaired["tools"]
    assert "api_tool_validation" in repaired["messages"][0]["content"]
    assert PRIVATE not in json.dumps([first, repaired]) and PRIVATE not in caplog.text
    report = router.get_request_report()
    assert report["attempts"][0]["kind"] == "invalid_response"
    assert report["attempts"][0]["diagnostic_code"] == "api_tool_validation"
    assert report["attempts"][1]["repair"] is True


@pytest.mark.asyncio
async def test_deadline_finishing_round_has_results_for_every_original_call_and_never_starts_pending_effect(delivery):
    async def exhausted_read(_arguments):
        return {"ok": False, "status": "deadline", "error": "A consulta esgotou seu prazo."}
    delivery.register("exhausted_read", exhausted_read)
    requests = []

    async def model(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return reply_calls(call("known", "read_state"), call("expired", "exhausted_read"),
                               call("never-started", "control_music"))
        assert kwargs["allow_tool_calls"] is False
        messages = kwargs["messages"]
        announced = [item.id for message in messages for item in message.tool_calls]
        answered = [message.tool_call_id for message in messages if message.role == "tool"]
        assert announced == answered == ["known", "expired", "never-started"]
        responses = [json.loads(message.content) for message in messages if message.role == "tool"]
        assert responses[-1]["ok"] is False and responses[-1]["status"] == "not_executed"
        assert responses[0]["data"]["value"] == 42 and responses[1]["status"] == "deadline"
        return ChatReply("Consegui o primeiro dado; as outras etapas ficaram sem executar.")

    delivery.cog._router.chat.side_effect = model
    result, _registry, state, _messages = await loop(delivery)
    assert result.text.startswith("Consegui o primeiro dado")
    assert not state.get("deadline") and not state["effects_confirmed"]
    assert delivery.events == ["query"] and len(requests) == 2


@pytest.mark.asyncio
async def test_external_turn_cancellation_never_starts_finishing_request_or_pending_effect(delivery):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_read(_arguments):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    delivery.register("blocked_read", blocked_read)
    delivery.cog._router.chat.return_value = reply_calls(
        call("known", "read_state"), call("blocked", "blocked_read"), call("pending-effect", "control_music"))
    task = asyncio.create_task(loop(delivery))
    await asyncio.wait_for(entered.wait(), timeout=.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and delivery.events == ["query"]
    delivery.cog._router.chat.assert_awaited_once()
    delivery.message.reply.assert_not_awaited()
