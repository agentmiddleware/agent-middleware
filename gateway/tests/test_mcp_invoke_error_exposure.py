"""HTTP /mcp/tools/{id}/invoke never echoes exception text (follow-up to #242).

Each case raises an exception carrying a unique marker. The marker must be
absent from the HTTP response and present in the server log under the same
request id that the response returns.
"""

from __future__ import annotations

import logging
import uuid

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
        headers={"X-API-Key": "test-key", **headers},
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
    registered_tool["exc"] = RuntimeError(f"db password=hunter2 {marker}")
    caplog.set_level(logging.ERROR, logger="app.routers.mcp")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet_id = await _create_funded_agent_wallet(client, "invoke-err-agent-1")
        response = await _invoke(client, wallet_id, {"X-Request-ID": "req-abc.123_X"})
    assert response.status_code == 200
    assert marker not in response.text
    assert "hunter2" not in response.text
    payload = response.json()
    assert payload["isError"] is True
    assert payload["structuredContent"]["request_id"] == "req-abc.123_X"
    assert payload["structuredContent"]["error"] == "tool_execution_failed"
    _assert_logged(caplog, marker, "req-abc.123_X")


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
        wallet_id = await _create_funded_agent_wallet(client, f"invoke-err-{status}")
        # An unsafe X-Request-ID is replaced with a fresh uuid4 hex.
        response = await _invoke(client, wallet_id, {"X-Request-ID": "bad id\nx"})
    assert response.status_code == status
    assert marker not in response.text
    detail = response.json()["detail"]
    request_id = detail["request_id"]
    assert request_id != "bad id\nx"
    uuid.UUID(hex=request_id)
    assert isinstance(detail["error"], str) and marker not in detail["error"]
    _assert_logged(caplog, marker, request_id)
