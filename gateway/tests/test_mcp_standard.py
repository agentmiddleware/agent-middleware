"""Unit coverage for the standard MCP endpoint helpers (app/routers/mcp_standard).

The protocol surface (initialize, version negotiation, notifications, ping,
JSON-RPC framing) is owned by the official MCP SDK and already pinned by
test_standard_mcp_endpoint.py. These tests pin the trust-plane helpers in
this module that need no database, network, or running server: permit
budgeting, machine-actionable denial mapping, permit-reference handling,
replay-key resolution, the off-by-default gate, and origin validation.
"""

from __future__ import annotations

from decimal import Decimal

import mcp.types as mcp_types
import pytest
from fastapi import HTTPException
from mcp.shared.exceptions import McpError
from starlette.requests import Request

from app.core.config import get_settings
from app.routers import mcp_standard as std
from app.services.permits import PermitError


def _params(**kwargs: object) -> mcp_types.CallToolRequestParams:
    base: dict[str, object] = {"name": "demo.tool", "arguments": {}}
    base.update(kwargs)
    return mcp_types.CallToolRequestParams.model_validate(base)


def _make_request(
    *, origin: str | None = None, host: str = "testserver", path: str = "/mcp"
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "headers": headers,
            "server": (host, 80),
        }
    )


# Permit budgeting -----------------------------------------------------------


def test_tool_call_budget_uses_exact_cost() -> None:
    assert std._tool_call_budget({"credits_per_unit_exact": "2.5"}) == Decimal("2.5")


def test_tool_call_budget_floors_zero_cost_tool_at_one_credit() -> None:
    assert std._tool_call_budget({"credits_per_unit": 0.0}) == Decimal("1")


def test_tool_call_budget_defaults_when_nothing_configured() -> None:
    assert std._tool_call_budget({}) == Decimal("1.0")


def test_tool_call_budget_falls_back_when_exact_cost_is_not_a_number() -> None:
    record = {"credits_per_unit_exact": "not-a-number", "credits_per_unit": 3.0}
    assert std._tool_call_budget(record) == Decimal("3.0")


# Denial mapping -------------------------------------------------------------


def test_permit_budget_denial_is_machine_actionable() -> None:
    exc = PermitError("permit_budget_exceeds_wallet_balance")
    err = std._permit_error(
        exc, tool_name="demo.tool", record={"credits_per_unit": 2.0}
    )
    assert err.error.code == -32004
    assert err.error.data["reason_code"] == "permit_budget_exceeds_wallet_balance"
    assert err.error.data["requested"]["tool"] == "demo.tool"
    assert err.error.data["requested"]["estimated_credits"] == "2.0"
    assert "request_authority" in err.error.data["remediation"]


def test_non_budget_permit_denial_maps_to_generic_forbidden() -> None:
    err = std._permit_error(PermitError("permit_not_found"))
    assert err.error.code == -32003
    assert err.error.message == "permit_not_found"


def test_mcp_error_carries_code_message_and_data() -> None:
    err = std._mcp_error(-32003, "nope", {"reason_code": "x"})
    assert (err.error.code, err.error.message) == (-32003, "nope")
    assert err.error.data == {"reason_code": "x"}


# Permit references ----------------------------------------------------------


def test_no_permit_reference_when_absent() -> None:
    assert std._action_permit_reference(_params()) is None


def test_permit_reference_from_meta() -> None:
    params = _params(
        _meta={"io.agentmiddleware/permit_id": "permit-123"},
    )
    assert std._action_permit_reference(params) == "permit-123"


def test_permit_reference_from_legacy_context() -> None:
    params = _params(mcpContext={"permit_id": "permit-legacy"})
    assert std._action_permit_reference(params) == "permit-legacy"


def test_conflicting_permit_references_are_refused() -> None:
    params = _params(
        _meta={"io.agentmiddleware/permit_id": "permit-a"},
        mcpContext={"permit_id": "permit-b"},
    )
    with pytest.raises(McpError) as exc_info:
        std._action_permit_reference(params)
    assert exc_info.value.error.code == -32602


