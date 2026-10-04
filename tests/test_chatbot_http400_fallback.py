"""HTTP 400 diagnostics and protocol-safe native fallback, without real APIs."""
from __future__ import annotations

import json
import logging

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot import providers as P
from cogs.chatbot.action_protocol import NativeToolCall
from cogs.chatbot.tool_registry import ToolSpec
from tests.test_chatbot_action_providers import _Response, _Session, _gemini


READ = ToolSpec("get_conversation_preferences", "Consulte preferências confirmadas.",
                {"type": "object", "properties": {}, "additionalProperties": False})
SET = ToolSpec("set_conversation_preferences", "Altere a própria preferência.",
               {"type": "object", "properties": {
                   "mode": {"type": "string", "enum": ["audio", "text", "auto"]}},
                "required": ["mode"], "additionalProperties": False}, permission="self")


def history():
    calls = (NativeToolCall("confirmed_groq_call", READ.name, {}),)
    return [P.ChatMessage("user", "Qual é minha preferência?"),
            P.ChatMessage("assistant", "", tool_calls=calls),
            P.ChatMessage("tool", '{"ok":true,"mode":"audio"}',
                          tool_call_id=calls[0].id, name=READ.name)]


@pytest.mark.asyncio
async def test_gemini_no_argument_function_omits_optional_parameters_and_preserves_host_schema():
    session = _Session(_Response(_gemini("Pronto.")))
    await P._GeminiClient(session, "offline-placeholder").chat(
        system="s", messages=[P.ChatMessage("user", "oi")], temperature=.8,
        model="gemini-2.5-flash", timeout_seconds=5, tool_specs=(READ, SET))
    declarations = {item["name"]: item for item in
                    session.requests[0][1]["json"]["tools"][0]["functionDeclarations"]}
    assert "parameters" not in declarations[READ.name]
    assert declarations[SET.name]["parameters"]["properties"]["mode"]["enum"] == ["audio", "text", "auto"]
    assert declarations[SET.name]["parameters"]["required"] == ["mode"]
    assert READ.parameters == {"type": "object", "properties": {}, "additionalProperties": False}


@pytest.mark.asyncio
async def test_gemini_no_argument_function_still_rejects_extra_arguments_on_host():
    session = _Session(_Response(_gemini(calls=[(READ.name, {"unexpected": "PRIVATE_VALUE"})])))
    with pytest.raises(P.ProviderError) as rejected:
        await P._GeminiClient(session, "offline-placeholder").chat(
            system="s", messages=[P.ChatMessage("user", "oi")], temperature=.8,
            model="gemini-2.5-flash", timeout_seconds=5, tool_specs=(READ,))
    assert rejected.value.kind == "invalid_response" and rejected.value.stage == "output"
    assert rejected.value.diagnostic_code == "additional_properties"
    assert "PRIVATE_VALUE" not in str(rejected.value)
    assert "parameters" not in session.requests[0][1]["json"]["tools"][0]["functionDeclarations"][0]


