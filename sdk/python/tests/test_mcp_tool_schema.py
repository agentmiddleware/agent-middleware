"""@mcp_tool registers JSON Schema types, not Python type names (issue #362)."""

from __future__ import annotations

import enum
import json
from typing import Any, Literal, Optional, cast

import pytest

try:
    import pydantic
except ImportError:  # the SDK does not depend on pydantic
    pydantic = None  # type: ignore[assignment]

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


def _resolve(schema: dict[str, Any], ref: str) -> Any:
    assert ref.startswith("#/")
    node: Any = schema
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _all_refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_all_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_all_refs(item))
    return found


class _ItemHolder:
    """Stands in for a model class: exposes the schema hook @mcp_tool reads."""

    value_type = "integer"

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        return {
            "$defs": {
                "Item": {
                    "type": "object",
                    "properties": {"value": {"type": cls.value_type}},
                    "required": ["value"],
                }
            },
            "type": "object",
            "properties": {"item": {"$ref": "#/$defs/Item"}},
            "required": ["item"],
        }


class _TextItemHolder(_ItemHolder):
    value_type = "string"


def test_nested_model_refs_resolve_from_the_schema_root(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="nested-models")
    async def tool(
        owner: _ItemHolder,
        backup: _ItemHolder | None = None,
        team: list[_ItemHolder] = [],  # noqa: B006 - the default is never mutated
        pair: tuple[_ItemHolder, int] | None = None,
        by_name: dict[str, _ItemHolder] = {},  # noqa: B006 - the default is never mutated
    ) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    item = _ItemHolder.model_json_schema()["$defs"]["Item"]
    assert schema["$defs"] == {"Item": item}
    refs = _all_refs(schema)
    assert refs == ["#/$defs/Item"] * 5
    assert all(_resolve(schema, ref) == item for ref in refs)

    props = schema["properties"]
    assert '"$defs":' not in json.dumps(props)
    holder = {
        key: value for key, value in _ItemHolder.model_json_schema().items() if key != "$defs"
    }
    assert props["owner"] == holder
    assert props["backup"] == {"anyOf": [holder, {"type": "null"}]}
    assert props["team"] == {"type": "array", "items": holder}
    assert props["pair"]["anyOf"][0]["prefixItems"] == [holder, {"type": "integer"}]
    assert props["by_name"] == {"type": "object", "additionalProperties": holder}
    assert json.loads(json.dumps(schema)) == schema


def test_same_named_definitions_from_different_models_stay_distinct(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="name-clash")
    async def tool(a: _ItemHolder, b: list[_TextItemHolder], c: _ItemHolder | None = None) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    assert set(schema["$defs"]) == {"Item", "Item_2"}
    props = schema["properties"]
    a_ref = props["a"]["properties"]["item"]["$ref"]
    b_ref = props["b"]["items"]["properties"]["item"]["$ref"]
    c_ref = props["c"]["anyOf"][0]["properties"]["item"]["$ref"]
    assert _resolve(schema, a_ref)["properties"]["value"] == {"type": "integer"}
    assert _resolve(schema, b_ref)["properties"]["value"] == {"type": "string"}
    assert c_ref == a_ref


if pydantic is not None and int(pydantic.VERSION.split(".")[0]) >= 2:

    class Address(pydantic.BaseModel):
        city: str

    class Person(pydantic.BaseModel):
        name: str
        address: Address


@pytest.mark.skipif(
    pydantic is None or int(pydantic.VERSION.split(".")[0]) < 2,
    reason="needs pydantic 2 or newer",
)
def test_pydantic_nested_model_refs_resolve(registrations: list[dict[str, Any]]) -> None:
    @mcp_tool(service_id="pydantic-nested")
    async def tool(owner: Person, team: list[Person], backup: Person | None = None) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    assert set(schema["$defs"]) == {"Address"}
    refs = _all_refs(schema)
    assert refs == ["#/$defs/Address"] * 3
    for ref in refs:
        assert _resolve(schema, ref)["properties"]["city"]["type"] == "string"
    assert '"$defs":' not in json.dumps(schema["properties"])
    assert schema["required"] == ["owner", "team"]


class _BoxHolder:
    """Two levels of definitions: the tool refs Box, and Box refs Item."""

    value_type = "integer"

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        return {
            "$defs": {
                "Box": {"type": "object", "properties": {"item": {"$ref": "#/$defs/Item"}}},
                "Item": {"type": "object", "properties": {"value": {"type": cls.value_type}}},
            },
            "type": "object",
            "properties": {"box": {"$ref": "#/$defs/Box"}},
        }


class _TextBoxHolder(_BoxHolder):
    value_type = "string"


def _item_value_type(schema: dict[str, Any], holder: dict[str, Any]) -> str:
    box = _resolve(schema, holder["properties"]["box"]["$ref"])
    item = _resolve(schema, box["properties"]["item"]["$ref"])
    return cast(str, item["properties"]["value"]["type"])


def test_equal_definitions_that_ref_conflicting_ones_are_not_merged(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="graph-clash")
    async def tool(
        a: _BoxHolder,
        b: _TextBoxHolder,
        c: list[_BoxHolder],
        d: _TextBoxHolder | None = None,
    ) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    props = schema["properties"]
    assert _item_value_type(schema, props["a"]) == "integer"
    assert _item_value_type(schema, props["b"]) == "string"
    assert _item_value_type(schema, props["c"]["items"]) == "integer"
    assert _item_value_type(schema, props["d"]["anyOf"][0]) == "string"
    assert set(schema["$defs"]) == {"Box", "Item", "Box_2", "Item_2"}
    assert props["c"]["items"] == props["a"]
    assert props["d"]["anyOf"][0] == props["b"]


class Ratio(enum.Enum):
    UNBOUNDED = float("inf")


def test_non_finite_enum_values_fall_back_to_any(
    registrations: list[dict[str, Any]],
) -> None:
    @mcp_tool(service_id="non-finite")
    async def tool(ratio: Ratio, limit: Literal[Ratio.UNBOUNDED]) -> dict:
        return {}

    schema = registrations[0]["input_schema"]
    assert schema["properties"] == {"ratio": {}, "limit": {}}
    assert json.loads(json.dumps(schema, allow_nan=False)) == schema
