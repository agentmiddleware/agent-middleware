"""Wallet status, child spend caps, and child TTLs must be authoritative across
the money layer, not only inside charge().

Regression coverage for the finding that a frozen wallet kept spending and
could drain via transfer or spawn a fresh unfrozen child, and that a capped
child escaped its cap through transfer or child-spawn.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import WalletModel
from app.main import app
from app.schemas.billing import InsufficientFundsResponse, ServiceCategory
from app.services.agent_money import get_agent_money
from app.services.wallet_engine import MAX_CHILD_WALLET_TTL_SECONDS

BOOTSTRAP = {"X-API-Key": "test-key"}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_agent(client, *, budget=5000, initial=10000) -> tuple[str, str]:
    sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={"sponsor_name": "S", "email": "s@t.com", "initial_credits": initial},
        headers=BOOTSTRAP,
    )
    spn = sponsor.json()["wallet_id"]
    agent = await client.post(
        "/v1/billing/wallets/agent",
        json={"sponsor_wallet_id": spn, "agent_id": "bot", "budget_credits": budget},
        headers=BOOTSTRAP,
    )
    return spn, agent.json()["wallet_id"]


async def _set_status(wallet_id: str, status: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.execute(
            select(WalletModel).where(WalletModel.wallet_id == wallet_id)
        )
        wallet = row.scalar_one()
        wallet.status = status
        session.add(wallet)
        await session.commit()


async def _make_child(
    client,
    parent_wallet_id: str,
    *,
    ttl_seconds: int | None = 60,
) -> str:
    payload = {
        "parent_wallet_id": parent_wallet_id,
        "child_agent_id": "ttl-child",
        "budget_credits": 100,
        "max_spend": 100,
    }
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds
    response = await client.post(
        "/v1/billing/wallets/child",
        json=payload,
        headers=BOOTSTRAP,
    )
    assert response.status_code == 201, response.text
    return response.json()["wallet_id"]


async def _expire_child(wallet_id: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, wallet_id)
        assert wallet is not None
        assert wallet.ttl_seconds is not None
        wallet.created_at = utc_now() - timedelta(seconds=wallet.ttl_seconds + 1)
        session.add(wallet)
        await session.commit()


@pytest.mark.anyio
async def test_frozen_wallet_cannot_charge(client, clean_database):
    _, agent = await _make_agent(client)
    await _set_status(agent, "frozen")

    headers = {**BOOTSTRAP, "Idempotency-Key": "frozen-wallet-charge-1"}
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent}&service=iot_bridge&units=1",
        headers=headers,
    )
    replay = await client.post(
        f"/v1/billing/charge?wallet_id={agent}&service=iot_bridge&units=1",
        headers=headers,
    )
    assert resp.status_code == 403
    assert replay.status_code == 403
    assert replay.json() == resp.json()
    detail = resp.json()["detail"]
    assert detail["error"] == "wallet_frozen"
    assert detail["wallet_id"] == agent
    assert detail["shortfall"] == 0

    wallet = await client.get(f"/v1/billing/wallets/{agent}", headers=BOOTSTRAP)
    assert wallet.json()["balance"] == 5000
    assert wallet.json()["status"] == "frozen"


@pytest.mark.anyio
async def test_unknown_wallet_status_fails_closed_for_charge(client, clean_database):
    """A future or malformed status must not become spendable by omission."""
    _, agent = await _make_agent(client)
    await _set_status(agent, "operator_hold")

    result = await get_agent_money().charge(
        wallet_id=agent,
        service_category=ServiceCategory.IOT_BRIDGE,
        units=Decimal("1"),
        request_path="/tests/operator-hold",
    )

    assert isinstance(result, InsufficientFundsResponse)
    assert result.error == "insufficient_funds"
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, agent)
        assert wallet is not None
        assert wallet.status == "operator_hold"
        assert wallet.balance == Decimal("5000")
        assert wallet.lifetime_debits == Decimal("0")
        assert wallet.hourly_spent == Decimal("0")
        assert wallet.daily_spent == Decimal("0")


@pytest.mark.anyio
async def test_frozen_wallet_dry_run_commit_reports_wallet_frozen(
    client,
    clean_database,
):
    _, agent = await _make_agent(client)
    session = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": agent},
        headers=BOOTSTRAP,
    )
    assert session.status_code == 201
    session_id = session.json()["session_id"]

    simulated = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": agent,
            "service": "iot_bridge",
            "units": 1,
            "dry_run_session_id": session_id,
        },
        headers=BOOTSTRAP,
    )
    assert simulated.status_code == 200
    assert simulated.json()["would_succeed"] is True

    await _set_status(agent, "frozen")
    committed = await client.post(
        f"/v1/billing/dry-run/session/{session_id}/commit",
        headers=BOOTSTRAP,
    )

    assert committed.status_code == 200
    payload = committed.json()
    assert payload["success"] is False
    assert payload["committed_charges"] == 0
    assert payload["total_credits_deducted"] == 0
    assert payload["ledger_entries"][0]["error"] == "wallet_frozen"
    assert "wallet_frozen" in payload["message"]

    wallet = await client.get(f"/v1/billing/wallets/{agent}", headers=BOOTSTRAP)
    assert wallet.json()["balance"] == 5000
    assert wallet.json()["status"] == "frozen"


@pytest.mark.anyio
async def test_frozen_wallet_cannot_transfer_out(client, clean_database):
    _, agent = await _make_agent(client)
    _, other = await _make_agent(client)
    await _set_status(agent, "frozen")

    resp = await client.post(
        f"/v1/billing/transfer?from_wallet_id={agent}&to_wallet_id={other}&amount=10",
        headers=BOOTSTRAP,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "transfer_error"


@pytest.mark.anyio
async def test_frozen_wallet_cannot_spawn_child(client, clean_database):
    _, agent = await _make_agent(client)
    await _set_status(agent, "frozen")

    resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent,
            "child_agent_id": "sub",
            "budget_credits": 10,
            "max_spend": 10,
        },
        headers=BOOTSTRAP,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "child_wallet_error"


@pytest.mark.anyio
async def test_frozen_sponsor_cannot_provision_agent_with_positive_balance(
    client, clean_database
):
    sponsor, _ = await _make_agent(client, budget=5000, initial=10000)
    await _set_status(sponsor, "frozen")

    response = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor,
            "agent_id": "blocked-after-freeze",
            "budget_credits": 1000,
        },
        headers=BOOTSTRAP,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "wallet_error"
    assert "frozen" in response.json()["detail"]["message"]
    sponsor_response = await client.get(
        f"/v1/billing/wallets/{sponsor}", headers=BOOTSTRAP
    )
    assert sponsor_response.json()["balance"] == 5000
    assert sponsor_response.json()["status"] == "frozen"


@pytest.mark.anyio
async def test_child_cap_enforced_on_transfer(client, clean_database):
    _, agent = await _make_agent(client)
    _, sink = await _make_agent(client)

    child_resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent,
            "child_agent_id": "capped",
            "budget_credits": 100,
            "max_spend": 5,
        },
        headers=BOOTSTRAP,
    )
    assert child_resp.status_code == 201
    child = child_resp.json()["wallet_id"]

    # The child holds 100 credits but is capped at 5 lifetime spend. Moving 10
    # out would exceed the cap and must be rejected, closing the transfer-escape.
    resp = await client.post(
        f"/v1/billing/transfer?from_wallet_id={child}&to_wallet_id={sink}&amount=10",
        headers=BOOTSTRAP,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "transfer_error"


@pytest.mark.anyio
async def test_child_cap_enforced_when_spawning_grandchild(client, clean_database):
    _, agent = await _make_agent(client)
    child_resp = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent,
            "child_agent_id": "capped-parent",
            "budget_credits": 100,
            "max_spend": 5,
        },
        headers=BOOTSTRAP,
    )
    assert child_resp.status_code == 201
    child = child_resp.json()["wallet_id"]

    response = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": child,
            "child_agent_id": "cap-escape",
            "budget_credits": 10,
            "max_spend": 10,
        },
        headers=BOOTSTRAP,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "child_wallet_error"


@pytest.mark.anyio
async def test_expired_child_wallet_cannot_charge(client, clean_database):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent)
    await _expire_child(child)
    headers = {**BOOTSTRAP, "Idempotency-Key": "expired-charge-replay"}

    response = await client.post(
        f"/v1/billing/charge?wallet_id={child}&service=iot_bridge&units=1",
        headers=headers,
    )
    replay = await client.post(
        f"/v1/billing/charge?wallet_id={child}&service=iot_bridge&units=1",
        headers=headers,
    )

    assert response.status_code == 403
    assert replay.status_code == 403
    assert replay.json() == response.json()
    assert set(response.json()) == {"detail"}
    assert response.json()["detail"]["error"] == "wallet_expired"
    assert "expired" in response.json()["detail"]["message"].lower()
    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, child)
        assert wallet is not None
        assert wallet.balance == 100
        assert wallet.lifetime_debits == 0
        assert wallet.hourly_spent == 0
        assert wallet.daily_spent == 0


@pytest.mark.anyio
async def test_expired_child_wallet_cannot_transfer_out(client, clean_database):
    _, agent = await _make_agent(client)
    _, sink = await _make_agent(client)
    child = await _make_child(client, agent)
    await _expire_child(child)
    headers = {**BOOTSTRAP, "Idempotency-Key": "expired-transfer-replay"}

    response = await client.post(
        f"/v1/billing/transfer?from_wallet_id={child}&to_wallet_id={sink}&amount=10",
        headers=headers,
    )
    replay = await client.post(
        f"/v1/billing/transfer?from_wallet_id={child}&to_wallet_id={sink}&amount=10",
        headers=headers,
    )

    assert response.status_code == 403
    assert replay.status_code == 403
    assert replay.json() == response.json()
    assert set(response.json()) == {"detail"}
    assert response.json()["detail"]["error"] == "wallet_expired"
    assert "expired" in response.json()["detail"]["message"].lower()
    source = await client.get(f"/v1/billing/wallets/{child}", headers=BOOTSTRAP)
    destination = await client.get(f"/v1/billing/wallets/{sink}", headers=BOOTSTRAP)
    assert source.json()["balance"] == 100
    assert destination.json()["balance"] == 5000


@pytest.mark.anyio
async def test_expired_child_wallet_cannot_spawn_grandchild(client, clean_database):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent)
    await _expire_child(child)

    response = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": child,
            "child_agent_id": "ttl-escape",
            "budget_credits": 10,
            "max_spend": 10,
        },
        headers=BOOTSTRAP,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "wallet_expired"
    assert "expired" in response.json()["detail"]["message"].lower()
    parent = await client.get(f"/v1/billing/wallets/{child}", headers=BOOTSTRAP)
    assert parent.json()["balance"] == 100
    assert parent.json()["lifetime_debits"] == 0


@pytest.mark.anyio
async def test_child_wallet_ttl_is_attenuated_to_parent_deadline(
    client, clean_database
):
    _, agent = await _make_agent(client)
    parent = await _make_child(client, agent, ttl_seconds=120)
    parent_response = await client.get(
        f"/v1/billing/wallets/{parent}", headers=BOOTSTRAP
    )
    parent_payload = parent_response.json()
    parent_deadline = datetime.fromisoformat(parent_payload["created_at"]) + timedelta(
        seconds=parent_payload["ttl_seconds"]
    )

    for index, requested_ttl in enumerate((None, 3600)):
        payload = {
            "parent_wallet_id": parent,
            "child_agent_id": f"attenuated-{index}",
            "budget_credits": 10,
            "max_spend": 10,
        }
        if requested_ttl is not None:
            payload["ttl_seconds"] = requested_ttl
        response = await client.post(
            "/v1/billing/wallets/child",
            json=payload,
            headers=BOOTSTRAP,
        )

        assert response.status_code == 201, response.text
        child_payload = response.json()
        assert child_payload["ttl_seconds"] is not None
        child_deadline = datetime.fromisoformat(
            child_payload["created_at"]
        ) + timedelta(seconds=child_payload["ttl_seconds"])
        assert child_deadline <= parent_deadline


@pytest.mark.anyio
async def test_expired_ancestor_blocks_legacy_descendant_charge_and_transfer(
    client, clean_database
):
    _, agent = await _make_agent(client)
    _, sink = await _make_agent(client)
    parent = await _make_child(client, agent, ttl_seconds=60)
    grandchild = await _make_child(client, parent, ttl_seconds=None)

    # Simulate a pre-fix descendant that carried no local TTL. Runtime ancestry
    # enforcement must still keep it inside the parent's authority window.
    factory = get_session_factory()
    async with factory() as session:
        grandchild_wallet = await session.get(WalletModel, grandchild)
        assert grandchild_wallet is not None
        grandchild_wallet.ttl_seconds = None
        grandchild_wallet.created_at = utc_now()
        session.add(grandchild_wallet)
        await session.commit()
    await _expire_child(parent)

    charge = await client.post(
        f"/v1/billing/charge?wallet_id={grandchild}&service=iot_bridge&units=1",
        headers=BOOTSTRAP,
    )
    transfer = await client.post(
        f"/v1/billing/transfer?from_wallet_id={grandchild}&to_wallet_id={sink}&amount=10",
        headers=BOOTSTRAP,
    )

    assert charge.status_code == 403
    assert charge.json()["detail"]["error"] == "wallet_expired"
    assert transfer.status_code == 403
    assert transfer.json()["detail"]["error"] == "wallet_expired"
    source = await client.get(f"/v1/billing/wallets/{grandchild}", headers=BOOTSTRAP)
    destination = await client.get(f"/v1/billing/wallets/{sink}", headers=BOOTSTRAP)
    assert source.json()["balance"] == 100
    assert destination.json()["balance"] == 5000


@pytest.mark.anyio
async def test_expired_child_wallet_dry_runs_report_wallet_expired(
    client, clean_database
):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent)
    session_response = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": child},
        headers=BOOTSTRAP,
    )
    assert session_response.status_code == 201
    session_id = session_response.json()["session_id"]
    await _expire_child(child)

    single = await client.post(
        "/v1/billing/dry-run/charge",
        json={"wallet_id": child, "service": "iot_bridge", "units": 1},
        headers=BOOTSTRAP,
    )
    stateful = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": child,
            "service": "iot_bridge",
            "units": 1,
            "dry_run_session_id": session_id,
        },
        headers=BOOTSTRAP,
    )

    for response in (single, stateful):
        assert response.status_code == 200
        assert response.json()["would_succeed"] is False
        assert response.json()["reason"] == "wallet_expired"

    session_state = await client.get(
        f"/v1/billing/dry-run/session/{session_id}",
        headers=BOOTSTRAP,
    )
    assert session_state.json()["charge_count"] == 0
    assert session_state.json()["virtual_balance"] == 100


@pytest.mark.anyio
async def test_dry_run_commit_reports_wallet_expired_when_session_crosses_deadline(
    client, clean_database
):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent)
    session_response = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": child},
        headers=BOOTSTRAP,
    )
    assert session_response.status_code == 201
    session_id = session_response.json()["session_id"]
    simulated = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": child,
            "service": "iot_bridge",
            "units": 1,
            "dry_run_session_id": session_id,
        },
        headers=BOOTSTRAP,
    )
    assert simulated.status_code == 200
    assert simulated.json()["would_succeed"] is True
    await _expire_child(child)

    committed = await client.post(
        f"/v1/billing/dry-run/session/{session_id}/commit",
        headers=BOOTSTRAP,
    )

    assert committed.status_code == 200
    assert committed.json()["success"] is False
    assert committed.json()["committed_charges"] == 0
    assert committed.json()["total_credits_deducted"] == 0
    assert committed.json()["ledger_entries"][0]["committed"] is False
    assert committed.json()["ledger_entries"][0]["error"] == "wallet_expired"
    wallet = await client.get(f"/v1/billing/wallets/{child}", headers=BOOTSTRAP)
    assert wallet.json()["balance"] == 100


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_ttl",
    [0, -1, MAX_CHILD_WALLET_TTL_SECONDS + 1, True],
)
async def test_child_wallet_ttl_rejects_unsafe_values(
    client, clean_database, invalid_ttl
):
    _, agent = await _make_agent(client)
    response = await client.post(
        "/v1/billing/wallets/child",
        json={
            "parent_wallet_id": agent,
            "child_agent_id": "oversized-ttl",
            "budget_credits": 10,
            "max_spend": 10,
            "ttl_seconds": invalid_ttl,
        },
        headers=BOOTSTRAP,
    )

    assert response.status_code == 422
    with pytest.raises(ValueError, match="ttl_seconds must be between"):
        await get_agent_money().create_child_wallet(
            parent_wallet_id=agent,
            child_agent_id="service-oversized-ttl",
            budget_credits=10,
            max_spend=10,
            ttl_seconds=invalid_ttl,
        )

    parent = await client.get(f"/v1/billing/wallets/{agent}", headers=BOOTSTRAP)
    assert parent.json()["balance"] == 5000


def test_charge_openapi_documents_wallet_expired_denial():
    response = app.openapi()["paths"]["/v1/billing/charge"]["post"]["responses"]["403"]

    assert "expired" in response["description"].lower()


def test_child_wallet_openapi_documents_wallet_expired_denial():
    response = app.openapi()["paths"]["/v1/billing/wallets/child"]["post"]["responses"][
        "403"
    ]

    assert "expired" in response["description"].lower()


def test_transfer_openapi_documents_wallet_expired_denial():
    response = app.openapi()["paths"]["/v1/billing/transfer"]["post"]["responses"][
        "403"
    ]

    assert "expired" in response["description"].lower()


@pytest.mark.anyio
async def test_unexpired_child_wallet_still_operates(client, clean_database):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent, ttl_seconds=60)

    response = await client.post(
        f"/v1/billing/charge?wallet_id={child}&service=iot_bridge&units=1",
        headers=BOOTSTRAP,
    )

    assert response.status_code == 200
    assert response.json()["balance_after"] == 98


@pytest.mark.anyio
async def test_child_wallet_without_ttl_still_operates(client, clean_database):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent, ttl_seconds=None)
    factory = get_session_factory()
    async with factory() as session:
        wallet = await session.get(WalletModel, child)
        assert wallet is not None
        wallet.created_at = utc_now() - timedelta(days=365)
        session.add(wallet)
        await session.commit()

    response = await client.post(
        f"/v1/billing/charge?wallet_id={child}&service=iot_bridge&units=1",
        headers=BOOTSTRAP,
    )

    assert response.status_code == 200
    assert response.json()["balance_after"] == 98


@pytest.mark.anyio
async def test_expired_child_wallet_can_still_be_reclaimed(client, clean_database):
    _, agent = await _make_agent(client)
    child = await _make_child(client, agent)
    await _expire_child(child)

    response = await client.post(
        f"/v1/billing/wallets/{child}/reclaim",
        headers=BOOTSTRAP,
    )

    assert response.status_code == 200
    assert response.json()["credits_reclaimed"] == 100
    assert response.json()["child_status"] == "closed"


@pytest.mark.anyio
async def test_active_wallet_still_operates(client, clean_database):
    """The enforcement must not block a normal active wallet."""
    _, agent = await _make_agent(client)
    resp = await client.post(
        f"/v1/billing/charge?wallet_id={agent}&service=iot_bridge&units=1",
        headers=BOOTSTRAP,
    )
    assert resp.status_code == 200
