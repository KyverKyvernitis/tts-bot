"""Contratos OpenAI tool calling: testes sem Flask, discord ou rede."""
from __future__ import annotations

import pytest

from utility.openai_compat_tools import (
    ToolPayloadError, external_response_calls, normalize_messages, normalize_tools,
)


TOOLS = [{"type": "function", "function": {
    "name": "calculator", "description": "Compute", "parameters": {
        "type": "object", "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
}}]


def test_schema_auto_and_none():
    items, enabled = normalize_tools(TOOLS, "auto")
    assert enabled
    assert items[0]["name"] == "calculator"
    assert normalize_tools(TOOLS, "none")[1] is False
    assert normalize_tools([], "auto") == ([], False)


@pytest.mark.parametrize("tools,choice,code", [
    (TOOLS, "required", "unsupported_tool_choice"),
    (TOOLS * 20, "auto", "invalid_tools"),
    ([{"type": "function", "function": {"name": "admin-bad"}}], "auto", "invalid_tools"),
    ([{"type": "function", "function": {"name": "calculator", "parameters": {"type": "string"}}}], "auto", "invalid_tools"),
    ([{"type": "function", "function": {"name": "calculator", "parameters": {"type": "object", "properties": {"x": {"$ref": "evil"}}}}}], "auto", "invalid_tools"),
])
def test_reject_unsupported_tools(tools, choice, code):
    with pytest.raises(ToolPayloadError) as raised:
        normalize_tools(tools, choice)
    assert raised.value.code == code


def test_history_roundtrip_tool_reply():
    messages = [
        {"role": "user", "content": "2+3"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_5", "type": "function", "function": {"name": "calculator", "arguments": '{"expression":"2+3"}'}}]},
        {"role": "tool", "tool_call_id": "call_5", "content": "5"},
    ]
    output = normalize_messages(messages)
    assert output[1]["tool_calls"][0]["arguments"] == {"expression": "2+3"}
    assert output[2]["name"] == "calculator"


@pytest.mark.parametrize("tool_reply", [
    [{"role": "user", "content": "x"}, {"role": "tool", "content": "5", "tool_call_id": "missing"}],
    [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "calculator", "arguments": "{}"}}]},
     {"role": "tool", "tool_call_id": "call_b", "content": "5"}],
    [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "calculator", "arguments": '{"x":1,"x":2}'}}]},
     {"role": "tool", "tool_call_id": "call_a", "content": "5"}],
])
def test_reject_spoofed_or_invalid_history(tool_reply):
    with pytest.raises(ToolPayloadError):
        normalize_messages(tool_reply)


def test_external_calls_can_only_name_client_declared_tools():
    from types import SimpleNamespace
    good = [SimpleNamespace(id="call_5", name="calculator", arguments={"expression": "2+3"})]
    result = external_response_calls(good, {"calculator"})
    assert result == [{"id": "call_5", "type": "function",
                       "function": {"name": "calculator", "arguments": '{"expression":"2+3"}'}}]
    with pytest.raises(ToolPayloadError):
        external_response_calls(good, {"discord_ban"})
