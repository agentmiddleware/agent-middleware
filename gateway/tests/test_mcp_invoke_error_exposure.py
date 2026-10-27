"""HTTP /mcp/tools/{id}/invoke never echoes exception text (follow-up to #242).

Each case raises an exception carrying a unique marker. The marker must be
absent from the HTTP response and present in the server log under the same
request id that the response returns in its X-Request-ID header. Error bodies
carry no per-request value, so idempotent replays stay byte-identical.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import mcp as mcp_router
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_mcp_generator import _create_funded_agent_wallet

SERVICE_ID = "invoke-error-exposure-echo"


def _marker() -> str:
    return f"SECRET-MARKER-{uuid.uuid4().hex}"


def _api_key() -> str:
    # The key the test suite's conftest configured for this run; no literal
    # key is committed here.
    return os.environ["VALID_API_KEYS"].split(",")[0].strip()


def _request_id() -> str:
    return f"req-{secrets.token_hex(8)}"


def _assert_logged(caplog, marker: str, request_id: str) -> None:
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.routers.mcp" and "mcp_invoke_error" in r.getMessage()
    ]
    assert any(
        marker in line and f"request_id={request_id}" in line for line in lines
    ), lines


async def _invoke(client: AsyncClient, wallet_id: str, headers: dict[str, str]):
    return await client.post(
        f"/mcp/tools/{SERVICE_ID}/invoke",
        json={
            "name": SERVICE_ID,
            "arguments": {},
            "mcp_context": {"wallet_id": wallet_id},
        },
        headers={"X-API-Key": _api_key(), **headers},
    )


@pytest.fixture
def registered_tool():
    registry = get_service_registry()
    state: dict[str, BaseException] = {}

    def tool() -> dict:
        raise state["exc"]

    registry.register_local(
        service_id=SERVICE_ID,
        name="Invoke Error Exposure Echo",
        description="Raises a configured exception",
        category=ServiceCategory.AGENT_COMMS,
        func=tool,
        credits_per_unit=1.0,
        unit_name="call",
    )
    try:
        yield state
    finally:
        registry.unregister_local(SERVICE_ID)


@pytest.mark.anyio
async def test_tool_failure_text_stays_in_log(clean_database, registered_tool, caplog):
    marker = _marker()
    request_id = _request_id()
    registered_tool["exc"] = RuntimeError(marker)
    caplog.set_level(logging.ERROR, logger="app.routers.mcp")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet_id = await _create_funded_agent_wallet(
            client, f"invoke-err-{uuid.uuid4().hex[:12]}"
        )
        response = await _invoke(client, wallet_id, {"X-Request-ID": request_id})
    assert response.status_code == 200
    assert marker not in response.text
    assert request_id not in response.text
    assert response.headers["x-request-id"] == request_id
    payload = response.json()
    assert payload["isError"] is True
    assert payload["structuredContent"] == {"error": "tool_execution_failed"}
    _assert_logged(caplog, marker, request_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exc_factory", "status"),
    [
        (lambda m: PermissionError(m), 403),
        (lambda m: mcp_router.ToolPermissionDenied(m), 403),
        (lambda m: mcp_router.GovernedToolError(m, status_code=502), 502),
        (lambda m: mcp_router.IdempotencyInProgressError(m), 409),
        (lambda m: ValueError(f"Tool not found: {m}"), 404),
        (lambda m: ValueError(f"Tool not executable: {m}"), 501),
    ],
)
async def test_http_error_branches_return_fixed_messages(
    clean_database, monkeypatch, caplog, exc_factory, status
):
    marker = _marker()
    exc = exc_factory(marker)

    async def boom(_request):
        raise exc

    monkeypatch.setattr(mcp_router._mcp_adapter, "invoke", boom)
    caplog.set_level(logging.ERROR, logger="app.routers.mcp")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet_id = await _create_funded_agent_wallet(
            client, f"invoke-err-{uuid.uuid4().hex[:12]}"
        )
        # An unsafe X-Request-ID is replaced with a fresh uuid4 hex.
        response = await _invoke(client, wallet_id, {"X-Request-ID": "bad id\nx"})
    assert response.status_code == status
    assert marker not in response.text
    request_id = response.headers["x-request-id"]
    assert request_id != "bad id\nx"
    uuid.UUID(hex=request_id)
    assert request_id not in response.text
    _assert_logged(caplog, marker, request_id)


@pytest.mark.anyio
async def test_upstream_result_never_reaches_client(clean_database, monkeypatch):
    marker = _marker()
    dispatch = {"attempt_id": uuid.uuid4().hex}
    exc = mcp_router.GovernedToolError(
        "upstream_returned_error",
        extra_data={"upstream_result": {"error": marker}, "dispatch": dispatch},
        status_code=502,
    )

    async def boom(_request):
        raise exc

    monkeypatch.setattr(mcp_router._mcp_adapter, "invoke", boom)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet_id = await _create_funded_agent_wallet(
            client, f"invoke-err-{uuid.uuid4().hex[:12]}"
        )
        response = await _invoke(client, wallet_id, {})
    assert response.status_code == 502
    assert marker not in response.text
    assert "upstream_result" not in response.text
    detail = response.json()["detail"]
    assert detail["error"] == "upstream_returned_error"
    assert detail["dispatch"] == dispatch


@pytest.mark.anyio
async def test_identical_errors_replay_byte_identical(clean_database, monkeypatch):
    exc = mcp_router.GovernedToolError("quote_expired", status_code=409)

    async def boom(_request):
        raise exc

    monkeypatch.setattr(mcp_router._mcp_adapter, "invoke", boom)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet_id = await _create_funded_agent_wallet(
            client, f"invoke-err-{uuid.uuid4().hex[:12]}"
        )
        first = await _invoke(client, wallet_id, {})
        second = await _invoke(client, wallet_id, {})
    assert first.status_code == second.status_code == 409
    assert first.content == second.content
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_snake_case_tool_text_is_not_echoed():
    tool_text = f"secret_{secrets.token_hex(6)}"
    # A tool's own snake_case text, live and on replay, maps to a canned code.
    live = mcp_router.GovernedToolError(tool_text, tool_originated=True)
    assert mcp_router._invoke_public_message(live) == "tool_execution_failed"
    # A registered refusal code raised by a tool is still the tool's text.
    spoof = mcp_router.GovernedToolError("quote_expired", tool_originated=True)
    assert mcp_router._invoke_public_message(spoof) == "tool_execution_failed"
    # Unregistered snake_case text from the gateway types is not passed through.
    assert (
        mcp_router._invoke_public_message(mcp_router.GovernedToolError(tool_text))
        == "tool_call_refused"
    )
    assert mcp_router._invoke_public_message(PermissionError(tool_text)) == (
        "Permission denied"
    )
    # Registered gateway codes still pass through.
    assert (
        mcp_router._invoke_public_message(mcp_router.GovernedToolError("quote_expired"))
        == "quote_expired"
    )


@pytest.mark.anyio
async def test_replayed_tool_failure_stays_canned():
    tool_text = f"secret_{secrets.token_hex(6)}"
    replay = SimpleNamespace(
        status_code=500,
        response_json={
            "error": tool_text,
            "receipt": {"reason_code": "tool_execution_failed"},
        },
        response_reference=None,
    )
    with pytest.raises(mcp_router.GovernedToolError) as raised:
        await mcp_router._raise_replayed_error(replay)
    assert raised.value.tool_originated is True
    assert mcp_router._invoke_public_message(raised.value) == "tool_execution_failed"
