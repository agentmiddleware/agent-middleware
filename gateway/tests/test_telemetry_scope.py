"""
Tests for Pillar 14: Telemetry Scoping (Multi-Tenant Autonomous PM).
Validates scoped telemetry pipelines, anomaly detection, and auto-PR generation.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "test-key"}


@pytest.mark.anyio
async def test_create_pipeline(client):
    """Create a tenant-scoped telemetry pipeline."""
    resp = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "builder-agent-01",
            "service_name": "my-scraper-tool",
            "git_repo_url": "https://github.com/agent/scraper",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["pipeline_id"].startswith("pipe-")
    assert data["service_name"] == "my-scraper-tool"
    assert data["status"] == "active"


@pytest.mark.anyio
async def test_ingest_events(client):
    """Ingest telemetry events into a scoped pipeline."""
    create = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "ingest-test",
            "service_name": "widget-tool",
        },
        headers=HEADERS,
    )
    pipeline_id = create.json()["pipeline_id"]

    resp = await client.post(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/events",
        json={
            "events": [
                {
                    "type": "request",
                    "path": "/api/widgets",
                    "status_code": 200,
                    "latency_ms": 50,
                },
                {
                    "type": "request",
                    "path": "/api/widgets",
                    "status_code": 200,
                    "latency_ms": 60,
                },
            ],
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["events_ingested"] == 2
    assert data["total_events"] == 2


@pytest.mark.anyio
async def test_anomaly_detection_on_error_spike(client):
    """High error rate should trigger anomaly detection."""
    create = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "anomaly-test",
            "service_name": "broken-tool",
        },
        headers=HEADERS,
    )
    pipeline_id = create.json()["pipeline_id"]

    # Send batch with >30% errors
    events = [
        {"type": "request", "path": "/api/fail", "level": "error", "status_code": 500},
        {"type": "request", "path": "/api/fail", "level": "error", "status_code": 500},
        {"type": "request", "path": "/api/fail", "level": "error", "status_code": 500},
        {"type": "request", "path": "/api/ok", "level": "info", "status_code": 200},
        {"type": "request", "path": "/api/ok", "level": "info", "status_code": 200},
    ]
    resp = await client.post(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/events",
        json={
            "events": events,
        },
        headers=HEADERS,
    )
    assert resp.json()["anomalies_detected"] >= 1


@pytest.mark.anyio
async def test_get_anomalies(client):
    """Can retrieve anomalies for a pipeline."""
    create = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "get-anom-test",
            "service_name": "flaky-tool",
        },
        headers=HEADERS,
    )
    pipeline_id = create.json()["pipeline_id"]

    # Trigger anomaly
    await client.post(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/events",
        json={
            "events": [{"level": "error", "path": "/crash"} for _ in range(6)]
            + [{"level": "info", "path": "/ok"}],
        },
        headers=HEADERS,
    )

    resp = await client.get(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/anomalies", headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.anyio
async def test_auto_pr_generation(client):
    """Generate an auto-PR for a detected anomaly."""
    create = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "pr-test",
            "service_name": "buggy-tool",
            "git_repo_url": "https://github.com/agent/buggy",
        },
        headers=HEADERS,
    )
    pipeline_id = create.json()["pipeline_id"]

    # Trigger anomaly
    await client.post(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/events",
        json={
            "events": [{"level": "error", "path": "/bug"} for _ in range(6)]
            + [{"level": "info"}],
        },
        headers=HEADERS,
    )

    anomalies = await client.get(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/anomalies", headers=HEADERS
    )
    anomaly_id = anomalies.json()["anomalies"][0]["anomaly_id"]

    resp = await client.post(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/auto-pr",
        json={
            "anomaly_id": anomaly_id,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pr_id"].startswith("pr-")
    assert "Auto-Fix" in data["title"]
    assert data["target_repo"] == "https://github.com/agent/buggy"


@pytest.mark.anyio
async def test_pipeline_stats(client):
    """Pipeline stats should aggregate event data."""
    create = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "stats-test",
            "service_name": "stats-tool",
        },
        headers=HEADERS,
    )
    pipeline_id = create.json()["pipeline_id"]

    await client.post(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/events",
        json={
            "events": [
                {
                    "path": "/api/fast",
                    "latency_ms": 20,
                    "status_code": 200,
                    "level": "info",
                },
                {
                    "path": "/api/slow",
                    "latency_ms": 300,
                    "status_code": 200,
                    "level": "info",
                },
                {
                    "path": "/api/err",
                    "latency_ms": 100,
                    "status_code": 500,
                    "level": "error",
                },
            ],
        },
        headers=HEADERS,
    )

    resp = await client.get(
        f"/v1/telemetry-scope/pipelines/{pipeline_id}/stats", headers=HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_events"] == 3
    assert data["error_events"] == 1
    assert data["avg_latency_ms"] > 0


@pytest.mark.anyio
async def test_list_pipelines(client):
    """Can list pipelines filtered by tenant."""
    await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "list-test-tenant",
            "service_name": "list-tool",
        },
        headers=HEADERS,
    )

    resp = await client.get(
        "/v1/telemetry-scope/pipelines?tenant_id=list-test-tenant", headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.anyio
async def test_pipeline_not_found(client):
    resp = await client.get(
        "/v1/telemetry-scope/pipelines/pipe-nonexistent", headers=HEADERS
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_telemetry_scope_requires_api_key(client):
    resp = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": "test",
            "service_name": "test",
        },
    )
    assert resp.status_code in (401, 403)
