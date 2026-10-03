"""O catálogo só anuncia adapters explícitos e valida cada argumento no host."""
from types import SimpleNamespace

import pytest

from cogs.chatbot.tool_registry import InvalidToolArguments, ToolRegistry, ToolSpec, validate_tool_arguments


def spec(name="get_state", **kwargs):
    return ToolSpec(name, "Consulte o estado real; o resultado confirma disponibilidade.",
                    {"type": "object", "properties": {}, "additionalProperties": False}, **kwargs)


def test_adapter_exports_only_explicit_catalog_and_can_unload():
    adapter = SimpleNamespace(chatbot_tool_specs=lambda: (spec(),),
                              ban_everyone=lambda: pytest.fail("Método interno não pode ser exportado"))
    registry = ToolRegistry()
    registry.register_adapter(adapter, owner="example")
    registry.register(spec("get_preferences"), owner="preferences")
    assert [item.name for item in registry.get_specs()] == ["get_state", "get_preferences"]
    assert registry.get("ban_everyone") is None
    registry.unregister_owner("example")
    assert registry.get("get_state") is None
    assert registry.get("get_preferences") is not None


def test_duplicate_registration_does_not_replace_existing_handler():
    original = spec(handler=lambda args: {"ok": True})
    registry = ToolRegistry((original,))
    with pytest.raises(ValueError):
        registry.register(spec(), owner="other")
    assert registry.get("get_state") is original


def test_availability_and_summary_use_snapshot_without_executing_handler():
    handler = lambda args: pytest.fail("Resumo não pode executar ferramenta")
    registry = ToolRegistry((spec(handler=handler, availability=lambda context: (context["connected"], "Bot fora da call.")),))
    assert registry.get_specs({"connected": False}) == ()
    unavailable = registry.summary({"connected": False})
    assert "indisponível. Bot fora da call." in unavailable
    available = registry.summary({"connected": True})
    assert "Consulte o estado real" in available
    assert "nunca peça códigos internos" in available
    assert "Só confirme um efeito depois do resultado real" in available


def test_failed_availability_does_not_leak_private_exception():
    def fail(context):
        raise RuntimeError("private operational details")
    registry = ToolRegistry((spec(availability=fail),))
    assert registry.get_specs() == ()
    assert "private" not in registry.summary()


def test_declaration_does_not_mutate_host_validation_schema():
    schema = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["audio", "text"]}},
              "required": ["mode"], "additionalProperties": False}
    tool = ToolSpec("set_preferences", "Defina preferências próprias.", schema)
    schema["properties"]["mode"]["enum"].append("invalid")
    declaration = tool.native_declaration()
    declaration["parameters"].pop("additionalProperties")
    assert tool.parameters["additionalProperties"] is False
    with pytest.raises(InvalidToolArguments):
        validate_tool_arguments({"mode": "invalid"}, tool.parameters)


@pytest.mark.parametrize("arguments", [
    {"count": True, "mode": "audio"}, {"count": 26, "mode": "audio"},
    {"count": 1, "mode": "english"}, {"count": 1},
    {"count": 1, "mode": "audio", "target_id": 123},
    {"count": 1, "mode": "audio", "options": {"silent_ban": True}},
    {"count": 1, "mode": "audio", "options": {"refs": ["x"] * 3}},
    {"count": 1, "mode": "audio", "options": {"refs": ["\x00"]}},
    {"count": 1, "mode": "audio", "options": {"refs": ["x" * 8193]}},
    {"count": float("nan"), "mode": "audio"},
])
def test_argument_validation_rejects_bounds_types_and_undeclared_fields(arguments):
    schema = {"type": "object", "additionalProperties": False, "required": ["count", "mode"], "properties": {
        "count": {"type": "integer", "minimum": 1, "maximum": 25},
        "mode": {"type": "string", "enum": ["audio", "text"]},
        "options": {"type": "object", "additionalProperties": False, "properties": {
            "refs": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 32}},
        }},
    }}
    with pytest.raises(InvalidToolArguments):
        validate_tool_arguments(arguments, schema)


def test_validated_arguments_are_isolated_from_api_object():
    source = {"refs": ["m1"]}
    result = validate_tool_arguments(source, {"type": "object", "properties": {
        "refs": {"type": "array", "items": {"type": "string"}},
    }})
    source["refs"].append("m2")
    assert result == {"refs": ["m1"]}
