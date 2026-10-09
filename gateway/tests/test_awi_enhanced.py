"""Focused router tests for AWI Enhanced endpoints (passkey, DOM, RAG admin).

Covers the main public behavior of app/routers/awi_enhanced.py with faked
services: no network, no database, no real browser. Each test builds a
standalone FastAPI app around the router with an overridden auth context.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthContext, get_auth_context
from app.routers import awi_enhanced as awi_enhanced_router
from app.routers.awi_enhanced import router
from app.services import awi_playwright_bridge as bridge_module
from app.services import awi_rag_engine as rag_module
from app.services import awi_session as session_module
from app.services import webauthn_provider as webauthn_module
from app.services.awi_playwright_bridge import BrowserSessionLimitExceeded


def _make_client(auth, monkeypatch):
    """Build an httpx client around the router with faked auth and sessions."""
    current = {"auth": auth}
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_auth_context] = lambda: current["auth"]

    async def _get_session(session_id):
        return SimpleNamespace(
            session_id=session_id, wallet_id="owner", target_url="https://example.com"
        )

    manager = SimpleNamespace(
        get_session=AsyncMock(side_effect=_get_session),
    )
    monkeypatch.setattr(session_module, "get_awi_session_manager", lambda: manager)
    transport = httpx.ASGITransport(app=test_app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _owner_auth():
    return AuthContext(
        source="api_key", raw_key="synthetic", wallet_id="owner", key_id="synthetic"
    )


def _admin_auth():
    return AuthContext(
        source="api_key",
        raw_key="synthetic",
        wallet_id="owner",
        key_id="synthetic",
        is_bootstrap_admin=True,
    )


# ── Passkey ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_high_risk_actions_reports_actions_and_count(monkeypatch):
    provider = SimpleNamespace(
        HIGH_RISK_ACTIONS={"checkout", "payment"},
        get_high_risk_actions=lambda: ["checkout", "payment"],
    )
    monkeypatch.setattr(webauthn_module, "get_webauthn_provider", lambda: provider)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.get("/v1/awi/passkey/high-risk-actions")

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["actions"]) == ["checkout", "payment"]
    assert body["count"] == 2


@pytest.mark.asyncio
async def test_check_passkey_status_returns_verification(monkeypatch):
    provider = SimpleNamespace(
        get_verification_status=AsyncMock(
            return_value={
                "session_id": "sess-1",
                "action": "checkout",
                "is_verified": True,
                "verified_at": None,
                "expires_in_seconds": 42,
            }
        )
    )
    monkeypatch.setattr(webauthn_module, "get_webauthn_provider", lambda: provider)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.get("/v1/awi/passkey/status/sess-1/checkout")

    assert response.status_code == 200
    body = response.json()
    assert body["is_verified"] is True
    assert body["expires_in_seconds"] == 42


@pytest.mark.asyncio
async def test_verify_unknown_challenge_returns_400(monkeypatch):
    provider = SimpleNamespace(get_challenge=lambda challenge_id: None)
    monkeypatch.setattr(webauthn_module, "get_webauthn_provider", lambda: provider)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.post(
            "/v1/awi/passkey/verify",
            json={"challenge_id": "no-such-challenge", "credential": {}},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "verification_failed"


# ── DOM bridge sessions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_dom_session_returns_201_and_records_owner(monkeypatch):
    bridge = SimpleNamespace(
        create_session=AsyncMock(
            return_value=SimpleNamespace(
                session_id="dom-1", current_url="https://example.com/shop"
            )
        )
    )
    monkeypatch.setattr(bridge_module, "get_playwright_bridge", lambda: bridge)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.post(
            "/v1/awi/dom/session",
            json={"target_url": "https://example.com/shop", "wallet_id": "owner"},
        )

    assert response.status_code == 201
    assert response.json() == {
        "session_id": "dom-1",
        "current_url": "https://example.com/shop",
    }
    assert awi_enhanced_router._DOM_SESSION_WALLETS.get("dom-1") == "owner"
    awi_enhanced_router._DOM_SESSION_WALLETS.pop("dom-1", None)


@pytest.mark.asyncio
async def test_create_dom_session_at_limit_returns_429(monkeypatch):
    bridge = SimpleNamespace(
        create_session=AsyncMock(
            side_effect=BrowserSessionLimitExceeded("too many sessions")
        )
    )
    monkeypatch.setattr(bridge_module, "get_playwright_bridge", lambda: bridge)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.post(
            "/v1/awi/dom/session",
            json={"target_url": "https://example.com", "wallet_id": "owner"},
        )

    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "dom_session_limit_exceeded"


@pytest.mark.asyncio
async def test_destroy_unknown_dom_session_returns_404(monkeypatch):
    bridge = SimpleNamespace(get_session=AsyncMock(return_value=None))
    monkeypatch.setattr(bridge_module, "get_playwright_bridge", lambda: bridge)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.delete("/v1/awi/dom/session/dom-missing")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "error": "not_found",
        "message": "Session dom-missing not found",
    }


@pytest.mark.asyncio
async def test_list_dom_sessions_hides_other_wallets(monkeypatch):
    bridge = SimpleNamespace(
        list_sessions=AsyncMock(
            return_value=[{"session_id": "mine"}, {"session_id": "theirs"}]
        )
    )
    monkeypatch.setattr(bridge_module, "get_playwright_bridge", lambda: bridge)
    monkeypatch.setitem(awi_enhanced_router._DOM_SESSION_WALLETS, "mine", "owner")
    monkeypatch.setitem(awi_enhanced_router._DOM_SESSION_WALLETS, "theirs", "other")
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.get("/v1/awi/dom/sessions")

    assert response.status_code == 200
    body = response.json()
    assert body == {"sessions": [{"session_id": "mine"}], "count": 1}


@pytest.mark.asyncio
async def test_get_dom_state_returns_representation(monkeypatch):
    bridge = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(session_id="dom-1", current_url="https://x")
        ),
        extract_state_representation=AsyncMock(
            return_value={
                "url": "https://example.com",
                "title": "Shop",
                "page_type": "shopping",
                "main_content": "hello",
                "forms": [],
                "navigation": [],
            }
        ),
    )
    monkeypatch.setattr(bridge_module, "get_playwright_bridge", lambda: bridge)
    monkeypatch.setitem(awi_enhanced_router._DOM_SESSION_WALLETS, "dom-1", "owner")
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.get("/v1/awi/dom/state/dom-1")

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://example.com"
    assert body["title"] == "Shop"
    assert body["page_type"] == "shopping"


@pytest.mark.asyncio
async def test_preview_invalid_action_returns_400(monkeypatch):
    bridge = SimpleNamespace(
        get_session=AsyncMock(return_value=SimpleNamespace(session_id="dom-1")),
        preview_action=AsyncMock(side_effect=ValueError("unknown action")),
    )
    monkeypatch.setattr(bridge_module, "get_playwright_bridge", lambda: bridge)
    monkeypatch.setitem(awi_enhanced_router._DOM_SESSION_WALLETS, "dom-1", "owner")
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.post(
            "/v1/awi/dom/preview",
            json={"session_id": "dom-1", "action": "bogus", "parameters": {}},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "preview_failed"


# ── RAG memories ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_memories_lists_memories(monkeypatch):
    memory = SimpleNamespace(
        memory_id="mem-1",
        session_type="shopping",
        user_intent="buy shoes",
        action_sequence=["search", "add_to_cart"],
        key_entities=["shoes"],
        created_at=datetime(2026, 1, 1),
    )
    rag = SimpleNamespace(get_session_memories=AsyncMock(return_value=[memory]))
    monkeypatch.setattr(rag_module, "get_awi_rag_engine", lambda: rag)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.get("/v1/awi/rag/sessions/sess-1/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["memories"][0]["memory_id"] == "mem-1"
    assert body["memories"][0]["action_count"] == 2


@pytest.mark.asyncio
async def test_delete_session_memories_reports_count(monkeypatch):
    rag = SimpleNamespace(delete_session_memories=AsyncMock(return_value=3))
    monkeypatch.setattr(rag_module, "get_awi_rag_engine", lambda: rag)
    async with _make_client(_owner_auth(), monkeypatch) as client:
        response = await client.delete("/v1/awi/rag/sessions/sess-1/memories")

    assert response.status_code == 200
    assert response.json() == {"session_id": "sess-1", "deleted_count": 3}


@pytest.mark.asyncio
async def test_rag_stats_requires_bootstrap_admin(monkeypatch):
    rag = SimpleNamespace(get_stats=AsyncMock(return_value={"total_memories": 5}))
    monkeypatch.setattr(rag_module, "get_awi_rag_engine", lambda: rag)

    async with _make_client(_owner_auth(), monkeypatch) as client:
        denied = await client.get("/v1/awi/rag/stats")
    assert denied.status_code == 403

    async with _make_client(_admin_auth(), monkeypatch) as client:
        allowed = await client.get("/v1/awi/rag/stats")
    assert allowed.status_code == 200
    assert allowed.json() == {"total_memories": 5}
