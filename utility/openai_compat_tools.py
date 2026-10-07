"""Tool calling OpenAI compatível: somente contratos externos, sem execução no bot.

Contratos declarados pelo cliente são entregues ao ProviderRouter como ToolSpec
sem handler. Resultados de chamadas são devolvidos ao cliente, que é o único
responsável por executar (e autorizar) as ferramentas do Off Grid AI.
"""
from __future__ import annotations

import json
import re
from typing import Any


_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_MAX_TOOLS = 16
_MAX_CALLS_PER_TURN = 8
_MAX_HISTORY_CALLS = 64
_MAX_TOOL_BYTES = 32768
_MAX_ARGUMENT_BYTES = 8192


class ToolPayloadError(ValueError):
    def __init__(self, code: str):
        super().__init__("Invalid tool calling payload.")
        self.code = code


def _json_size(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ToolPayloadError("invalid_tools") from exc


def _schema_safe(value: Any, depth: int = 0) -> bool:
    """Rejeita referências/ciclos de schema e payloads muito profundos.

    JSON Schema completo não pode ser assumido: o validador do chatbot suporta
    os tipos/propriedades comuns. Conservar apenas contratos que ele compreende.
    """
    if depth > 10:
        return False
    if isinstance(value, dict):
        if any(key in value for key in ("$ref", "$defs", "definitions", "patternProperties", "oneOf", "allOf", "not")):
            return False
        return all(isinstance(key, str) and len(key) <= 100 and _schema_safe(val, depth + 1)
                   for key, val in value.items())
    if isinstance(value, list):
        return len(value) <= 128 and all(_schema_safe(item, depth + 1) for item in value)
    return value is None or isinstance(value, (str, int, float, bool)) and (
        not isinstance(value, str) or len(value) <= 4096
    )


def normalize_tools(tools: Any, tool_choice: Any = None) -> tuple[list[dict[str, Any]], bool]:
    """Retorna declarações limpas e se novas chamadas estão habilitadas."""
    if tools is None:
        tools = []
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        raise ToolPayloadError("invalid_tools")
    if tool_choice not in (None, "auto", "none"):
        # Não fingir honrar required ou uma escolha forçada que o router não suporta.
        raise ToolPayloadError("unsupported_tool_choice")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ToolPayloadError("invalid_tools")
        func = tool.get("function")
        if not isinstance(func, dict):
            raise ToolPayloadError("invalid_tools")
        name = func.get("name")
        desc = func.get("description", "")
        schema = func.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in seen:
            raise ToolPayloadError("invalid_tools")
        if not isinstance(desc, str) or len(desc) > 2048:
            raise ToolPayloadError("invalid_tools")
        if not isinstance(schema, dict) or schema.get("type") != "object" or not _schema_safe(schema):
            raise ToolPayloadError("invalid_tools")
        if _json_size(schema) > _MAX_TOOL_BYTES:
            raise ToolPayloadError("invalid_tools")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or len(properties) > 64 or not isinstance(required, list):
            raise ToolPayloadError("invalid_tools")
        if not all(isinstance(key, str) and len(key) <= 100 for key in properties):
            raise ToolPayloadError("invalid_tools")
        if not all(isinstance(key, str) and key in properties for key in required):
            raise ToolPayloadError("invalid_tools")
        seen.add(name)
        normalized.append({"name": name, "description": desc, "parameters": schema})
    if _json_size(normalized) > 96_000:
        raise ToolPayloadError("invalid_tools")
    return normalized, bool(normalized and tool_choice != "none")


def _strict_json_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ToolPayloadError("invalid_tool_arguments")
        def no_duplicates(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise ToolPayloadError("invalid_tool_arguments")
                values[key] = value
            return values
        def no_constants(_):
            raise ToolPayloadError("invalid_tool_arguments")
        try:
            raw = json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=no_constants)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ToolPayloadError("invalid_tool_arguments") from exc
    if not isinstance(raw, dict) or _json_size(raw) > _MAX_ARGUMENT_BYTES:
        raise ToolPayloadError("invalid_tool_arguments")
    return raw


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Confere a ordem/identidade do histórico sem executar ferramentas.

    Ferramentas antigas podem estar ausentes das declarações deste turno se o
    usuário as desligou no app; isso não autoriza novas chamadas.
    """
    normalized: list[dict[str, Any]] = []
    pending: dict[str, str] = {}
    call_count = 0
    for msg in messages:
        role = msg["role"]
        if pending and role != "tool":
            raise ToolPayloadError("invalid_tool_history")
        if role == "assistant" and msg.get("tool_calls") is not None:
            calls = msg["tool_calls"]
            if not isinstance(calls, list) or not 1 <= len(calls) <= _MAX_CALLS_PER_TURN:
                raise ToolPayloadError("invalid_tool_history")
            call_count += len(calls)
            if call_count > _MAX_HISTORY_CALLS:
                raise ToolPayloadError("invalid_tool_history")
            structured = []
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ToolPayloadError("invalid_tool_history")
                func = call.get("function")
                call_id = call.get("id")
                name = func.get("name") if isinstance(func, dict) else None
                if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 200
                        or call_id in pending or not isinstance(name, str) or not _NAME.fullmatch(name)):
                    raise ToolPayloadError("invalid_tool_history")
                args = _strict_json_arguments(func.get("arguments"))
                pending[call_id] = name
                structured.append({"id": call_id, "name": name, "arguments": args})
            normalized.append({**msg, "tool_calls": structured})
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ToolPayloadError("invalid_tool_history")
            actual_name = pending.pop(call_id)
            provided_name = msg.get("name")
            if provided_name is not None and provided_name != actual_name:
                raise ToolPayloadError("invalid_tool_history")
            normalized.append({**msg, "name": actual_name})
        else:
            normalized.append(msg)
    if pending:
        raise ToolPayloadError("invalid_tool_history")
    return normalized


def external_response_calls(calls: Any, names: set[str]) -> list[dict[str, Any]]:
    if not isinstance(calls, (tuple, list)) or len(calls) > _MAX_CALLS_PER_TURN:
        raise ToolPayloadError("invalid_backend_tools")
    result = []
    ids = set()
    for call in calls:
        call_id, name, args = getattr(call, "id", None), getattr(call, "name", None), getattr(call, "arguments", None)
        if (not isinstance(call_id, str) or not 1 <= len(call_id) <= 200 or call_id in ids
                or not isinstance(name, str) or name not in names):
            raise ToolPayloadError("invalid_backend_tools")
        if not isinstance(args, dict) or _json_size(args) > _MAX_ARGUMENT_BYTES:
            raise ToolPayloadError("invalid_backend_tools")
        ids.add(call_id)
        result.append({"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        }})
    return result
