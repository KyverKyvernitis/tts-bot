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
    """Erro público genérico com metadados derivados do schema, nunca do valor."""

    def __init__(self, message="Argumentos inválidos.", *, code="invalid_arguments", path="$"):
        super().__init__(message)
        self.code = code
        self.path = path


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
    capabilities: tuple[str, ...] = ()

    def __post_init__(self):
        if not _NAME.fullmatch(self.name) or not isinstance(self.description, str):
            raise ValueError("Ferramenta inválida.")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise ValueError("Os argumentos da ferramenta precisam de schema object.")
        object.__setattr__(self, "parameters", deepcopy(self.parameters))
        if (not isinstance(self.capabilities, (tuple, list))
                or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in self.capabilities)):
            raise ValueError("As capacidades precisam de nomes declarados válidos.")
        capabilities = tuple(dict.fromkeys(self.capabilities))
        object.__setattr__(self, "capabilities", capabilities)

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

    def capability_index(self, *, exclude_names=()) -> str:
        """Índice compacto das funções ainda não descritas por schemas nativos.

        ``exclude_names`` não avalia availability e portanto não transforma o
        catálogo em autorização. A rodada já recebe os contratos completos das
        funções carregadas; repetir seus resumos aqui só desperdiça entrada.
        """
        excluded = set(exclude_names or ())
        lines = [
            "Índice de outras ferramentas reais do bot; contratos nativos presentes já estão carregados.",
            ("Para uma função deste índice, use carregar_ferramentas antes de chamá-la."
             if "carregar_ferramentas" in self._specs else "Use apenas contratos nativos presentes nesta rodada."),
            "Não peça códigos internos; só confirme efeitos após resultado real; staff aprova ações privilegiadas.",
        ]
        # Sem avaliar availability: nomes/descritivos são catálogo publicado, não
        # promessa de disponibilidade. Descrições carregadas ficam nos schemas.
        count = 0
        for spec in self._specs.values():
            if spec.name in excluded:
                continue
            description = re.split(r"(?<=[.!?])\s+", " ".join(spec.description.split()), maxsplit=1)[0]
            description = description if len(description) <= 76 else description[:73].rstrip() + "..."
            suffix = f" [{spec.permission}]"
            actions = spec.capabilities or spec.parameters.get("properties", {}).get("action", {}).get("enum", ())
            if actions:
                suffix += " ações=" + ",".join(actions)
            lines.append(f"{spec.name}: {description}{suffix}")
            count += 1
        if not count:
            return "Contratos nativos presentes cobrem as ferramentas carregadas desta rodada."
        return "\n".join(lines)


def tool_summary(specs) -> str:
    lines = [
        "Ferramentas reais deste turno. Escolha quando forem úteis à conversa e use chamadas nativas.",
        "Parâmetros e referências internas são resolvidos pelo sistema; nunca peça códigos internos ao usuário.",
        "Só confirme um efeito depois do resultado real. Pedidos à staff aguardam aprovação; não concedem permissão.",
        "As declarações nativas trazem o contrato completo; este catálogo resume função, disponibilidade e requisitos.",
    ]
    for spec in specs:
        if not spec.available:
            lines.append(f"{spec.name}: indisponível. {spec.why[:300]}")
            continue
        required = spec.parameters.get("required", [])
        arguments = ", ".join(required) if required else "nenhum obrigatório"
        # O contrato detalhado já segue na declaração nativa. Repeti-lo no
        # system prompt consome tokens em todo turno e em cada rodada de tools.
        description = re.split(r"(?<=[.!?])\s+", " ".join(spec.description.split()), maxsplit=1)[0]
        if len(description) > 160:
            description = description[:157].rstrip() + "..."
        lines.append(f"{spec.name}: {description} Requisitos: {spec.permission}. Argumentos obrigatórios: {arguments}.")
        actions = spec.parameters.get("properties", {}).get("action", {}).get("enum", ())
        if actions:
            lines.append("Ações disponíveis nesta ferramenta (valores de action, não nomes de ferramentas): " + ", ".join(actions) + ".")
    return "\n".join(lines)


def validate_tool_arguments(arguments: Any, schema: dict) -> dict:
    if not isinstance(arguments, dict):
        raise InvalidToolArguments(code="type")
    try:
        argument_size = len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InvalidToolArguments(code="serialization") from exc
    if argument_size > MAX_TOOL_ARGUMENT_BYTES:
        raise InvalidToolArguments(code="argument_size")

    def child_path(path, key):
        # key somente vem de properties/required do schema do host. Nunca
        # inclua os nomes de campos extras escritos pelo modelo nos logs.
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")

    def fail(code, path):
        raise InvalidToolArguments(code=code, path=path)

    def check(value, node, depth=0, path="$"):
        if depth > 12:
            fail("max_depth", path)
        if not isinstance(node, dict):
            fail("invalid_schema", path)
        kind = node.get("type")
        valid = {
            "object": isinstance(value, dict), "array": isinstance(value, list),
            "string": isinstance(value, str), "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "null": value is None,
        }
        if kind and not (any(valid.get(item, False) for item in kind) if isinstance(kind, list) else valid.get(kind, False)):
            fail("type", path)
        if "enum" in node and value not in node["enum"]:
            fail("enum", path)
        if isinstance(value, dict):
            properties = node.get("properties", {})
            for key in node.get("required", []):
                if key not in value:
                    fail("required", child_path(path, key))
            if node.get("additionalProperties") is False and set(value) - set(properties):
                fail("additional_properties", path)
            for key, item in value.items():
                if key in properties:
                    check(item, properties[key], depth + 1, child_path(path, key))
        elif isinstance(value, list):
            if len(value) > node.get("maxItems", 100):
                fail("max_items", path)
            if len(value) < node.get("minItems", 0):
                fail("min_items", path)
            for item in value:
                check(item, node.get("items", {}), depth + 1, path + "/*")
        elif isinstance(value, str):
            if "\x00" in value:
                fail("null_character", path)
            if len(value) > node.get("maxLength", MAX_TOOL_ARGUMENT_BYTES):
                fail("max_length", path)
            if len(value) < node.get("minLength", 0):
                fail("min_length", path)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < node.get("minimum", float("-inf")):
                fail("minimum", path)
            if value > node.get("maximum", float("inf")):
                fail("maximum", path)
        for option in node.get("anyOf", []):
            try:
                check(value, option, depth + 1, path)
                break
            except InvalidToolArguments:
                pass
        else:
            if node.get("anyOf"):
                fail("any_of", path)

    check(arguments, schema)
    return deepcopy(arguments)
