"""@mcp_tool registers JSON Schema types, not Python type names (issue #362)."""

from __future__ import annotations

import enum
import json
from typing import Any, Literal, Optional

import pytest

from b2a_sdk import decorators
from b2a_sdk.decorators import mcp_tool

JSON_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}


def _all_type_values(schema: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(schema, dict):
        if "type" in schema:
            found.append(schema["type"])
        for value in schema.values():
            found.extend(_all_type_values(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_all_type_values(item))
    return found


@pytest.fixture
def registrations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(decorators, "_registration_callbacks", [])

    def record(service_id: str, func: Any, input_schema: Any, output_schema: Any) -> None:
        captured.append({"service_id": service_id, "input_schema": input_schema})

    decorators.register_mcp_tool_callback(record)
    return captured


def test_example_tool_schema_uses_json_schema_types(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="text-analyzer")
    async def analyze_text(text: str, operation: str = "summarize", max_length: int = 100) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    assert schema == {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "operation": {"type": "string"},
            "max_length": {"type": "integer"},
        },
        "required": ["text"],
    }


class Mode(enum.Enum):
    FAST = "fast"
    SLOW = "slow"


def test_common_annotations_map_to_valid_json_schema(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="many-types")
    async def tool(
        ratio: float,
        enabled: bool,
        tags: list[str],
        pair: tuple[int, str],
        counts: dict[str, int],
        note: Optional[str] = None,  # noqa: UP045 - the typing form is what users write
        limit: int | None = None,
        mode: Mode = Mode.FAST,
        level: Literal["low", "high"] = "low",
        extra: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        return {}

    props = registrations[0]["input_schema"]["properties"]
    assert props["ratio"] == {"type": "number"}
    assert props["enabled"] == {"type": "boolean"}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["pair"] == {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "string"}],
        "minItems": 2,
        "maxItems": 2,
    }
    assert props["counts"] == {"type": "object", "additionalProperties": {"type": "integer"}}
    assert props["note"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert props["limit"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert props["mode"] == {"enum": ["fast", "slow"]}
    assert props["level"] == {"enum": ["low", "high"]}
    assert props["extra"] == {}
    assert "args" not in props and "kwargs" not in props
    assert set(_all_type_values(props)) <= JSON_SCHEMA_TYPES


def test_unannotated_parameter_stays_string(registrations: list[dict[str, Any]]) -> None:
    @mcp_tool(service_id="untyped")
    async def tool(value):  # type: ignore[no-untyped-def]
        return value

    assert registrations[0]["input_schema"]["properties"] == {"value": {"type": "string"}}


class Blob(enum.Enum):
    RAW = b"raw"


def test_literal_enum_members_become_their_values(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="literal-enum")
    async def tool(
        mode: Literal[Mode.FAST],
        mixed: Literal[Mode.SLOW, "auto", 3, None] = "auto",
    ) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    assert schema["properties"]["mode"] == {"enum": ["fast"]}
    assert schema["properties"]["mixed"] == {"enum": ["slow", "auto", 3, None]}
    assert json.loads(json.dumps(schema)) == schema


def test_values_json_cannot_hold_fall_back_to_any(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="non-json-values")
    async def tool(blob: Blob, raw: Literal[b"raw"], member: Literal[Blob.RAW]) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    assert schema["properties"] == {"blob": {}, "raw": {}, "member": {}}
    assert json.loads(json.dumps(schema)) == schema