@pytest.mark.asyncio
async def test_gemini_no_argument_declaration_does_not_remove_past_function_result():
    session = _Session(_Response(_gemini("Prefere áudio.")))
    reply = await P._GeminiClient(session, "offline-placeholder").chat(
        system="s", messages=history(), temperature=.8, model="gemini-2.5-flash-lite",
        timeout_seconds=5, tool_specs=(READ,), allow_tool_calls=False)
    payload = session.requests[0][1]["json"]
    assert reply.text == "Prefere áudio." and not reply.tool_calls
    assert "parameters" not in payload["tools"][0]["functionDeclarations"][0]
    assert payload["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"
    assert payload["contents"][1] == {"role": "model", "parts": [
        {"functionCall": {"name": READ.name, "args": {}}}]}
    assert payload["contents"][2] == {"role": "user", "parts": [
        {"functionResponse": {"name": READ.name, "response": {"ok": True, "mode": "audio"}}}]}
    assert "confirmed_groq_call" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_groq_http_tool_validation_repairs_once_then_preserves_gemini_fallback(
        monkeypatch, caplog):
    monkeypatch.setattr(C, "GROQ_MODELS", ("openai/gpt-oss-120b", "openai/gpt-oss-20b"))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-2.5-flash", "gemini-2.5-flash-lite"))
    failure = {"error": {"code": "tool_use_failed", "type": "invalid_request_error",
                         "message": "Failed to call a function. Please adjust your prompt.",
                         "failed_generation": "PRIVATE_AUDIO_AND_UNTRUSTED_GENERATION"}}
    session = _Session(
        _Response({"error": {"code": "rate_limit_exceeded", "retry_after": 4}}, status=429),
        _Response(failure, status=400), _Response(failure, status=400),
        _Response(_gemini("Sua preferência é áudio.")))
    router = P.ProviderRouter(session, groq_key="offline-placeholder", gemini_key="offline-placeholder")
    report, repair_state = {}, {}
    with caplog.at_level(logging.INFO, logger="cogs.chatbot.providers"):
        reply = await router.chat(system="s", messages=history(), temperature=.8,
                                  tool_specs=(READ,), request_report=report, repair_state=repair_state)
    assert reply.provider == "gemini" and reply.model == "gemini-2.5-flash"
    assert reply.text == "Sua preferência é áudio." and not reply.tool_calls
    assert len(session.requests) == report["request_count"] == 4
    attempts = report["attempts"]
    assert [(item["provider"], item["model"], item["repair"]) for item in attempts] == [
        ("groq", "openai/gpt-oss-120b", False),
        ("groq", "openai/gpt-oss-20b", False),
        ("groq", "openai/gpt-oss-20b", True),
        ("gemini", "gemini-2.5-flash", False)]
    assert report["repair_used"] and repair_state["used"]
    assert all(item["kind"] == "invalid_response" and item["stage"] == "output"
               and item["diagnostic_code"] == "api_tool_validation" for item in attempts[1:3])
    for _url, request in session.requests[:3]:
        payload = request["json"]
        assert payload["messages"][-2]["tool_calls"][0]["id"] == "confirmed_groq_call"
        assert payload["messages"][-1]["tool_call_id"] == "confirmed_groq_call"
        assert json.loads(payload["messages"][-1]["content"]) == {"ok": True, "mode": "audio"}
    gemini = session.requests[-1][1]["json"]
    assert "parameters" not in gemini["tools"][0]["functionDeclarations"][0]
    assert gemini["contents"][-1]["parts"][0]["functionResponse"]["response"] == {"ok": True, "mode": "audio"}
    assert "PRIVATE_AUDIO_AND_UNTRUSTED_GENERATION" not in caplog.text
    assert "PRIVATE_AUDIO_AND_UNTRUSTED_GENERATION" not in json.dumps(report)
    assert "PRIVATE_AUDIO_AND_UNTRUSTED_GENERATION" not in json.dumps([
        request["json"] for _url, request in session.requests])


@pytest.mark.asyncio
@pytest.mark.parametrize("message,code", [
    ("parameters.properties: should be non-empty for OBJECT type", "api_tool_schema"),
    ("The number of function response parts should equal the number of function call parts", "api_tool_history"),
    ("Maximum context length exceeded", "api_context_limit"),
    ("Unsupported parameter: reasoning_effort", "api_parameter"),
    ("Unspecified invalid argument", "api_invalid_argument"),
])
async def test_http400_diagnostics_keep_safe_category_without_raw_error_message(message, code):
    error = await P._http_error(_Response({"error": {
        "status": "INVALID_ARGUMENT", "message": message + " PRIVATE_USER_TEXT",
        "failed_generation": "PRIVATE_GENERATION"}}, status=400), provider="gemini")
    assert error.kind == "request" and error.stage == "api"
    assert error.diagnostic_code == code and error.diagnostic_path == "$"
    assert error.diagnostic_tool == "" and error.diagnostic_index is None
    assert "PRIVATE" not in str(error) and "PRIVATE" not in json.dumps(vars(error))


@pytest.mark.asyncio
async def test_failed_generation_cannot_change_http400_error_category():
    error = await P._http_error(_Response({"error": {
        "message": "Invalid request", "code": "invalid_request_error",
        "failed_generation": "content_filter prohibited_content model_not_found tool_call_id "
                             "parameters.properties should be non-empty PRIVATE_GENERATION"}}, status=400), provider="groq")
    assert error.kind == "request" and error.diagnostic_code == "api_invalid_argument"
    assert "PRIVATE_GENERATION" not in str(error) and "PRIVATE_GENERATION" not in json.dumps(vars(error))


@pytest.mark.asyncio
async def test_mixed_quota_and_schema_failure_reports_request_rejection_without_losing_quota_cause(monkeypatch):
    monkeypatch.setattr(C, "GROQ_MODELS", ("openai/gpt-oss-120b", "openai/gpt-oss-20b"))
    monkeypatch.setattr(C, "GEMINI_MODELS", ("gemini-2.5-flash", "gemini-2.5-flash-lite"))
    bad_schema = {"error": {"status": "INVALID_ARGUMENT",
                            "message": "parameters.properties: should be non-empty for OBJECT type PRIVATE_PATH"}}
    session = _Session(
        _Response({"error": {"code": "rate_limit_exceeded", "retry_after": 4}}, status=429),
        _Response(bad_schema, status=400), _Response(bad_schema, status=400),
        _Response(bad_schema, status=400))
    router = P.ProviderRouter(session, groq_key="offline-placeholder", gemini_key="offline-placeholder")
    report = {}
    with pytest.raises(P.AllProvidersExhausted) as exhausted:
        await router.chat(system="s", messages=history(), tool_specs=(READ,), request_report=report)
    error = exhausted.value
    assert error.kind == "request" and error.status == 400 and error.stage == "api"
    assert error.diagnostic_code == "api_tool_schema" and error.retry_after is None
    assert len(session.requests) == report["request_count"] == 4
    assert [cause["kind"] for cause in error.causes] == ["rate_limit", "request", "request", "request"]
    assert error.causes[0]["status"] == 429 and error.causes[0]["retry_after"] == 4
    assert not report.get("repair_used")
    assert "PRIVATE_PATH" not in str(error) and "PRIVATE_PATH" not in json.dumps(report)
