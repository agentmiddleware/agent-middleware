"""Unit tests for the public MCP tool handlers (app/routers/mcp_public.py).

This complements test_public_mcp_endpoint.py, which drives the surface over
HTTP. These tests call the module's handlers directly with stubbed services,
so they need no credentials and touch no database rows: they pin the
read-only tool surface, the argument-validation errors, the verification
taxonomy for callers that hold no key, and the off-by-default gating.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from mcp.shared.exceptions import McpError

from app.routers import mcp_public
from app.routers.mcp_public import (
    PUBLIC_SERVER_NAME,
    _keys_unavailable_result,
    _require_enabled,
    _signing_input_too_large,
    _TOOL_HANDLERS,
    _TOOLS,
)
from app.services.signing_keys import RECEIPT_CANONICALIZATION

_READ_ONLY_NAMES = {"verify_receipt", "get_trust_plane_info", "list_governed_tools"}


def _unverifiable_bundle() -> dict:
    return {
        "receipt_id": "rcpt-no-key",
        "kid": "no-published-key",
        "alg": "Ed25519",
        "canonicalization": RECEIPT_CANONICALIZATION,
        "signing_input": json.dumps(
            {"receipt_id": "rcpt-no-key", "kid": "no-published-key"}
        ),
        "signature": base64.b64encode(b"\x00" * 64).decode(),
    }


class _StubKeyService:
    def __init__(self, keys):
        self._keys = keys

    async def list_public_keys(self):
        return list(self._keys)


def _fake_key(key_id="kid-unit-1"):
    return SimpleNamespace(
        key_id=key_id,
        alg="Ed25519",
        status="active",
        public_key_b64=base64.b64encode(bytes(32)).decode(),
        created_at=None,
        activated_at=None,
        retired_at=None,
    )


def _stub_manifest_tool(name, category="agent_comms", **annotations):
    entry = {"name": name, "description": f"{name} description"}
    entry["annotations"] = {"category": category, **annotations}
    return entry


class _StubGenerator:
    """Mirror of the real manifest builder's category filter."""

    def __init__(self, tools):
        self._tools = tools

    async def generate_tools_json_async(self, category=None, **kwargs):
        tools = self._tools
        if category is not None:
            tools = [
                tool
                for tool in tools
                if (tool.get("annotations") or {}).get("category") == category.value
            ]
        return {"tools": tools}


def _stub_settings(environment="test", enabled=True):
    return SimpleNamespace(ENVIRONMENT=environment, ENABLE_PUBLIC_MCP_ENDPOINT=enabled)


def test_tool_surface_is_exactly_three_read_only_tools():
    assert {tool.name for tool in _TOOLS} == _READ_ONLY_NAMES
    assert set(_TOOL_HANDLERS) == _READ_ONLY_NAMES
    for tool in _TOOLS:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.inputSchema["type"] == "object"
        assert tool.outputSchema["type"] == "object"


def test_governed_tool_names_are_not_reachable_here():
    assert "partner.notes.write" not in _TOOL_HANDLERS
    assert "verify_receipt" in _TOOL_HANDLERS


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [{}, {"bundle": None}, {"bundle": 123}])
async def test_verify_receipt_requires_a_bundle_object_or_string(arguments):
    with pytest.raises(McpError) as exc_info:
        await mcp_public._tool_verify_receipt(arguments)
    assert exc_info.value.error.code == -32602


@pytest.mark.anyio
async def test_verify_receipt_rejects_a_non_string_expected_issuer():
    with pytest.raises(McpError) as exc_info:
        await mcp_public._tool_verify_receipt(
            {"bundle": _unverifiable_bundle(), "expected_issuer": 123}
        )
    assert exc_info.value.error.code == -32602


@pytest.mark.anyio
async def test_malformed_bundle_is_a_verdict_without_touching_keys(monkeypatch):
    def fail_if_called():
        raise AssertionError("malformed input must not touch the key store")

    monkeypatch.setattr(
        mcp_public, "get_signing_key_service", lambda: _StubKeyService([])
    )
    monkeypatch.setattr(_StubKeyService, "list_public_keys", fail_if_called)
    structured, text = await mcp_public._tool_verify_receipt({"bundle": {}})

    assert structured["ok"] is False
    assert structured["status"] == "malformed"
    assert structured["claims"] == {}
    assert "not evidence of tampering" in text.lower()


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["signing_input", "garbage"])
async def test_oversized_bundle_is_malformed_without_touching_keys(monkeypatch, field):
    async def fail_if_called():
        raise AssertionError("oversized input must not touch the key store")

    monkeypatch.setattr(
        mcp_public,
        "get_signing_key_service",
        lambda: SimpleNamespace(list_public_keys=fail_if_called),
    )
    bundle = {
        **_unverifiable_bundle(),
        field: "x" * (mcp_public._MAX_SIGNING_INPUT_BYTES + 1),
    }
    structured, text = await mcp_public._tool_verify_receipt({"bundle": bundle})

    assert structured["ok"] is False
    assert structured["status"] == "malformed"
    assert "larger than this endpoint will verify" in text.lower()


@pytest.mark.anyio
async def test_unknown_key_without_published_keys_is_retryable(monkeypatch):
    monkeypatch.setattr(
        mcp_public, "get_signing_key_service", lambda: _StubKeyService([])
    )
    structured, text = await mcp_public._tool_verify_receipt(
        {"bundle": _unverifiable_bundle()}
    )

    assert structured["ok"] is False
    assert structured["status"] == "keys_unavailable"
    assert "retry" in text.lower()
    assert "not a verdict" in text.lower()


