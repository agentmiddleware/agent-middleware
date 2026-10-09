"""Focused unit tests for app.services.service_registry (in-memory paths only)."""

from typing import Any, Optional

import pytest
from pydantic import BaseModel

from app.schemas.billing import ServiceCategory
from app.services.service_registry import (
    ServiceRegistry,
    _annotation_to_json_schema,
    _service_to_mcp_tool,
    extract_schema_from_callable,
    get_service_registry,
    pydantic_to_mcp_schema,
)


class EchoInput(BaseModel):
    message: str
    count: int = 1


class EchoOutput(BaseModel):
    reply: str


def _local_kwargs(service_id="tool-echo", **overrides):
    def handler(message: str, count: int = 1) -> str:
        return f"{message}:{count}"

    kwargs = {
        "service_id": service_id,
        "name": "Echo",
        "description": "Echoes a message",
        "category": ServiceCategory.SANDBOX,
        "func": handler,
    }
    kwargs.update(overrides)
    return kwargs


def test_pydantic_to_mcp_schema_none_returns_none():
    assert pydantic_to_mcp_schema(None) is None


def test_pydantic_to_mcp_schema_model():
    schema = pydantic_to_mcp_schema(EchoInput)
    assert schema is not None
    assert schema["type"] == "object"
    assert "message" in schema["properties"]
    assert "message" in schema["required"]
    assert "count" not in schema["required"]
    assert schema["additionalProperties"] is False


def test_annotation_to_json_schema_primitives_and_optional():
    assert _annotation_to_json_schema(str) == {"type": "string"}
    assert _annotation_to_json_schema(int) == {"type": "integer"}
    assert _annotation_to_json_schema(Any) == {}
    assert _annotation_to_json_schema(dict[str, int]) == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }
    assert _annotation_to_json_schema(list[str]) == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert _annotation_to_json_schema(Optional[int]) == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }


def test_annotation_to_json_schema_unknown_returns_none():
    class Unknown:
        pass

    assert _annotation_to_json_schema(Unknown) is None


def test_extract_schema_from_callable_basic():
    def handler(name: str, count: int = 2) -> str:
        return name * count

    input_schema, output_schema = extract_schema_from_callable(handler)
    assert input_schema is not None
    assert input_schema["properties"]["name"] == {"type": "string"}
    assert input_schema["required"] == ["name"]
    assert output_schema == {"type": "string"}


def test_extract_schema_from_callable_no_params_returns_none_input():
    def handler() -> None:
        return None

    input_schema, output_schema = extract_schema_from_callable(handler)
    assert input_schema is None
    assert output_schema is None


def test_register_local_uses_models_and_lookup_helpers():
    registry = ServiceRegistry()
    record = registry.register_local(
        **_local_kwargs(input_model=EchoInput, output_model=EchoOutput)
    )
    assert record["service_id"] == "tool-echo"
    assert record["category"] == ServiceCategory.SANDBOX.value
    assert record["is_local"] is True
    assert record["is_executable"] is True
    assert "message" in (record["input_schema"] or {})["properties"]
    assert "reply" in (record["output_schema"] or {})["properties"]
    assert registry.get_local("tool-echo") is record
    assert callable(registry.get_local_func("tool-echo"))
    assert registry.get_executor("tool-echo") is None


def test_register_local_derives_schema_from_callable_without_models():
    registry = ServiceRegistry()
    record = registry.register_local(**_local_kwargs(service_id="tool-derived"))
    assert record["input_schema"] is not None
    assert "message" in record["input_schema"]["properties"]
    assert record["output_schema"] == {"type": "string"}


def test_register_local_unknown_returns_none():
    registry = ServiceRegistry()
    assert registry.get_local("missing") is None
    assert registry.get_local_func("missing") is None
    assert registry.get_executor("missing") is None


async def test_get_prefers_local_without_touching_db():
    registry = ServiceRegistry()
    registry.register_local(**_local_kwargs(service_id="tool-cached"))

    def _boom():
        raise AssertionError("DB session factory must not be used for local hits")

    registry._session_factory = _boom
    assert (await registry.get("tool-cached"))["service_id"] == "tool-cached"
    assert await registry.get_pricing("tool-cached") == (1.0, "call")


async def test_list_local_strips_func_and_list_executable_filters():
    registry = ServiceRegistry()
    registry.register_local(**_local_kwargs(service_id="tool-a"))
    registry.register_local(
        **_local_kwargs(service_id="tool-b", category=ServiceCategory.ORACLE)
    )
    local = await registry.list_local()
    assert {s["service_id"] for s in local} == {"tool-a", "tool-b"}
    assert all("func" not in s for s in local)

    executable = await registry.list_executable(category=ServiceCategory.ORACLE)
    assert [s["service_id"] for s in executable] == ["tool-b"]

    filtered = await registry.list_executable(category=ServiceCategory.RED_TEAM)
    assert filtered == []


def test_to_mcp_tool_annotations_and_defaults():
    service = {
        "service_id": "tool-annotated",
        "description": "desc",
        "credits_per_unit": 2.5,
        "unit_name": "run",
        "category": "sandbox",
        "input_schema": None,
        "output_schema": {"type": "object"},
        "owner_wallet_id": "wallet-1",
        "require_permit": True,
        "is_local": False,
        "external": True,
    }
    tool = _service_to_mcp_tool(service)
    assert tool["name"] == "tool-annotated"
    assert tool["inputSchema"] == {"type": "object", "properties": {}}
    assert tool["annotations"]["creditsPerCall"] == 2.5
    assert tool["annotations"]["providerWallet"] == "wallet-1"
    assert tool["annotations"]["requirePermit"] is True
    assert tool["annotations"]["hasOutputSchema"] is True
    assert tool["annotations"]["external"] is True

    registry = ServiceRegistry()
    assert registry.to_mcp_tool(service) == tool


def test_unregister_local_true_and_false():
    registry = ServiceRegistry()
    registry.register_local(**_local_kwargs(service_id="tool-gone"))
    assert registry.unregister_local("tool-gone") is True
    assert registry.get_local("tool-gone") is None
    assert registry.unregister_local("tool-gone") is False


def test_unregister_execution_backend_removes_only_matching():
    registry = ServiceRegistry()
    registry.register_local(**_local_kwargs(service_id="tool-local"))
    executor = object()
    registry.register_upstream(
        service_id="tool-remote",
        name="Remote",
        description="remote tool",
        category=ServiceCategory.SANDBOX,
        executor=executor,
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        credits_per_unit=1.0,
        upstream_tool_name="remote.tool",
        upstream_origin="https://example.invalid",
    )
    assert registry.get_executor("tool-remote") is executor
    removed = registry.unregister_execution_backend("upstream_mcp")
    assert removed == 1
    assert registry.get_local("tool-remote") is None
    assert registry.get_local("tool-local") is not None


def test_register_upstream_rejects_local_id_collision():
    registry = ServiceRegistry()
    registry.register_local(**_local_kwargs(service_id="tool-dup"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register_upstream(
            service_id="tool-dup",
            name="Remote",
            description="remote tool",
            category=ServiceCategory.SANDBOX,
            executor=object(),
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
            credits_per_unit=1.0,
            upstream_tool_name="remote.tool",
            upstream_origin="https://example.invalid",
        )


def test_get_service_registry_singleton():
    first = get_service_registry()
    second = get_service_registry()
    assert first is second
