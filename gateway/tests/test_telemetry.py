"""
Tests for the Autonomous Product Manager / Telemetry endpoints.
Validates event ingestion, stats, and anomaly listing, and that all of it is
scoped to the ingesting wallet (tenant isolation).
"""

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from app.core.dependencies import get_autonomous_pm
from app.db.database import get_session_factory
from app.db.models import TelemetryEventModel
from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


@pytest.fixture
def sample_batch():
    return {
        "events": [
            {
                "event_type": "error",
                "source": "iot-bridge",
                "message": "MQTT connection timeout",
                "severity": "high",
                "metadata": {"device_id": "sensor-042"},
            },
            {
                "event_type": "api_call",
                "source": "media-engine",
                "message": "POST /v1/media/videos 200 OK",
                "severity": "info",
                "metadata": {"latency_ms": 142},
            },
            {
                "event_type": "llm_trace",
                "source": "auto-pr",
                "message": "Fix generation completed",
                "severity": "info",
                "metadata": {"model": "claude-3", "tokens": 1500},
            },
        ]
    }


# --- Event Ingestion ---


@pytest.mark.anyio
async def test_batch_ingest(client, api_headers, sample_batch):
    resp = await client.post(
        "/v1/telemetry/events", json=sample_batch, headers=api_headers
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["ingested"] == 3
    assert data["failed"] == 0
    assert "batch_id" in data


@pytest.mark.anyio
async def test_single_event_ingest(client, api_headers):
    event = {
        "event_type": "warning",
        "source": "auth-service",
        "message": "Rate limit approaching threshold",
        "severity": "medium",
    }
    resp = await client.post(
        "/v1/telemetry/events/single", json=event, headers=api_headers
    )
    assert resp.status_code == 202
    assert resp.json()["ingested"] == 1


@pytest.mark.anyio
async def test_invalid_event_type(client, api_headers):
    event = {
        "event_type": "invalid_type",
        "source": "test",
        "message": "test",
    }
    resp = await client.post(
        "/v1/telemetry/events/single", json=event, headers=api_headers
    )
    assert resp.status_code == 422  # Validation error


# --- Stats ---


@pytest.mark.anyio
async def test_stats_endpoint(client, api_headers, sample_batch):
    await client.post("/v1/telemetry/events", json=sample_batch, headers=api_headers)
    resp = await client.get("/v1/telemetry/stats", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "total_events" in data
    assert "events_by_type" in data


# --- Anomalies ---


@pytest.mark.anyio
async def test_list_anomalies_empty(client, api_headers):
    resp = await client.get("/v1/telemetry/anomalies", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 0
    assert isinstance(data["anomalies"], list)


@pytest.mark.anyio
async def test_anomaly_not_found(client, api_headers):
    resp = await client.get("/v1/telemetry/anomalies/nonexistent", headers=api_headers)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_auto_pr_anomaly_not_found(client, api_headers):
    resp = await client.post(
        "/v1/telemetry/anomalies/nonexistent/auto-pr",
        json={"anomaly_id": "nonexistent", "dry_run": True},
        headers=api_headers,
    )
    assert resp.status_code == 404


# --- Tenant isolation (tenant = ingesting wallet) ---
#
# Telemetry events, the anomalies derived from them, the auto-PR context and
# the stats rollup are scoped to the wallet that ingested the events. Wallet B
# must never read, enumerate, or act on wallet A's telemetry; a foreign anomaly
# is indistinguishable from a missing one (404). Bootstrap admins see every
# tenant.


async def _reset_telemetry(pm) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(TelemetryEventModel))
        await session.commit()
    await pm.detector._hydrate_if_needed()
    pm.detector._anomalies.clear()


@pytest.fixture
async def fresh_telemetry():
    """Empty event store and anomaly registry so detection sees only this test."""
    pm = get_autonomous_pm()
    await _reset_telemetry(pm)
    yield pm
    await _reset_telemetry(pm)


def _error_batch(source: str, count: int, message: str) -> dict:
    return {
        "events": [
            {
                "event_type": "error",
                "source": source,
                "message": f"{message} #{i}",
                "severity": "high",
                "stack_trace": f"Traceback: {source} internal frame {i}",
            }
            for i in range(count)
        ]
    }


async def _seed_two_tenants(client, pm):
    """A reports a concentrated error burst; B reports a couple of its own
    errors. Returns (a, b, anomaly_id) where the anomaly belongs to A."""
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    resp = await client.post(
        "/v1/telemetry/events",
        json=_error_batch("a-private-svc", 12, "A secret failure"),
        headers=a["agent_headers"],
    )
    assert resp.status_code == 202
    resp = await client.post(
        "/v1/telemetry/events",
        json=_error_batch("b-private-svc", 2, "B secret failure"),
        headers=b["agent_headers"],
    )
    assert resp.status_code == 202

    detected = await pm.detector.analyze()
    assert [r.category for r in detected] == ["source_concentration"]
    return a, b, detected[0].anomaly_id


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_routes_require_auth(client):
    for method, path, body in (
        ("POST", "/v1/telemetry/events", {"events": []}),
        ("POST", "/v1/telemetry/events/single", {}),
        ("GET", "/v1/telemetry/anomalies", None),
        ("GET", "/v1/telemetry/anomalies/anom-x", None),
        ("POST", "/v1/telemetry/anomalies/anom-x/auto-pr", {}),
        ("GET", "/v1/telemetry/stats", None),
    ):
        resp = await client.request(method, path, json=body)
        assert resp.status_code == 401, (method, path, resp.status_code)


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_anomalies_not_visible_across_tenants(
    client, clean_database, fresh_telemetry
):
    a, b, anomaly_id = await _seed_two_tenants(client, fresh_telemetry)

    # B cannot enumerate A's anomaly.
    listed = await client.get("/v1/telemetry/anomalies", headers=b["agent_headers"])
    assert listed.status_code == 200
    assert listed.json()["total"] == 0
    assert listed.json()["anomalies"] == []
    assert "a-private-svc" not in listed.text

    # B cannot read it or trigger an auto-PR on it; a foreign anomaly answers
    # exactly like a missing one and echoes nothing of A's.
    missing = await client.get(
        "/v1/telemetry/anomalies/anom-missing", headers=b["agent_headers"]
    )
    foreign = await client.get(
        f"/v1/telemetry/anomalies/{anomaly_id}", headers=b["agent_headers"]
    )
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["error"] == missing.json()["detail"]["error"]
    assert "a-private-svc" not in foreign.text
    assert a["agent_wallet_id"] not in foreign.text

    pr = await client.post(
        f"/v1/telemetry/anomalies/{anomaly_id}/auto-pr",
        json={"anomaly_id": anomaly_id, "dry_run": True},
        headers=b["agent_headers"],
    )
    assert pr.status_code == 404
    assert "a-private-svc" not in pr.text
    assert a["agent_wallet_id"] not in pr.text

    # B's stats do not count A's anomaly.
    b_stats = await client.get("/v1/telemetry/stats", headers=b["agent_headers"])
    assert b_stats.status_code == 200
    assert b_stats.json()["total_anomalies"] == 0

    # The owner still sees, reads, and acts on its anomaly, and the anomaly
    # was derived from A's events alone (B's two errors did not dilute it).
    own = await client.get("/v1/telemetry/anomalies", headers=a["agent_headers"])
    assert own.status_code == 200
    assert own.json()["total"] == 1
    [own_anomaly] = own.json()["anomalies"]
    assert own_anomaly["anomaly_id"] == anomaly_id
    assert own_anomaly["event_count"] == 12
    assert own_anomaly["summary"].startswith("12/12 errors")
    assert (
        await client.get(
            f"/v1/telemetry/anomalies/{anomaly_id}", headers=a["agent_headers"]
        )
    ).status_code == 200
    own_pr = await client.post(
        f"/v1/telemetry/anomalies/{anomaly_id}/auto-pr",
        json={"anomaly_id": anomaly_id, "dry_run": True},
        headers=a["agent_headers"],
    )
    assert own_pr.status_code == 200
    assert own_pr.json()["anomaly_id"] == anomaly_id
    a_stats = await client.get("/v1/telemetry/stats", headers=a["agent_headers"])
    assert a_stats.json()["total_anomalies"] == 1

    # Bootstrap admins see every tenant.
    admin_list = await client.get("/v1/telemetry/anomalies", headers=BOOTSTRAP_HEADERS)
    assert admin_list.status_code == 200
    assert anomaly_id in [x["anomaly_id"] for x in admin_list.json()["anomalies"]]
    assert (
        await client.get(
            f"/v1/telemetry/anomalies/{anomaly_id}", headers=BOOTSTRAP_HEADERS
        )
    ).status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_auto_pr_context_excludes_other_tenants_events(
    client, clean_database, fresh_telemetry, monkeypatch
):
    pm = fresh_telemetry
    a, _b, anomaly_id = await _seed_two_tenants(client, pm)

    captured = []
    original = pm.pr_generator.generate_fix

    async def spy(*, anomaly, related_events, dry_run=True):
        captured.append(related_events)
        return await original(
            anomaly=anomaly, related_events=related_events, dry_run=dry_run
        )

    monkeypatch.setattr(pm.pr_generator, "generate_fix", spy)

    # Whether the owner or a bootstrap admin triggers it, the fix context is
    # built only from the anomaly owner's events, never another tenant's.
    for headers in (a["agent_headers"], BOOTSTRAP_HEADERS):
        resp = await client.post(
            f"/v1/telemetry/anomalies/{anomaly_id}/auto-pr",
            json={"anomaly_id": anomaly_id, "dry_run": True},
            headers=headers,
        )
        assert resp.status_code == 200
        assert "b-private-svc" not in resp.text

    assert len(captured) == 2
    for related in captured:
        assert related
        assert {se.event.source for se in related} == {"a-private-svc"}
        assert not any("B secret" in se.event.message for se in related)


@pytest.mark.proof
@pytest.mark.anyio
async def test_auto_pr_never_reports_a_fabricated_pr(
    client, clean_database, fresh_telemetry, monkeypatch
):
    """The generator pushes no branch and runs no tests, so it must not say so.

    With a git remote configured, dry_run=false used to answer
    status="pr_created" with a made-up ``<remote>/pull/auto-xxxxxx`` URL and
    tests_passed=true for a placeholder diff nobody tested.
    """
    pm = fresh_telemetry
    a, _b, anomaly_id = await _seed_two_tenants(client, pm)
    monkeypatch.setattr(
        pm.pr_generator, "git_remote", "https://github.com/example/repo"
    )

    for headers in (a["agent_headers"], BOOTSTRAP_HEADERS):
        resp = await client.post(
            f"/v1/telemetry/anomalies/{anomaly_id}/auto-pr",
            json={"anomaly_id": anomaly_id, "dry_run": False},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["anomaly_id"] == anomaly_id
        assert body["pr_url"] is None
        assert body["status"] == "simulated"
        assert body["tests_passed"] is None
        assert "/pull/" not in resp.text

    preview = await client.post(
        f"/v1/telemetry/anomalies/{anomaly_id}/auto-pr",
        json={"anomaly_id": anomaly_id, "dry_run": True},
        headers=a["agent_headers"],
    )
    assert preview.status_code == 200
    assert preview.json()["pr_url"] is None
    assert preview.json()["status"] == "dry_run"
    assert preview.json()["tests_passed"] is None
    assert preview.json()["diff"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_ingest_and_stats_scoped_to_caller(
    client, clean_database, fresh_telemetry
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    resp = await client.post(
        "/v1/telemetry/events",
        json={
            "events": [
                {"event_type": "error", "source": "a-private-svc", "message": "x"},
                {"event_type": "error", "source": "a-private-svc", "message": "y"},
                {"event_type": "api_call", "source": "a-private-api", "message": "z"},
            ]
        },
        headers=a["agent_headers"],
    )
    assert resp.status_code == 202
    resp = await client.post(
        "/v1/telemetry/events/single",
        json={"event_type": "warning", "source": "b-private-svc", "message": "w"},
        headers=b["agent_headers"],
    )
    assert resp.status_code == 202
    resp = await client.post(
        "/v1/telemetry/events/single",
        json={"event_type": "custom", "source": "ops-internal", "message": "o"},
        headers=BOOTSTRAP_HEADERS,
    )
    assert resp.status_code == 202

    a_stats = await client.get("/v1/telemetry/stats", headers=a["agent_headers"])
    assert a_stats.status_code == 200
    assert a_stats.json()["total_events"] == 3
    assert a_stats.json()["events_by_source"] == {
        "a-private-svc": 2,
        "a-private-api": 1,
    }
    assert a_stats.json()["events_by_type"] == {"error": 2, "api_call": 1}

    b_stats = await client.get("/v1/telemetry/stats", headers=b["agent_headers"])
    assert b_stats.status_code == 200
    assert b_stats.json()["total_events"] == 1
    assert b_stats.json()["events_by_source"] == {"b-private-svc": 1}
    assert "a-private" not in b_stats.text
    assert "ops-internal" not in b_stats.text

    # Bootstrap admin events stay out of wallet views; admins see every tenant.
    admin_stats = await client.get("/v1/telemetry/stats", headers=BOOTSTRAP_HEADERS)
    assert admin_stats.status_code == 200
    assert admin_stats.json()["total_events"] == 5
    assert set(admin_stats.json()["events_by_source"]) == {
        "a-private-svc",
        "a-private-api",
        "b-private-svc",
        "ops-internal",
    }