def test_blank_permit_reference_is_refused() -> None:
    params = _params(mcpContext={"permit_id": "   "})
    with pytest.raises(McpError) as exc_info:
        std._action_permit_reference(params)
    assert exc_info.value.error.code == -32602


def test_non_string_permit_reference_is_refused() -> None:
    params = _params(mcpContext={"permit_id": 42})
    with pytest.raises(McpError) as exc_info:
        std._action_permit_reference(params)
    assert exc_info.value.error.code == -32602


# Replay-key resolution ------------------------------------------------------


def test_absent_replay_key_resolves_to_none() -> None:
    assert std._client_idempotency_key(None, _params()) is None


def test_null_meta_key_reads_as_absent() -> None:
    params = _params(_meta={"io.agentmiddleware/idempotency_key": None})
    assert std._client_idempotency_key(None, params) is None


def test_meta_key_is_honored() -> None:
    params = _params(_meta={"io.agentmiddleware/idempotency_key": "key-abc-123"})
    assert std._client_idempotency_key(None, params) == "key-abc-123"


def test_blank_replay_key_is_refused_not_replaced() -> None:
    params = _params(_meta={"io.agentmiddleware/idempotency_key": "   "})
    with pytest.raises(McpError) as exc_info:
        std._client_idempotency_key(None, params)
    assert exc_info.value.error.code == -32602


def test_disagreeing_key_sources_are_refused() -> None:
    params = _params(
        _meta={"io.agentmiddleware/idempotency_key": "key-one"},
        mcpContext={"idempotency_key": "key-two"},
    )
    with pytest.raises(McpError) as exc_info:
        std._client_idempotency_key(None, params)
    assert exc_info.value.error.code == -32602


def test_matching_key_sources_agree() -> None:
    params = _params(
        _meta={"io.agentmiddleware/idempotency_key": "key-same"},
        mcpContext={"idempotency_key": "key-same"},
    )
    assert std._client_idempotency_key(None, params) == "key-same"


def test_non_object_legacy_context_is_refused() -> None:
    params = _params(mcpContext="not-an-object")
    with pytest.raises(McpError) as exc_info:
        std._client_idempotency_key(None, params)
    assert exc_info.value.error.code == -32602


# Gating and origin validation ----------------------------------------------


def test_endpoint_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_STANDARD_MCP_ENDPOINT", False)
    with pytest.raises(HTTPException) as exc_info:
        std._require_enabled()
    assert exc_info.value.status_code == 404


def test_endpoint_enabled_passes_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_STANDARD_MCP_ENDPOINT", True)
    assert std._require_enabled() is None


def test_origin_absent_passes_for_non_browser_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "PUBLIC_URL", "")
    assert std._validate_origin(_make_request()) is None


def test_wrong_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "PUBLIC_URL", "https://app.example.com")
    with pytest.raises(HTTPException) as exc_info:
        std._validate_origin(
            _make_request(origin="https://evil.example.com", host="app.example.com")
        )
    assert exc_info.value.status_code == 403


def test_wrong_host_is_refused_when_public_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "PUBLIC_URL", "https://app.example.com")
    with pytest.raises(HTTPException) as exc_info:
        std._validate_origin(_make_request(host="other.example.com"))
    assert exc_info.value.status_code == 421


# Server composition ---------------------------------------------------------


def test_standard_server_registers_governed_tools_call() -> None:
    server = std._build_standard_mcp_server()
    assert mcp_types.CallToolRequest in server.request_handlers


def test_auto_permit_scope_is_distinct_from_governed_calls() -> None:
    assert std._AUTO_PERMIT_ENDPOINT == "/mcp#auto-permit"


def test_server_instructions_describe_governance() -> None:
    assert "permit" in std._SERVER_INSTRUCTIONS
