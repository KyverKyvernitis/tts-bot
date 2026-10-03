"""Catálogo explícito de ferramentas, descrições e disponibilidade operacional."""
from __future__ import annotations

import inspect
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
MAX_TOOL_ARGUMENT_BYTES = 8192


class InvalidToolArguments(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    permission: str = "read"
    available: bool = True
    why: str = ""
    handler: Callable | None = field(default=None, repr=False, compare=False)
    availability: Callable | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not _NAME.fullmatch(self.name) or not isinstance(self.description, str):
            raise ValueError("Ferramenta inválida.")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise ValueError("Os argumentos da ferramenta precisam de schema object.")
        object.__setattr__(self, "parameters", deepcopy(self.parameters))

    def native_declaration(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": deepcopy(self.parameters)}


class ToolRegistry:
    def __init__(self, specs=()):
        self._specs: dict[str, ToolSpec] = {}
        self._owners: dict[str, str] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec, *, owner: str = "core") -> ToolSpec:
        if not isinstance(spec, ToolSpec) or spec.name in self._specs:
            raise ValueError("Ferramenta ausente ou já registrada.")
        self._specs[spec.name], self._owners[spec.name] = spec, owner
        return spec

    def register_adapter(self, adapter, *, owner: str):
        # Somente um catálogo declarado pelo adapter é exportado. Métodos
        # internos e comandos do cog nunca viram ferramentas por reflexão.
        for spec in adapter.chatbot_tool_specs():
            self.register(spec, owner=owner)

    def unregister_owner(self, owner: str):
        for name in tuple(self._specs):
            if self._owners[name] == owner:
                self._specs.pop(name)
                self._owners.pop(name)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def snapshot(self, context=None) -> tuple[ToolSpec, ...]:
        specs = []
        for spec in self._specs.values():
            if spec.availability is not None:
                try:
                    result = spec.availability(context)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise ValueError("Disponibilidade precisa de snapshot síncrono.")
                    enabled, why = result if isinstance(result, tuple) and len(result) == 2 else (bool(result), spec.why)
                    spec = replace(spec, available=bool(enabled), why=str(why or ""))
                except Exception:
                    spec = replace(spec, available=False, why="Disponibilidade não confirmada neste turno.")
            specs.append(spec)
        return tuple(specs)

    def get_specs(self, context=None) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec in self.snapshot(context) if spec.available)

    def summary(self, context=None) -> str:
        return tool_summary(self.snapshot(context))


def tool_summary(specs) -> str:
    lines = [
        "Ferramentas reais deste turno. Escolha quando forem úteis à conversa e use chamadas nativas.",
        "Parâmetros e referências internas são resolvidos pelo sistema; nunca peça códigos internos ao usuário.",
        "Só confirme um efeito depois do resultado real. Pedidos à staff aguardam aprovação; não concedem permissão.",
    ]
    for spec in specs:
        if not spec.available:
            lines.append(f"{spec.name}: indisponível. {spec.why[:300]}")
            continue
        required = spec.parameters.get("required", [])
        arguments = ", ".join(required) if required else "nenhum obrigatório"
        lines.append(f"{spec.name}: {spec.description[:1000]} Requisitos: {spec.permission}. Argumentos obrigatórios: {arguments}.")
    return "\n".join(lines)


def validate_tool_arguments(arguments: Any, schema: dict) -> dict:
    if not isinstance(arguments, dict):
        raise InvalidToolArguments("Argumentos inválidos.")
    try:
        if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise InvalidToolArguments("Argumentos inválidos.")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InvalidToolArguments("Argumentos inválidos.") from exc

    def check(value, node, depth=0):
        if depth > 12 or not isinstance(node, dict):
            raise InvalidToolArguments("Argumentos inválidos.")
        kind = node.get("type")
        valid = {
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "null": value is None,
        }
        if kind and not (any(valid.get(item, False) for item in kind) if isinstance(kind, list) else valid.get(kind, False)):
            raise InvalidToolArguments("Argumentos inválidos.")
        if "enum" in node and value not in node["enum"]:
            raise InvalidToolArguments("Argumentos inválidos.")
        if isinstance(value, dict):
            properties = node.get("properties", {})
            if any(key not in value for key in node.get("required", [])):
                raise InvalidToolArguments("Argumentos inválidos.")
            if node.get("additionalProperties") is False and set(value) - set(properties):
                raise InvalidToolArguments("Argumentos inválidos.")
            for key, item in value.items():
                if key in properties:
                    check(item, properties[key], depth + 1)
        elif isinstance(value, list):
            if len(value) > node.get("maxItems", 100) or len(value) < node.get("minItems", 0):
                raise InvalidToolArguments("Argumentos inválidos.")
            for item in value:
                check(item, node.get("items", {}), depth + 1)
        elif isinstance(value, str):
            if "\x00" in value or len(value) > node.get("maxLength", MAX_TOOL_ARGUMENT_BYTES) or len(value) < node.get("minLength", 0):
                raise InvalidToolArguments("Argumentos inválidos.")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < node.get("minimum", float("-inf")) or value > node.get("maximum", float("inf")):
                raise InvalidToolArguments("Argumentos inválidos.")
        for option in node.get("anyOf", []):
            try:
                check(value, option, depth + 1)
                break
            except InvalidToolArguments:
                pass
        else:
            if node.get("anyOf"):
                raise InvalidToolArguments("Argumentos inválidos.")

    check(arguments, schema)
    return deepcopy(arguments)
