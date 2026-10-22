"""AWI session creation inherits a wallet-scoped key's wallet."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.auth import AuthContext, get_auth_context
from app.routers import awi, awi_enhanced
from app.schemas.awi import AWISession, AWISessionStatus
from app.services import awi_playwright_bridge


def _test_app(monkeypatch, auth: AuthContext):
    async def create_awi_session(request):
        now = datetime.now(timezone.utc)
        return AWISession(
            session_id="awi-wallet-default",
            target_url=request.target_url,
            wallet_id=request.wallet_id,
            status=AWISessionStatus.CREATED,
            created_at=now,
            updated_at=now,
            max_steps=request.max_steps,
        )

    manager = SimpleNamespace(create_session=AsyncMock(side_effect=create_awi_session))
    bridge = SimpleNamespace(
        create_session=AsyncMock(
            return_value=SimpleNamespace(
                session_id="dom-wallet-default", current_url="https://example.com"
            )
        )
    )
    owners = {}
    monkeypatch.setattr(awi, "get_awi_session_manager", lambda: manager)
    monkeypatch.setattr(awi_playwright_bridge, "get_playwright_bridge", lambda: bridge)
    monkeypatch.setattr(awi_enhanced, "_DOM_SESSION_WALLETS", owners)

    app = FastAPI()
    app.include_router(awi.router)
    app.include_router(awi_enhanced.router)
    app.dependency_overrides[get_auth_context] = lambda: auth
    return app, manager, bridge, owners


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/awi/sessions", "/v1/awi/dom/session"])
async def test_bound_key_defaults_omitted_wallet_id(monkeypatch, path):
    auth = AuthContext(source="api_key", raw_key="synthetic", wallet_id="owner")
    app, manager, bridge, owners = _test_app(monkeypatch, auth)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json={"target_url": "https://example.com"})

    assert response.status_code == 201, response.text
    if path == "/v1/awi/sessions":
        assert response.json()["wallet_id"] == "owner"
        assert manager.create_session.await_args.args[0].wallet_id == "owner"
    else:
        assert owners[response.json()["session_id"]] == "owner"
        bridge.create_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/awi/sessions", "/v1/awi/dom/session"])
async def test_bootstrap_admin_can_still_create_ownerless_session(monkeypatch, path):
    auth = AuthContext(source="api_key", raw_key="synthetic", is_bootstrap_admin=True)
    app, manager, _, owners = _test_app(monkeypatch, auth)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(path, json={"target_url": "https://example.com"})

    assert response.status_code == 201, response.text
    if path == "/v1/awi/sessions":
        assert response.json()["wallet_id"] is None
        assert manager.create_session.await_args.args[0].wallet_id is None
    else:
        assert owners[response.json()["session_id"]] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/awi/sessions", "/v1/awi/dom/session"])
async def test_bound_key_cannot_create_foreign_wallet_session(monkeypatch, path):
    auth = AuthContext(source="api_key", raw_key="synthetic", wallet_id="owner")
    app, manager, bridge, _ = _test_app(monkeypatch, auth)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            json={"target_url": "https://example.com", "wallet_id": "foreign"},
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "wallet_access_denied"
    manager.create_session.assert_not_awaited()
    bridge.create_session.assert_not_awaited()
