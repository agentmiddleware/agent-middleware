from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import audit as audit_router
from app.services import audit_log
from app.services.audit_log import record_audit_event


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_bootstrap_admin_can_list_audit_events(client, clean_database):
    await record_audit_event(
        event="mcp.invoke",
        wallet_id="wallet-1",
        tool="echo",
        endpoint="/mcp/messages",
        auth_source="db",
        key_id="key-1",
        policy_decision_id="pol-1",
        request_id="req-1",
        ok=True,
        metadata={"cost": 2.0},
    )

    response = await client.get(
        "/v1/audit/events?wallet_id=wallet-1",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["events"][0]["wallet_id"] == "wallet-1"
    assert data["events"][0]["metadata"]["cost"] == 2.0


@pytest.mark.anyio
async def test_bootstrap_admin_can_filter_paginate_and_summarize_audit_events(
    client,
    clean_database,
):
    await record_audit_event(
        event="billing.charge",
        wallet_id="wallet-1",
        tool="billing",
        endpoint="/v1/billing/charge",
        auth_source="bootstrap",
        key_id="key-1",
        request_id="req-billing-1",
        ok=True,
        metadata={"credits": "2.0"},
    )
    await record_audit_event(
        event="planner.optimize",
        wallet_id="wallet-1",
        tool="planner",
        endpoint="/v1/planner/optimize",
        auth_source="bootstrap",
        key_id="key-1",
        request_id="req-planner-1",
        ok=False,
        error="infeasible",
        metadata={"status": "Infeasible"},
    )
    await record_audit_event(
        event="sandbox.evaluate",
        wallet_id="wallet-2",
        tool="sandbox",
        endpoint="/v1/sandbox/environments/env-1/evaluate",
        auth_source="bootstrap",
        key_id="key-2",
        request_id="req-sandbox-1",
        ok=True,
    )

    response = await client.get(
        "/v1/audit/events?wallet_id=wallet-1&limit=1&offset=1&summary=true",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert data["limit"] == 1
    assert data["offset"] == 1
    assert len(data["events"]) == 1
    assert data["events"][0]["wallet_id"] == "wallet-1"
    assert data["summary"] == {
        "total": 2,
        "ok": 1,
        "failed": 1,
        "by_event": {
            "billing.charge": 1,
            "planner.optimize": 1,
        },
    }


@pytest.mark.anyio
async def test_db_wallet_key_can_list_own_wallet_audit_events(client, clean_database):
    sponsor_response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Audit Route Test Corp",
            "email": "audit-route@test.com",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert sponsor_response.status_code == 201
    wallet_id = sponsor_response.json()["wallet_id"]

    key_response = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": wallet_id,
            "key_name": "audit-route-test-key",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert key_response.status_code == 201
    wallet_api_key = key_response.json()["api_key"]
    key_id = key_response.json()["key_id"]

    await record_audit_event(
        event="billing.charge",
        wallet_id=wallet_id,
        tool="billing",
        endpoint="/v1/billing/charge",
        auth_source="db",
        key_id=key_id,
        request_id="req-own-wallet-audit",
        ok=True,
    )
    await record_audit_event(
        event="mcp.invoke",
        wallet_id=wallet_id,
        tool="echo",
        endpoint="/mcp/messages",
        auth_source="bootstrap",
        key_id=None,
        request_id="req-own-wallet-admin-audit",
        ok=True,
    )
    await record_audit_event(
        event="billing.charge",
        wallet_id="wallet-other",
        tool="billing",
        endpoint="/v1/billing/charge",
        auth_source="db",
        key_id="key-other",
        request_id="req-other-wallet-audit",
        ok=True,
    )

    response = await client.get(
        f"/v1/audit/events?wallet_id={wallet_id}",
        headers={"X-API-Key": wallet_api_key},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert {event["wallet_id"] for event in data["events"]} == {wallet_id}
    assert {event["request_id"] for event in data["events"]} == {
        "req-own-wallet-audit",
        "req-own-wallet-admin-audit",
    }


@pytest.mark.anyio
async def test_db_wallet_key_unscoped_audit_list_is_scoped_to_itself(
    client, clean_database
):
    """An unscoped list from a wallet key means "my events", not "everyone's"."""
    sponsor_response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Audit Route Test Corp",
            "email": "audit-route-global@test.com",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert sponsor_response.status_code == 201
    wallet_id = sponsor_response.json()["wallet_id"]

    key_response = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": wallet_id,
            "key_name": "audit-route-global-test-key",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert key_response.status_code == 201
    wallet_api_key = key_response.json()["api_key"]

    response = await client.get(
        "/v1/audit/events",
        headers={"X-API-Key": wallet_api_key},
    )

    assert response.status_code == 200
    # Whatever it returns belongs to this wallet and no other.
    assert all(event["wallet_id"] == wallet_id for event in response.json()["events"])


@pytest.mark.anyio
async def test_db_wallet_key_cannot_list_other_wallet_audit_events(
    client,
    clean_database,
):
    sponsor_response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Audit Route Test Corp",
            "email": "audit-route-cross-wallet@test.com",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert sponsor_response.status_code == 201
    wallet_id = sponsor_response.json()["wallet_id"]

    key_response = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": wallet_id,
            "key_name": "audit-route-cross-wallet-test-key",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert key_response.status_code == 201
    wallet_api_key = key_response.json()["api_key"]

    response = await client.get(
        "/v1/audit/events?wallet_id=wallet-other",
        headers={"X-API-Key": wallet_api_key},
    )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_db_wallet_key_summary_is_scoped_to_its_own_events(
    client, clean_database
):
    """The summarized form is computed over the same scoped rows."""
    sponsor_response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Audit Route Test Corp",
            "email": "audit-route-summary@test.com",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert sponsor_response.status_code == 201
    wallet_id = sponsor_response.json()["wallet_id"]

    key_response = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": wallet_id,
            "key_name": "audit-route-summary-test-key",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert key_response.status_code == 201
    wallet_api_key = key_response.json()["api_key"]

    response = await client.get(
        f"/v1/audit/events?wallet_id={wallet_id}&summary=true",
        headers={"X-API-Key": wallet_api_key},
    )

    assert response.status_code == 200
    body = response.json()
    # The summary is computed over the same wallet-filtered rows the list
    # returns, so it can only ever describe this wallet.
    assert body["summary"] is not None
    assert body["summary"]["total"] == body["total"]
    assert all(event["wallet_id"] == wallet_id for event in body["events"])

    # Another wallet's summary is still refused outright.
    assert (
        await client.get(
            "/v1/audit/events?wallet_id=wallet-other&summary=true",
            headers={"X-API-Key": wallet_api_key},
        )
    ).status_code == 403


@pytest.mark.anyio
async def test_audit_summary_requires_bootstrap_admin(client, clean_database):
    response = await client.get("/v1/audit/events?summary=true")

    assert response.status_code in (401, 403)


def _cap_audit_list_reads(monkeypatch, cap: int) -> None:
    """Make every audit list read return at most ``cap`` rows.

    Stands in for a table bigger than the summary row cap without writing
    10,000 signed events: a summary still tallied from a capped list read
    undercounts, while one aggregated in SQL does not.
    """
    real_list = audit_log.list_audit_events

    async def capped_list(**kwargs):
        kwargs["limit"] = min(kwargs.get("limit", 50), cap)
        return await real_list(**kwargs)

    monkeypatch.setattr(audit_log, "list_audit_events", capped_list)
    monkeypatch.setattr(audit_router, "list_audit_events", capped_list)


async def _record_summary_fixture_events(wallet_id: str, other_wallet_id: str):
    await record_audit_event(
        event="mcp.invoke", wallet_id=wallet_id, ok=True, request_id="sum-1"
    )
    await record_audit_event(
        event="mcp.invoke", wallet_id=wallet_id, ok=True, request_id="sum-2"
    )
    await record_audit_event(
        event="permit.denied",
        wallet_id=wallet_id,
        ok=False,
        error="budget_exceeded",
        request_id="sum-3",
    )
    await record_audit_event(
        event="mcp.invoke",
        wallet_id=other_wallet_id,
        ok=False,
        metadata={"policy_reason": "tool_not_allowed"},
        request_id="sum-4",
    )


@pytest.mark.anyio
async def test_events_summary_counts_every_matching_event_past_the_row_cap(
    client, clean_database, monkeypatch
):
    """ok + failed must equal total even when the table outgrows any list read."""
    await _record_summary_fixture_events("wallet-sum", "wallet-sum-other")
    _cap_audit_list_reads(monkeypatch, cap=2)

    response = await client.get(
        "/v1/audit/events?wallet_id=wallet-sum&summary=true",
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["summary"] == {
        "total": 3,
        "ok": 2,
        "failed": 1,
        "by_event": {"mcp.invoke": 2, "permit.denied": 1},
    }


@pytest.mark.anyio
async def test_audit_summary_counts_every_event_and_flags_truncated_reasons(
    client, clean_database, monkeypatch
):
    """Totals stay exact past the row cap; the row-tallied bucket says it is partial."""
    await _record_summary_fixture_events("wallet-sum", "wallet-sum-other")

    uncapped = await client.get("/v1/audit/summary", headers={"X-API-Key": "test-key"})
    assert uncapped.status_code == 200
    assert uncapped.json()["by_policy_reason_truncated"] is False
    assert sum(uncapped.json()["by_policy_reason"].values()) == 4

    _cap_audit_list_reads(monkeypatch, cap=2)
    response = await client.get("/v1/audit/summary", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["by_outcome"] == {"ok": 2, "error": 2}
    assert body["by_event"] == {"mcp.invoke": 3, "permit.denied": 1}
    assert body["by_wallet"] == {"wallet-sum": 3, "wallet-sum-other": 1}
    # Policy reasons live in metadata, so that bucket is still tallied from
    # the newest rows only -- and now says so instead of silently undercounting.
    assert body["by_policy_reason_truncated"] is True
    assert sum(body["by_policy_reason"].values()) == 2


@pytest.mark.anyio
async def test_audit_summaries_apply_time_and_outcome_filters(client, clean_database):
    await record_audit_event(
        event="mcp.invoke",
        wallet_id="wallet-sum",
        ok=False,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    await _record_summary_fixture_events("wallet-sum", "wallet-sum-other")

    summary = await client.get(
        "/v1/audit/summary?created_after=2021-01-01T00:00:00Z",
        headers={"X-API-Key": "test-key"},
    )
    assert summary.status_code == 200
    assert summary.json()["total"] == 4
    assert summary.json()["by_wallet"] == {"wallet-sum": 3, "wallet-sum-other": 1}

    failed_only = await client.get(
        "/v1/audit/events?wallet_id=wallet-sum&ok=false&summary=true",
        headers={"X-API-Key": "test-key"},
    )
    assert failed_only.status_code == 200
    assert failed_only.json()["summary"] == {
        "total": 2,
        "ok": 0,
        "failed": 2,
        "by_event": {"mcp.invoke": 1, "permit.denied": 1},
    }

    invalid = await client.get(
        "/v1/audit/summary?created_after=not-a-date",
        headers={"X-API-Key": "test-key"},
    )
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_wallet_key_aggregated_summaries_stay_scoped_to_its_wallet(
    client, clean_database, monkeypatch
):
    """The SQL-aggregated counts are scoped exactly like the rows they replace."""
    sponsor_response = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Audit Summary Scope Corp",
            "email": "audit-summary-scope@test.com",
        },
        headers={"X-API-Key": "test-key"},
    )
    assert sponsor_response.status_code == 201
    wallet_id = sponsor_response.json()["wallet_id"]
    key_response = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": "audit-summary-scope-key"},
        headers={"X-API-Key": "test-key"},
    )
    assert key_response.status_code == 201
    wallet_headers = {"X-API-Key": key_response.json()["api_key"]}

    await _record_summary_fixture_events(wallet_id, "wallet-sum-other")
    await record_audit_event(event="mcp.invoke", wallet_id="wallet-sum-other", ok=True)
    own_total = (
        await client.get(
            f"/v1/audit/events?wallet_id={wallet_id}",
            headers={"X-API-Key": "test-key"},
        )
    ).json()["total"]
    assert own_total >= 3
    _cap_audit_list_reads(monkeypatch, cap=2)

    summary = await client.get("/v1/audit/summary", headers=wallet_headers)
    assert summary.status_code == 200
    assert summary.json()["total"] == own_total
    assert summary.json()["by_wallet"] == {wallet_id: own_total}

    events_summary = await client.get(
        "/v1/audit/events?summary=true", headers=wallet_headers
    )
    assert events_summary.status_code == 200
    scoped = events_summary.json()["summary"]
    assert scoped["total"] == own_total
    assert scoped["ok"] + scoped["failed"] == own_total

    other = await client.get(
        "/v1/audit/events?wallet_id=wallet-sum-other&summary=true",
        headers=wallet_headers,
    )
    assert other.status_code == 403

    unauthenticated = await client.get("/v1/audit/summary")
    assert unauthenticated.status_code in (401, 403)
