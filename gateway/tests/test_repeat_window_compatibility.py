"""The schema-040 rollback keeps signed window limits enforceable."""

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import McpDispatchAttemptModel
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet
from tests.test_upstream_retry_cap_enforcement import (
    FakeUpstreamExecutor,
    _call_body,
    _register_upstream,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("permit_window,global_window", [(60, 86400), (120, 1)])
async def test_rollback_enforces_signed_window_with_issuance_disabled(
    clean_database, monkeypatch, permit_window, global_window
):
    import app.services.mcp_dispatch_attempts as dispatch

    settings = get_settings()
    monkeypatch.setattr(settings, "MCP_UPSTREAM_DUPLICATE_GUARD", "enforce")
    monkeypatch.setattr(
        settings, "MCP_UPSTREAM_DUPLICATE_WINDOW_SECONDS", global_window
    )
    monkeypatch.setattr(settings, "ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE", True)
    executor = FakeUpstreamExecutor()
    tool = "repeat-window-rollback"
    _register_upstream(tool, executor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        wallet = await provision_agent_wallet(client)
        created = await client.post(
            "/v1/permits",
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "window-permit"},
            json={
                "issuer_wallet_id": wallet["agent_wallet_id"],
                "subject_wallet_id": wallet["agent_wallet_id"],
                "allowed_tools": [tool],
                "scopes": [f"tool:{tool}:invoke", "billing:charge"],
                "max_credits": 100,
                "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
                "repeat_window_seconds": permit_window,
            },
        )
        assert created.status_code == 201, created.text
        permit = created.json()
        assert permit["repeat_window_seconds"] == permit_window
        monkeypatch.setattr(settings, "ENABLE_PERMIT_REPEAT_WINDOW_ISSUANCE", False)

        async def invoke(key):
            response = await client.post(
                "/mcp/messages",
                headers=wallet["agent_headers"],
                json=_call_body(
                    tool_name=tool,
                    wallet_id=wallet["agent_wallet_id"],
                    permit_id=permit["permit_id"],
                    idempotency_key=key,
                ),
            )
            assert response.status_code == 200, response.text
            return response.json()

        first = await invoke("first-window-call")
        assert "result" in first, first
        async with get_session_factory()() as session:
            attempt = (
                await session.execute(select(McpDispatchAttemptModel))
            ).scalar_one()
            started = attempt.created_at
        monkeypatch.setattr(dispatch, "utc_now", lambda: started + timedelta(seconds=2))
        duplicate = await invoke("duplicate-window-call")
        assert "duplicate_request_new_key" in str(duplicate), duplicate
        assert executor.dispatch_count == 1
        replay = await invoke("first-window-call")
        assert "result" in replay, replay
        assert executor.dispatch_count == 1
        monkeypatch.setattr(
            dispatch, "utc_now", lambda: started + timedelta(seconds=permit_window + 1)
        )
        after = await invoke("after-window-call")
        assert "result" in after, after
        assert executor.dispatch_count == 2
