"""Operator boundary, immutable evidence and interrupted compensation proofs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.support import db_gate
from app.main import app
from app.db.database import get_session_factory
from app.db.models import ControlPlaneAuditEventModel, ReceiptResolutionModel
from app.services import operator_resolution as service
from app.services.agent_money import get_agent_money
from app.services.mcp_dispatch_reconciliation import (
    get_mcp_dispatch_reconciliation_service,
)
from app.services.receipts import get_receipt_service
from b2a_sdk.receipt_verifier import key_set_from_document
from b2a_sdk.resolution_verifier import verify_resolution
from tests.test_trust_helpers import BOOTSTRAP_HEADERS
from tests.test_mcp_dispatch_reconciliation import (
    _seed_attempt,
    _ledger_counts,
    _replay,
    _attempt,
)


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def lookup_registry(monkeypatch):
    monkeypatch.setattr(service, "_LOOKUPS", {})


def body(outcome="confirmed_success", *, refund=False, lookup=False):
    return {
        "outcome": outcome,
        "refund_credits": refund,
        "use_lookup": lookup,
        "evidence": {
            "source": "operator",
            "upstream_reference_ids": [],
            "observed_at": "2026-11-14T12:00:00Z",
            "note": "Reviewed authoritative upstream state; sender stopped.",
        },
    }


async def uncertain(client, suffix):
    seed = await _seed_attempt(client, suffix=suffix, state="dispatched")
    result = await get_mcp_dispatch_reconciliation_service().reconcile(idle_seconds=300)
    assert result.dispatched_uncertain == 1
    attempt = await _attempt(seed.attempt_id)
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        attempt.idempotency_record_id
    )
    assert receipt.outcome == "delivery_uncertain"
    return seed, receipt


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "refund"),
    [
        ("confirmed_success", False),
        ("confirmed_not_delivered", False),
        ("confirmed_not_delivered", True),
    ],
)
async def test_operator_resolution_replay_and_original_unchanged(
    client, clean_database, outcome, refund
):
    seed, receipt = await uncertain(client, f"operator-{outcome}-{refund}")
    path = f"/v1/admin/receipts/{receipt.receipt_id}/resolution"
    original = await get_receipt_service().signing_input(receipt.receipt_id)
    replay_before = await _replay(seed)
    request = body(outcome, refund=refund)
    first = await client.post(path, headers=BOOTSTRAP_HEADERS, json=request)
    assert first.status_code == 200, first.text
    second = await client.post(path, headers=BOOTSTRAP_HEADERS, json=request)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert first.json()["version"] == "resolution-v0.2"
    assert await _ledger_counts(seed.wallet_id) == (1, int(refund))
    assert await get_receipt_service().signing_input(receipt.receipt_id) == original
    assert (
        await get_receipt_service().get_receipt(receipt.receipt_id)
    ).signature == receipt.signature
    assert await _replay(seed) == replay_before
    if refund:
        attempt = await _attempt(seed.attempt_id)
        assert attempt.budget_released_at is not None
        assert attempt.debit_refunded_at is not None
        assert (
            first.json()["evidence"]["refund_entry_id"]
            == f"refund-{seed.ledger_entry_id}"
        )
    portable = (
        await client.get(
            f"/v1/receipts/{receipt.receipt_id}/portable", headers=BOOTSTRAP_HEADERS
        )
    ).json()
    keys = key_set_from_document(
        (await client.get("/.well-known/trust-keys.json")).json()
    )
    assert verify_resolution(first.json(), portable, keys).ok
    tampered = deepcopy(first.json())
    tampered["evidence"]["note"] += "changed"
    assert not verify_resolution(tampered, portable, keys).ok
    changed = deepcopy(request)
    changed["evidence"]["note"] += " changed"
    assert (
        await client.post(path, headers=BOOTSTRAP_HEADERS, json=changed)
    ).status_code == 409
    changed = deepcopy(request)
    changed["outcome"] = (
        "confirmed_not_delivered"
        if outcome == "confirmed_success"
        else "confirmed_success"
    )
    assert (
        await client.post(path, headers=BOOTSTRAP_HEADERS, json=changed)
    ).status_code == 409
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ReceiptResolutionModel)
            )
            == 1
        )
        audits = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel).where(
                        ControlPlaneAuditEventModel.event
                        == "receipt.resolution.attempt"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 4
        assert all(json.loads(row.metadata_json)["operator_id"] for row in audits)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["api_key", "demo", "permit", "wallet"])
async def test_non_admin_refused(client, clean_database, monkeypatch, source):
    from app.core.auth import AuthContext, get_auth_context

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        source=source, raw_key="test", wallet_id="test-wallet"
    )
    try:
        response = await client.post(
            "/v1/admin/receipts/missing/resolution", json=body()
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_auth_context)


@pytest.mark.anyio
async def test_interrupt_after_append_retry_finishes_refund(
    client, clean_database, monkeypatch
):
    seed, receipt = await uncertain(client, "operator-interrupt")
    money = get_agent_money()
    original = money.refund_charge

    async def fail(**kwargs):
        raise RuntimeError("injected interruption")

    monkeypatch.setattr(money, "refund_charge", fail)
    path = f"/v1/admin/receipts/{receipt.receipt_id}/resolution"
    request = body("confirmed_not_delivered", refund=True)
    assert (
        await client.post(path, headers=BOOTSTRAP_HEADERS, json=request)
    ).status_code == 503
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    monkeypatch.setattr(money, "refund_charge", original)
    for _ in range(2):
        response = await client.post(path, headers=BOOTSTRAP_HEADERS, json=request)
        assert response.status_code == 200, response.text
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ReceiptResolutionModel)
            )
            == 1
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "unknown",
        "not_executed",
        "malformed",
        "error",
        "timeout",
        "unregistered",
    ],
)
async def test_inconclusive_writes_no_resolution(
    client, clean_database, monkeypatch, status
):
    seed, receipt = await uncertain(client, f"operator-{status}")

    async def lookup(query):
        if status == "timeout":
            await asyncio.sleep(1)
        if status == "error":
            raise RuntimeError("injected")
        return status

    if status != "unregistered":
        service.register_receipt_status_lookup("https://partner.example", lookup)
    monkeypatch.setattr(service, "STATUS_LOOKUP_TIMEOUT_SECONDS", 0.001)
    response = await client.post(
        f"/v1/admin/receipts/{receipt.receipt_id}/resolution",
        headers=BOOTSTRAP_HEADERS,
        json=body("confirmed_not_delivered", refund=True, lookup=True),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "resolution_inconclusive"
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ReceiptResolutionModel)
            )
            == 0
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "origin", ["https://partner.example", "HTTPS://PARTNER.EXAMPLE:443/"]
)
async def test_fixture_lookup_success_and_stable_retry(client, clean_database, origin):
    seed, receipt = await uncertain(client, "operator-lookup")
    calls = []

    def handler(request):
        assert request.url.path == "/status"
        assert request.headers["authorization"] == "Bearer test-token"
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "executed", "token": "test-token"})

    service.register_receipt_status_lookup(
        origin,
        service.RefundFixtureStatusLookup(
            origin,
            "test-token",
            transport=httpx.MockTransport(handler),
        ),
    )
    path = f"/v1/admin/receipts/{receipt.receipt_id}/resolution"
    for _ in range(2):
        response = await client.post(
            path, headers=BOOTSTRAP_HEADERS, json=body(lookup=True)
        )
        assert response.status_code == 200, response.text
        assert "test-token" not in response.text
        assert response.json()["evidence"]["lookup_status"] == "executed"
    assert len(calls) == 1
    assert calls[0]["idempotency_key"] == seed.idempotency_key
    assert (
        calls[0]["invocation_id"]
        == (await _attempt(seed.attempt_id)).idempotency_record_id
    )


@pytest.mark.anyio
async def test_lost_response_restart_retry_and_offline_operator_resolution(
    client, clean_database, monkeypatch
):
    from fastapi import FastAPI
    from app.routers import mcp, receipts
    from app.services import (
        idempotency,
        mcp_dispatch_attempts,
        receipts as receipt_module,
        agent_money,
    )
    from app.services.service_registry import get_service_registry
    from tests.test_mcp_upstream_governed import (
        FakeUpstreamExecutor,
        _register_upstream,
        _call_body,
    )
    from tests.test_trust_helpers import provision_agent_wallet, create_tool_permit

    provisioned = await provision_agent_wallet(client)
    tool = "operator-restart-tool"
    executor = FakeUpstreamExecutor("delivery_uncertain")
    _register_upstream(tool, executor)
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool,
            idem_key="operator-restart-permit",
        )
        request = _call_body(
            tool_name=tool,
            wallet_id=provisioned["agent_wallet_id"],
            permit_id=permit["permit_id"],
            idempotency_key="operator-restart-invoke",
        )
        first = await client.post(
            "/mcp/messages", headers=provisioned["agent_headers"], json=request
        )
        error = first.json()["error"]
        receipt_id = error["data"]["receipt"]["receipt_id"]
        portable = (
            await client.get(
                f"/v1/receipts/{receipt_id}/portable", headers=BOOTSTRAP_HEADERS
            )
        ).json()
        # Fresh application and fresh service objects, same durable database.
        monkeypatch.setattr(idempotency, "_service", None)
        monkeypatch.setattr(mcp_dispatch_attempts, "_service", None)
        monkeypatch.setattr(receipt_module, "_service", None)
        monkeypatch.setattr(agent_money, "_agent_money", None)
        fresh = FastAPI()
        fresh.include_router(mcp.router)
        fresh.include_router(receipts.router)
        fresh.include_router(receipts.admin_router)
        service.register_receipt_status_lookup(
            "https://partner.example",
            service.RefundFixtureStatusLookup(
                "https://partner.example",
                "test",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"status": "executed"})
                ),
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=fresh), base_url="http://test"
        ) as restarted:
            replay = await restarted.post(
                "/mcp/messages", headers=provisioned["agent_headers"], json=request
            )
            assert replay.json()["error"]["code"] == error["code"]
            assert replay.json()["error"]["data"]["receipt"] == error["data"]["receipt"]
            assert (
                replay.json()["error"]["data"]["dispatch"] == error["data"]["dispatch"]
            )
            resolved = await restarted.post(
                f"/v1/admin/receipts/{receipt_id}/resolution",
                headers=BOOTSTRAP_HEADERS,
                json=body(lookup=True),
            )
            assert resolved.status_code == 200, resolved.text
            after = await restarted.get(
                f"/v1/receipts/{receipt_id}/portable", headers=BOOTSTRAP_HEADERS
            )
            assert after.json() == portable
            replay_after = await restarted.post(
                "/mcp/messages", headers=provisioned["agent_headers"], json=request
            )
            assert (
                replay_after.json()["error"]["data"]["receipt"]
                == error["data"]["receipt"]
            )
        keys = key_set_from_document(
            (await client.get("/.well-known/trust-keys.json")).json()
        )
        assert verify_resolution(resolved.json(), portable, keys).ok
        assert executor.dispatch_count == 1
        assert await _ledger_counts(provisioned["agent_wallet_id"]) == (1, 0)
    finally:
        get_service_registry().unregister_local(tool)


@pytest.mark.asyncio(loop_scope="session")
@db_gate.collection_mark(
    flag="RUN_POSTGRES_CONCURRENCY_TESTS", reason="requires disposable PostgreSQL"
)
async def test_two_operators_racing_converge(monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from app.db import database
    from app.main import app
    from tests.test_operator_resolution import uncertain, body
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS
    from tests.test_mcp_dispatch_reconciliation import _ledger_counts

    with monkeypatch.context() as patch:
        patch.setattr(database, "_engine", None)
        patch.setattr(database, "_session_factory", None)
        engine = database.get_engine()
        try:
            db_gate.require_live_postgres(
                flag="RUN_POSTGRES_CONCURRENCY_TESTS",
                reason="requires disposable PostgreSQL",
            )
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                seed, receipt = await uncertain(client, "operator-postgres-race")
                path = f"/v1/admin/receipts/{receipt.receipt_id}/resolution"
                responses = await asyncio.gather(
                    *(
                        client.post(
                            path,
                            headers=BOOTSTRAP_HEADERS,
                            json=body("confirmed_not_delivered", refund=True),
                        )
                        for _ in range(2)
                    )
                )
                assert [response.status_code for response in responses] == [200, 200], [
                    r.text for r in responses
                ]
                assert responses[0].json() == responses[1].json()
                assert await _ledger_counts(seed.wallet_id) == (1, 1)
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_real_agent_key_refused_and_evidence_bounded(client, clean_database):
    from tests.test_trust_helpers import provision_agent_wallet

    provisioned = await provision_agent_wallet(client)
    response = await client.post(
        "/v1/admin/receipts/missing/resolution",
        headers=provisioned["agent_headers"],
        json=body(),
    )
    assert response.status_code == 403
    seed, receipt = await uncertain(client, "operator-large-evidence")
    request = body()
    request["evidence"]["raw_lookup_answer"] = {"data": "x" * 16384}
    response = await client.post(
        f"/v1/admin/receipts/{receipt.receipt_id}/resolution",
        headers=BOOTSTRAP_HEADERS,
        json=request,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "resolution_evidence_invalid"
    assert await _ledger_counts(seed.wallet_id) == (1, 0)


@pytest.mark.anyio
async def test_interrupt_after_refund_before_budget_release(
    client, clean_database, monkeypatch
):
    from app.services.permits import get_permit_service

    seed, receipt = await uncertain(client, "operator-after-refund")
    permits = get_permit_service()
    release = permits.release_dispatch_budget_once

    async def fail(*args, **kwargs):
        raise RuntimeError("injected interruption after refund")

    monkeypatch.setattr(permits, "release_dispatch_budget_once", fail)
    path = f"/v1/admin/receipts/{receipt.receipt_id}/resolution"
    request = body("confirmed_not_delivered", refund=True)
    assert (
        await client.post(path, headers=BOOTSTRAP_HEADERS, json=request)
    ).status_code == 503
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    monkeypatch.setattr(permits, "release_dispatch_budget_once", release)
    response = await client.post(path, headers=BOOTSTRAP_HEADERS, json=request)
    assert response.status_code == 200, response.text
    assert await _ledger_counts(seed.wallet_id) == (1, 1)
    assert (await _attempt(seed.attempt_id)).budget_released_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"status": []},
        {"status": "unknown"},
        {"status": "pending"},
        {"status": "not_executed"},
    ],
)
async def test_fixture_adapter_inconclusive(client, clean_database, answer):
    seed, receipt = await uncertain(client, "operator-fixture-inconclusive")
    service.register_receipt_status_lookup(
        "https://partner.example",
        service.RefundFixtureStatusLookup(
            "https://partner.example",
            "test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=answer)
            ),
        ),
    )
    response = await client.post(
        f"/v1/admin/receipts/{receipt.receipt_id}/resolution",
        headers=BOOTSTRAP_HEADERS,
        json=body("confirmed_not_delivered", lookup=True, refund=True),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "resolution_inconclusive"
    assert await _ledger_counts(seed.wallet_id) == (1, 0)


@pytest.mark.anyio
async def test_no_attempt_refund_rejected_before_intent(client, clean_database):
    from app.db.models import ReceiptModel

    seed, receipt = await uncertain(client, "operator-no-attempt")
    async with get_session_factory()() as session:
        model = await session.get(ReceiptModel, receipt.receipt_id)
        model.dispatch_attempt_id = None
        model.idempotency_record_id = None
        session.add(model)
        await session.commit()
    response = await client.post(
        f"/v1/admin/receipts/{receipt.receipt_id}/resolution",
        headers=BOOTSTRAP_HEADERS,
        json=body("confirmed_not_delivered", refund=True),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "resolution_refund_unsupported"
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ReceiptResolutionModel)
            )
            == 0
        )


@pytest.mark.anyio
@pytest.mark.parametrize("complete", [False, True])
async def test_budget_reconcile_requires_completed_operator_refund(
    client, clean_database, monkeypatch, complete
):
    from datetime import timedelta
    from decimal import Decimal
    from app.core.time import utc_now
    from app.db.models import PermitModel
    from app.services.permits import get_permit_service

    seed, receipt = await uncertain(client, f"operator-budget-{complete}")
    if not complete:

        async def fail(**kwargs):
            raise RuntimeError("interrupted refund")

        monkeypatch.setattr(get_agent_money(), "refund_charge", fail)
    response = await client.post(
        f"/v1/admin/receipts/{receipt.receipt_id}/resolution",
        headers=BOOTSTRAP_HEADERS,
        json=body("confirmed_not_delivered", refund=True),
    )
    assert response.status_code == (200 if complete else 503)
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, seed.permit_id)
        expected = Decimal("0") if complete else receipt.credits_charged
        assert permit.spent_credits == expected
        permit.expires_at = utc_now() - timedelta(hours=1)
        permit.updated_at = utc_now() - timedelta(hours=1)
        session.add(permit)
        await session.commit()
    await get_permit_service().reconcile_budgets(idle_seconds=1)
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, seed.permit_id)
        assert permit.spent_credits == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reason", ["dispatch_permit_missing", "dispatch_approval_binding_mismatch"]
)
async def test_lookup_context_conflict_is_audited_inconclusive(
    client, clean_database, monkeypatch, reason
):
    from app.services.mcp_dispatch_attempts import (
        DispatchAttemptConflictError,
        get_mcp_dispatch_attempt_service,
    )

    seed, receipt = await uncertain(client, f"operator-context-{reason}")

    async def fail(*args):
        raise DispatchAttemptConflictError(reason)

    monkeypatch.setattr(get_mcp_dispatch_attempt_service(), "get_context", fail)
    response = await client.post(
        f"/v1/admin/receipts/{receipt.receipt_id}/resolution",
        headers=BOOTSTRAP_HEADERS,
        json=body(lookup=True),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "resolution_inconclusive"
    assert await _ledger_counts(seed.wallet_id) == (1, 0)
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ReceiptResolutionModel)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ControlPlaneAuditEventModel)
                .where(
                    ControlPlaneAuditEventModel.event == "receipt.resolution.attempt"
                )
            )
            == 1
        )
