"""Issue 354: /dom/sync signs a stable reason_code, never bridge free text.

The bridge's "No page available for session <uuid>" used to become the
signed receipt's reason_code verbatim (sanitized), session id included, and
the call kept its charge as delivery_uncertain although no command ran.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel
from app.main import app
from app.routers.awi_enhanced import _DOM_SESSION_WALLETS
from app.services import awi_playwright_bridge
from app.services.awi_http_governance import _stable_awi_failure_reason
from app.services.awi_playwright_bridge import ExecutionResult, get_playwright_bridge
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

pytestmark = [pytest.mark.proof, pytest.mark.anyio]

PATH = "/v1/awi/dom/sync"


@pytest.fixture
async def client():
    http_app = FastAPI()
    http_app.include_router(app.router)
    async with AsyncClient(
        transport=ASGITransport(app=http_app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def dom_case(client, clean_database, monkeypatch):
    # The outbound guard resolves DNS; a reserved name never resolves. The
    # browser layer is absent, which is the bridge's mock mode: no page.
    monkeypatch.setattr(
        awi_playwright_bridge, "check_outbound_url", AsyncMock(return_value=None)
    )
    bridge = get_playwright_bridge()
    monkeypatch.setattr(bridge, "_init_playwright", AsyncMock(return_value=None))
    monkeypatch.setattr(bridge, "_create_browser_context", AsyncMock(return_value=None))
    actor = await provision_agent_wallet(client)
    wallet = actor["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet,
        key_id=actor["key_id"],
        tool_name="awi_dom_sync",
        max_credits=50,
        idem_key="permit-dom-sync-reason",
    )
    created = await client.post(
        "/v1/awi/dom/session",
        json={"target_url": "https://synthetic.invalid", "wallet_id": wallet},
        headers=actor["agent_headers"],
    )
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    headers = {
        **actor["agent_headers"],
        "X-Wallet-Id": wallet,
        "X-Permit-Id": permit["permit_id"],
    }
    try:
        yield wallet, permit["permit_id"], sid, headers
    finally:
        await bridge.destroy_session(sid)
        _DOM_SESSION_WALLETS.pop(sid, None)


async def _ledger(wallet):
    async with get_session_factory()() as db:
        return (
            (
                await db.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == wallet,
                        LedgerEntryModel.request_path == "POST " + PATH,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _receipt(client, headers, receipt_id):
    resp = await client.get(f"/v1/receipts/{receipt_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _body(sid):
    return {
        "session_id": sid,
        "action": "navigate_to",
        "parameters": {"url": "https://synthetic.invalid/next"},
    }


async def test_no_page_is_a_stable_code_and_refunded(client, dom_case):
    wallet, permit_id, sid, headers = dom_case
    headers = {**headers, "Idempotency-Key": "dom-no-page"}
    first = await client.post(PATH, json=_body(sid), headers=headers)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "error"
    assert data["commands_executed"] == 0
    assert data["effect_status"] == "not_dispatched"
    assert data["error"].startswith("no_page_available: ")
    assert data["receipt"]["outcome"] == "failed_refunded"

    receipt = await _receipt(client, headers, data["receipt"]["receipt_id"])
    assert receipt["reason_code"] == "no_page_available"
    assert sid not in receipt["reason_code"]
    assert float(receipt["credits_charged"]) == 0

    ledger = await _ledger(wallet)
    assert len(ledger) == 2 and sum(entry.amount for entry in ledger) == 0
    async with get_session_factory()() as db:
        assert (await db.get(PermitModel, permit_id)).spent_credits == 0

    replay = await client.post(PATH, json=_body(sid), headers=headers)
    assert replay.json() == first.json()
    assert len(await _ledger(wallet)) == 2


async def test_failed_command_keeps_charge_with_stable_code(
    client, dom_case, monkeypatch
):
    wallet, _, sid, headers = dom_case
    bridge = get_playwright_bridge()

    async def fails_after_dispatch(*, session_id, commands):
        return ExecutionResult(
            success=False,
            commands_executed=0,
            new_url=None,
            new_title=None,
            error=f"Timeout waiting on page for session {session_id}",
            error_code="dom_command_failed",
        )

    monkeypatch.setattr(bridge, "execute_commands", fails_after_dispatch)
    headers = {**headers, "Idempotency-Key": "dom-command-failed"}
    resp = await client.post(PATH, json=_body(sid), headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # A command reached the page path; zero completions do not prove no effect.
    assert data["effect_status"] == "unknown"
    assert data["receipt"]["outcome"] == "delivery_uncertain"
    receipt = await _receipt(client, headers, data["receipt"]["receipt_id"])
    assert receipt["reason_code"] == "dom_command_failed"
    ledger = await _ledger(wallet)
    assert len(ledger) == 1 and ledger[0].amount < 0


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("error", "dom_bridge_failed: target closed", "dom_bridge_failed"),
        ("error", "dom_bridge_failed", "dom_bridge_failed"),
        ("passkey_required", None, "passkey_required"),
        (
            "error",
            "No page available for session fb4c127c-1c0d-43ba-a8af-2fc0de60dd8a",
            "awi_failed",
        ),
        ("error", "Session 1f0e not found", "awi_failed"),
        ("error", "fb4c127c-1c0d-43ba-a8af-2fc0de60dd8a", "awi_failed"),
        ("error", "", "error"),
        ("error", None, "error"),
    ],
)
def test_reason_is_a_code_or_the_fallback(status, error, expected):
    assert _stable_awi_failure_reason(status, error) == expected
