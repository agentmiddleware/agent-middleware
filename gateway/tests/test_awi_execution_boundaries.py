from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.main import app
from app.routers.awi_enhanced import _DOM_SESSION_WALLETS
from app.schemas.awi import AWIExecutionRequest, AWISessionCreate, AWIStandardAction
from app.services.awi_playwright_bridge import get_playwright_bridge
from app.services.awi_session import AWISessionManager
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.mark.proof
@pytest.mark.anyio
async def test_dom_structured_failure_does_not_become_success(monkeypatch):
    manager = AWISessionManager()
    session = await manager.create_session(
        AWISessionCreate(target_url="https://example.test")
    )
    manager._dom_sessions[session.session_id] = "dom-synthetic"
    monkeypatch.setattr(
        manager._playwright_bridge, "translate_action", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        manager._playwright_bridge,
        "execute_commands",
        AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                commands_executed=0,
                new_url=None,
                error="synthetic selector failure",
                duration_ms=0,
            )
        ),
    )
    response = await manager.execute_action(
        AWIExecutionRequest(
            session_id=session.session_id,
            action=AWIStandardAction.NAVIGATE_TO,
            parameters={"url": "https://example.test/next"},
        )
    )
    assert response.result["success"] is False
    assert response.status == "error"
    assert response.effect_status == "unknown"


@pytest.mark.proof
@pytest.mark.anyio
async def test_dry_run_never_dispatches_browser_commands(monkeypatch):
    manager = AWISessionManager()
    session = await manager.create_session(
        AWISessionCreate(target_url="https://example.test")
    )
    manager._dom_sessions[session.session_id] = "dom-synthetic"
    dispatched = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(manager, "_execute_via_dom_bridge", dispatched)
    response = await manager.execute_action(
        AWIExecutionRequest(
            session_id=session.session_id,
            action=AWIStandardAction.NAVIGATE_TO,
            parameters={"url": "https://example.test/next"},
            dry_run=True,
        )
    )
    assert response.error == "dry_run_unsupported"
    assert response.effect_status == "not_dispatched"
    dispatched.assert_not_awaited()
    assert session.step_count == 0


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize("commands_executed", [0, 1])
async def test_dom_sync_failure_retains_uncertain_receipt_and_key(
    monkeypatch, clean_database, commands_executed
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        actor = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=actor["agent_wallet_id"],
            key_id=actor["key_id"],
            tool_name="awi_dom_sync",
            max_credits=50,
            idem_key="synthetic-dom-permit",
        )
        session_id = "synthetic-dom-session"
        monkeypatch.setitem(_DOM_SESSION_WALLETS, session_id, actor["agent_wallet_id"])
        bridge = get_playwright_bridge()
        monkeypatch.setattr(
            bridge,
            "get_session",
            AsyncMock(return_value=SimpleNamespace(session_id=session_id)),
        )
        monkeypatch.setattr(bridge, "translate_action", AsyncMock(return_value=[]))
        execute = AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                commands_executed=commands_executed,
                new_url=None,
                error="synthetic command failure",
            )
        )
        monkeypatch.setattr(bridge, "execute_commands", execute)
        representation = AsyncMock()
        monkeypatch.setattr(bridge, "extract_state_representation", representation)
        headers = {
            **actor["agent_headers"],
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "synthetic-dom-attempt",
        }
        payload = {
            "session_id": session_id,
            "action": "navigate_to",
            "parameters": {"url": "https://example.test"},
        }
        response = await client.post("/v1/awi/dom/sync", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "error"
        assert response.json()["effect_status"] == "unknown"
        assert response.json()["receipt"]["outcome"] == "delivery_uncertain"
        replay = await client.post("/v1/awi/dom/sync", json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json() == response.json()
        execute.assert_awaited_once()
        representation.assert_not_awaited()
