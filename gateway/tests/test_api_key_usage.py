"""SEL-346: per-key API usage events and operator reporting."""

from __future__ import annotations

from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core import api_key_usage
from app.db.database import get_session_factory
from tests.test_demo_keys import (
    client as _client,
    demo_enabled as _demo_enabled,
    _issue,
)
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


client = _client
demo_enabled = _demo_enabled
DAILY = "/v1/admin/api-keys/usage/daily"


async def _events_for(key_id: str) -> list[dict]:
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT * FROM api_key_usage_events "
                "WHERE key_id = :key_id ORDER BY created_at, id"
            ),
            {"key_id": key_id},
        )
        return [dict(row) for row in result.mappings()]


@pytest.mark.anyio
async def test_authenticated_calls_record_one_event_each_without_request_data(
    client: AsyncClient, clean_database: None
) -> None:
    wallet = await provision_agent_wallet(client)
    private = "private-person@example.test"
    response = await client.request(
        "GET",
        f"/v1/api-keys/{wallet['agent_wallet_id']}",
        params={"email": private},
        headers={**wallet["agent_headers"], "X-Private-Note": private},
        content=private,
    )
    assert response.status_code == 200, response.text
    response = await client.get("/v1/me/authority", headers=wallet["agent_headers"])
    assert response.status_code == 200, response.text

    events = await _events_for(wallet["key_id"])
    assert len(events) == 2
    assert {event["endpoint"] for event in events} == {
        "/v1/api-keys/{wallet_id}",
        "/v1/me/authority",
    }
    assert all(event["method"] == "GET" for event in events)
    assert all(event["status_code"] == 200 for event in events)
    assert all(event["tenant"] is None for event in events)
    assert all(event["latency_ms"] >= 0 for event in events)
    assert all(event["created_at"] is not None for event in events)
    assert all(
        private not in str(value) for event in events for value in event.values()
    )
    assert all(
        set(event)
        == {
            "id",
            "key_id",
            "tenant",
            "method",
            "endpoint",
            "status_code",
            "latency_ms",
            "created_at",
        }
        for event in events
    )


@pytest.mark.anyio
async def test_missing_and_invalid_credentials_create_no_events(
    client: AsyncClient, clean_database: None
) -> None:
    assert (await client.get("/v1/me/authority")).status_code == 401
    assert (
        await client.get("/v1/me/authority", headers={"X-API-Key": "not-a-real-key"})
    ).status_code == 403
    async with get_session_factory()() as session:
        count = await session.scalar(text("SELECT COUNT(*) FROM api_key_usage_events"))
    assert count == 0


@pytest.mark.anyio
async def test_governed_route_and_head_record_original_method(
    client: AsyncClient, clean_database: None
) -> None:
    wallet = await provision_agent_wallet(client)
    malformed = await client.post(
        "/mcp/messages",
        content="{private-data",
        headers=wallet["agent_headers"],
    )
    assert malformed.status_code == 400
    head = await client.head("/v1/me/authority", headers=wallet["agent_headers"])
    assert head.status_code == 200

    events = await _events_for(wallet["key_id"])
    assert {
        (event["method"], event["endpoint"], event["status_code"]) for event in events
    } == {
        ("POST", "/mcp/messages", 400),
        ("HEAD", "/v1/me/authority", 200),
    }


@pytest.mark.anyio
async def test_daily_usage_requires_bootstrap_admin_and_reports_key_calls(
    client: AsyncClient, clean_database: None, demo_enabled
) -> None:
    wallet = await provision_agent_wallet(client)
    demo = await _issue(client)
    demo_headers = {"X-API-Key": demo["api_key"]}
    assert (
        await client.get("/v1/me/authority", headers=wallet["agent_headers"])
    ).status_code == 200
    assert (
        await client.get("/v1/me/authority", headers=demo_headers)
    ).status_code == 200

    assert (await client.get(DAILY)).status_code == 401
    assert (
        await client.get(DAILY, headers={"X-API-Key": "not-a-real-key"})
    ).status_code == 403
    assert (await client.get(DAILY, headers=wallet["agent_headers"])).status_code == 403
    assert (await client.get(DAILY, headers=demo_headers)).status_code == 403

    response = await client.get(DAILY, headers=BOOTSTRAP_HEADERS)
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    wallet_rows = [row for row in rows if row["key_id"] == wallet["key_id"]]
    demo_rows = [row for row in rows if row["key_id"] == demo["key_id"]]
    filtered = await client.get(
        DAILY, params={"key_id": wallet["key_id"]}, headers=BOOTSTRAP_HEADERS
    )
    assert filtered.status_code == 200
    assert filtered.json()["rows"] == wallet_rows
    assert sum(row["calls"] for row in wallet_rows) == 2
    assert sum(row["calls_4xx"] for row in wallet_rows) == 1
    assert sum(row["calls"] for row in demo_rows) == 2
    assert sum(row["calls_4xx"] for row in demo_rows) == 1
    assert all(event["tenant"] == "demo" for event in await _events_for(demo["key_id"]))
    for row in wallet_rows + demo_rows:
        assert row["calls_5xx"] == 0
        assert row["p50_latency_ms"] >= 0
        assert row["p95_latency_ms"] >= 0
        assert row["first_call_at"] <= row["last_call_at"]
        assert (
            datetime.fromisoformat(row["first_call_at"]).date().isoformat()
            == row["day"]
        )


@pytest.mark.anyio
async def test_failed_usage_insert_does_not_fail_authenticated_request(
    client: AsyncClient, clean_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = await provision_agent_wallet(client)

    async def fail_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("synthetic usage insert failure")

    monkeypatch.setattr(api_key_usage, "record_api_key_usage_event", fail_insert)
    response = await client.get("/v1/me/authority", headers=wallet["agent_headers"])
    assert response.status_code == 200, response.text
    assert await _events_for(wallet["key_id"]) == []
