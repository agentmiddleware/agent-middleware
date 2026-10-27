"""SEL-346: per-key API usage events and operator reporting."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from sqlalchemy.exc import OperationalError

from app.core import api_key_usage
from app.db import api_key_usage_view
from app.routers import api_keys as api_keys_router
from app.core.time import utc_now
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


async def _set_event_times(key_id: str, times: list[datetime]) -> None:
    events = await _events_for(key_id)
    assert len(events) == len(times)
    async with get_session_factory()() as session:
        for event, created_at in zip(events, times, strict=True):
            await session.execute(
                text(
                    "UPDATE api_key_usage_events SET created_at = :created_at "
                    "WHERE id = :id"
                ),
                {"id": event["id"], "created_at": created_at},
            )
        await session.commit()


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
async def test_daily_usage_window_limit_and_invalid_bounds(
    client: AsyncClient, clean_database: None
) -> None:
    wallet = await provision_agent_wallet(client)
    for _ in range(4):
        response = await client.get("/v1/me/authority", headers=wallet["agent_headers"])
        assert response.status_code == 200

    today = utc_now().date()
    await _set_event_times(
        wallet["key_id"],
        [
            datetime.combine(today, time.min),
            datetime.combine(today - timedelta(days=1), time.min),
            datetime.combine(today - timedelta(days=2), time.min),
            datetime.combine(today - timedelta(days=90), time.min),
        ],
    )
    response = await client.get(
        DAILY, params={"key_id": wallet["key_id"]}, headers=BOOTSTRAP_HEADERS
    )
    assert response.status_code == 200, response.text
    assert [row["day"] for row in response.json()["rows"]] == [
        (today - timedelta(days=offset)).isoformat() for offset in range(3)
    ]

    response = await client.get(
        DAILY,
        params={"key_id": wallet["key_id"], "days": 2},
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert [row["day"] for row in response.json()["rows"]] == [
        today.isoformat(),
        (today - timedelta(days=1)).isoformat(),
    ]

    response = await client.get(
        DAILY,
        params={"key_id": wallet["key_id"], "days": 90, "limit": 1},
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert [row["day"] for row in response.json()["rows"]] == [today.isoformat()]

    for params in ({"days": 0}, {"days": 91}, {"limit": 0}, {"limit": 201}):
        response = await client.get(DAILY, params=params, headers=BOOTSTRAP_HEADERS)
        assert response.status_code == 422, (params, response.text)


@pytest.mark.anyio
@pytest.mark.parametrize("retention_days", [None, 0])
async def test_prune_usage_disabled_never_opens_session(
    monkeypatch: pytest.MonkeyPatch, retention_days: int | None
) -> None:
    monkeypatch.setattr(
        api_key_usage,
        "get_settings",
        lambda: SimpleNamespace(API_KEY_USAGE_RETENTION_DAYS=retention_days),
    )

    def fail_session_factory():
        raise AssertionError("disabled pruning must not open a database session")

    monkeypatch.setattr(api_key_usage, "get_session_factory", fail_session_factory)
    assert await api_key_usage.prune_api_key_usage_events() == 0


@pytest.mark.anyio
async def test_prune_usage_cutoff_and_batch_cap(
    client: AsyncClient, clean_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = await provision_agent_wallet(client)
    for _ in range(4):
        response = await client.get("/v1/me/authority", headers=wallet["agent_headers"])
        assert response.status_code == 200

    now = utc_now().replace(microsecond=0)
    cutoff = now - timedelta(days=2)
    await _set_event_times(
        wallet["key_id"],
        [
            cutoff - timedelta(seconds=2),
            cutoff - timedelta(seconds=1),
            cutoff,
            cutoff + timedelta(seconds=1),
        ],
    )
    monkeypatch.setattr(
        api_key_usage,
        "get_settings",
        lambda: SimpleNamespace(API_KEY_USAGE_RETENTION_DAYS=2),
    )
    with pytest.raises(ValueError, match="batch_size"):
        await api_key_usage.prune_api_key_usage_events(batch_size=1_001, now=now)
    assert await api_key_usage.prune_api_key_usage_events(batch_size=1, now=now) == 1
    assert await api_key_usage.prune_api_key_usage_events(batch_size=1, now=now) == 1
    assert await api_key_usage.prune_api_key_usage_events(batch_size=1, now=now) == 0
    assert [event["created_at"] for event in await _events_for(wallet["key_id"])] == [
        cutoff.isoformat(sep=" "),
        (cutoff + timedelta(seconds=1)).isoformat(sep=" "),
    ]


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


@pytest.mark.anyio
async def test_slow_usage_insert_is_cancelled_without_failing_request(
    client: AsyncClient, clean_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = await provision_agent_wallet(client)
    cancelled = asyncio.Event()

    async def stalled_insert(*_args, **_kwargs) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(api_key_usage, "record_api_key_usage_event", stalled_insert)
    monkeypatch.setattr(api_key_usage, "_USAGE_WRITE_TIMEOUT_SECONDS", 0.01)
    response = await client.get("/v1/me/authority", headers=wallet["agent_headers"])
    assert response.status_code == 200, response.text
    assert cancelled.is_set()
    assert await _events_for(wallet["key_id"]) == []


@pytest.mark.anyio
async def test_daily_usage_long_window_requires_key_id(
    client: AsyncClient, clean_database: None
) -> None:
    wallet = await provision_agent_wallet(client)
    assert (
        await client.get("/v1/me/authority", headers=wallet["agent_headers"])
    ).status_code == 200
    assert api_key_usage_view.UNFILTERED_MAX_DAYS == 7
    limit = 7

    for params in ({"days": 8}, {"days": 30}, {"days": 90}, {"days": 8, "key_id": ""}):
        days = params
        response = await client.get(DAILY, params=params, headers=BOOTSTRAP_HEADERS)
        assert response.status_code == 422, (days, response.text)
        assert response.json()["detail"]["error"] == "key_id_required_for_long_window"

    response = await client.get(
        DAILY,
        params={"days": 90, "key_id": wallet["key_id"]},
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert sum(row["calls"] for row in response.json()["rows"]) == 1

    for params in ({}, {"days": limit}, {"days": 1}):
        response = await client.get(DAILY, params=params, headers=BOOTSTRAP_HEADERS)
        assert response.status_code == 200, (params, response.text)
        rows = [r for r in response.json()["rows"] if r["key_id"] == wallet["key_id"]]
        assert sum(row["calls"] for row in rows) == 1


class _QueryCanceled(Exception):
    sqlstate = "57014"


class _FakeSession:
    def __init__(self, dialect: str, error: Exception | None) -> None:
        self.dialect = dialect
        self.error = error
        self.statements: list[str] = []
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(str(statement))
        if self.error is not None and "SET LOCAL" not in str(statement):
            raise self.error
        return SimpleNamespace(mappings=lambda: [])

    async def rollback(self) -> None:
        self.rolled_back = True


def _use_fake_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(api_keys_router, "get_session_factory", lambda: lambda: session)


@pytest.mark.anyio
async def test_daily_usage_sets_local_statement_timeout_on_postgres(
    client: AsyncClient, clean_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _FakeSession("postgresql", None)
    _use_fake_session(monkeypatch, session)
    response = await client.get(DAILY, headers=BOOTSTRAP_HEADERS)
    assert response.status_code == 200, response.text
    assert session.statements[0] == api_key_usage_view.STATEMENT_TIMEOUT_SQL
    assert session.statements[0].startswith("SET LOCAL statement_timeout")
    assert len(session.statements) == 2


@pytest.mark.anyio
async def test_daily_usage_statement_timeout_returns_clear_503(
    client: AsyncClient, clean_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = OperationalError(
        "SELECT", {}, _QueryCanceled("canceling statement due to statement timeout")
    )
    session = _FakeSession("postgresql", error)
    _use_fake_session(monkeypatch, session)
    response = await client.get(DAILY, headers=BOOTSTRAP_HEADERS)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "usage_report_timeout"
    assert "Traceback" not in response.text
    assert session.rolled_back


@pytest.mark.anyio
async def test_daily_usage_other_db_errors_are_not_reported_as_timeouts() -> None:
    other = OperationalError("SELECT", {}, Exception("disk full"))
    assert not api_key_usage_view.is_statement_timeout(other)
    timeout = OperationalError(
        "SELECT", {}, _QueryCanceled("canceling statement due to statement timeout")
    )
    assert api_key_usage_view.is_statement_timeout(timeout)
    manual = OperationalError(
        "SELECT", {}, _QueryCanceled("canceling statement due to user request")
    )
    assert not api_key_usage_view.is_statement_timeout(manual)


@pytest.mark.anyio
async def test_daily_usage_default_window_is_seven_days(
    client: AsyncClient, clean_database: None
) -> None:
    wallet = await provision_agent_wallet(client)
    for _ in range(2):
        response = await client.get("/v1/me/authority", headers=wallet["agent_headers"])
        assert response.status_code == 200
    today = utc_now().date()
    await _set_event_times(
        wallet["key_id"],
        [
            datetime.combine(today - timedelta(days=6), time.min),
            datetime.combine(today - timedelta(days=7), time.min),
        ],
    )
    response = await client.get(DAILY, headers=BOOTSTRAP_HEADERS)
    assert response.status_code == 200, response.text
    days = [
        r["day"] for r in response.json()["rows"] if r["key_id"] == wallet["key_id"]
    ]
    assert days == [(today - timedelta(days=6)).isoformat()]