def test_keys_unavailable_result_shape():
    structured, text = _keys_unavailable_result()

    assert structured["ok"] is False
    assert structured["status"] == "keys_unavailable"
    assert structured["receipt_id"] is None
    assert structured["kid"] is None
    assert structured["claims"] == {}
    assert "retry" in text.lower()


@pytest.mark.anyio
async def test_get_trust_plane_info_without_keys(monkeypatch):
    monkeypatch.setattr(
        mcp_public, "get_signing_key_service", lambda: _StubKeyService([])
    )
    structured, text = await mcp_public._tool_get_trust_plane_info({})

    assert structured["name"] == PUBLIC_SERVER_NAME
    assert structured["alg"] == "Ed25519"
    assert structured["keys_available"] is False
    assert structured["keys"] == []
    assert structured["endpoints"]["trust_keys"] == "/.well-known/trust-keys.json"
    assert "none published" in text


@pytest.mark.anyio
async def test_get_trust_plane_info_publishes_key_without_jwk(monkeypatch):
    monkeypatch.setattr(
        mcp_public,
        "get_signing_key_service",
        lambda: _StubKeyService([_fake_key()]),
    )
    structured, _text = await mcp_public._tool_get_trust_plane_info({})

    assert structured["keys_available"] is True
    (entry,) = structured["keys"]
    assert entry["kid"] == "kid-unit-1"
    assert entry["alg"] == "Ed25519"
    assert entry["status"] == "active"
    assert "jwk" not in entry


@pytest.mark.anyio
async def test_get_trust_plane_info_treats_key_store_failure_as_empty(monkeypatch):
    async def unavailable():
        raise RuntimeError("transient signing-key store failure")

    monkeypatch.setattr(
        mcp_public,
        "get_signing_key_service",
        lambda: SimpleNamespace(list_public_keys=unavailable),
    )
    structured, _text = await mcp_public._tool_get_trust_plane_info({})

    assert structured["keys_available"] is False
    assert structured["keys"] == []


@pytest.mark.anyio
async def test_list_governed_tools_paginates_a_stubbed_manifest(monkeypatch):
    tools = [
        _stub_manifest_tool(
            "tool-a",
            creditsPerCallExact="2.5",
            unitName="call",
            requirePermit=True,
        ),
        _stub_manifest_tool("tool-b", creditsPerCall=1, requirePermit=False),
        _stub_manifest_tool("tool-c", category="iot_bridge"),
    ]
    monkeypatch.setattr(mcp_public, "get_mcp_generator", lambda: _StubGenerator(tools))
    structured, text = await mcp_public._tool_list_governed_tools(
        {"limit": 2, "offset": 0}
    )

    assert structured["total"] == 3
    assert structured["count"] == 2
    assert structured["limit"] == 2
    assert structured["offset"] == 0
    assert structured["has_more"] is True
    assert [tool["name"] for tool in structured["tools"]] == ["tool-a", "tool-b"]
    for entry in structured["tools"]:
        assert set(entry) == {
            "name",
            "description",
            "category",
            "credits_per_call",
            "unit_name",
            "requires_permit",
        }
    assert structured["tools"][0]["credits_per_call"] == "2.5"
    assert structured["tools"][0]["requires_permit"] is True
    assert structured["tools"][1]["credits_per_call"] == 1
    assert "returning 2 starting at offset 0" in text


@pytest.mark.anyio
async def test_list_governed_tools_filters_by_category(monkeypatch):
    tools = [
        _stub_manifest_tool("tool-a", category="agent_comms"),
        _stub_manifest_tool("tool-c", category="iot_bridge"),
    ]
    monkeypatch.setattr(mcp_public, "get_mcp_generator", lambda: _StubGenerator(tools))
    structured, text = await mcp_public._tool_list_governed_tools(
        {"category": "iot_bridge"}
    )

    assert structured["total"] == 1
    assert [tool["name"] for tool in structured["tools"]] == ["tool-c"]
    assert structured["has_more"] is False
    assert "iot_bridge" in text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"category": "not-a-category"},
        {"limit": 0},
        {"limit": 201},
        {"limit": "50"},
        {"limit": True},
        {"offset": -1},
        {"offset": "0"},
        {"offset": False},
    ],
)
async def test_list_governed_tools_rejects_bad_arguments(arguments):
    with pytest.raises(McpError) as exc_info:
        await mcp_public._tool_list_governed_tools(arguments)
    assert exc_info.value.error.code == -32602


@pytest.mark.parametrize(
    ("environment", "enabled", "allowed"),
    [
        ("test", False, False),
        ("test", True, True),
        ("production", True, False),
        ("staging", True, False),
        ("production", False, False),
    ],
)
def test_require_enabled_gating(monkeypatch, environment, enabled, allowed):
    monkeypatch.setattr(
        mcp_public, "get_settings", lambda: _stub_settings(environment, enabled)
    )
    if allowed:
        _require_enabled()
    else:
        with pytest.raises(HTTPException) as exc_info:
            _require_enabled()
        assert exc_info.value.status_code == 404


def test_signing_input_size_guard_boundaries():
    limit = mcp_public._MAX_SIGNING_INPUT_BYTES
    assert _signing_input_too_large({"signing_input": "x" * limit}) is False
    assert _signing_input_too_large({"signing_input": "x" * (limit + 1)}) is True
    assert _signing_input_too_large("x" * (limit + 1)) is True
    assert _signing_input_too_large({"other": 1}) is False
    assert _signing_input_too_large({"signing_input": ["not", "a", "string"]}) is False
    assert _signing_input_too_large(123) is False
