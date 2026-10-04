"""
Tests for Oracle Mass-Broadcast API.
Validates multi-directory broadcasting and discovery metrics.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "test-key"}


# --- Broadcast ---


@pytest.mark.anyio
async def test_broadcast_api(client):
    """Broadcast a service to all directories."""
    resp = await client.post(
        "/v1/broadcast",
        json={
            "service_name": "test-widget-api",
            "base_url": "https://api.test.com",
            "generation_id": "gen-abc123",
            "llm_txt": "# Test Widget API\n> Endpoints...",
            "openapi_spec": {"openapi": "3.1.0", "info": {"title": "Test"}},
            "agent_json": {"name": "test-widget-api", "capabilities": []},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["job_id"].startswith("bcast-")
    assert data["service_name"] == "test-widget-api"
    assert data["directories_contacted"] == 6  # All 6 directories
    assert data["directories_confirmed"] >= 1
    assert data["status"] in ("complete", "partial")
    assert "discovery_metrics" in data


@pytest.mark.anyio
async def test_broadcast_with_specific_directories(client):
    """Broadcast to specific directories only."""
    resp = await client.post(
        "/v1/broadcast",
        json={
            "service_name": "targeted-api",
            "base_url": "https://api.targeted.com",
            "generation_id": "gen-targeted",
            "agent_json": {"name": "targeted-api"},
            "target_directories": ["agent-protocol-registry", "anthropic-mcp-registry"],
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["directories_contacted"] == 2


@pytest.mark.anyio
async def test_list_broadcast_jobs(client):
    """List all broadcast jobs."""
    # Create a job first
    await client.post(
        "/v1/broadcast",
        json={
            "service_name": "list-test-api",
            "base_url": "https://api.list.com",
            "generation_id": "gen-list",
        },
        headers=HEADERS,
    )
    resp = await client.get("/v1/broadcast/jobs", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


@pytest.mark.anyio
async def test_get_broadcast_job(client):
    """Get specific broadcast job details."""
    create_resp = await client.post(
        "/v1/broadcast",
        json={
            "service_name": "detail-api",
            "base_url": "https://api.detail.com",
            "generation_id": "gen-detail",
            "llm_txt": "# Detail API",
        },
        headers=HEADERS,
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/v1/broadcast/jobs/{job_id}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert len(data["targets"]) > 0


@pytest.mark.anyio
async def test_get_discovery_metrics(client):
    """Get discovery metrics for a broadcast job."""
    create_resp = await client.post(
        "/v1/broadcast",
        json={
            "service_name": "metrics-api",
            "base_url": "https://api.metrics.com",
            "generation_id": "gen-metrics",
            "openapi_spec": {"openapi": "3.1.0"},
        },
        headers=HEADERS,
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/v1/broadcast/jobs/{job_id}/metrics", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "impressions" in data
    assert "lookups" in data
    assert "integrations" in data
    assert "referral_sources" in data


@pytest.mark.anyio
async def test_simulate_discovery_event(client):
    """Simulate discovery events and verify metrics update."""
    create_resp = await client.post(
        "/v1/broadcast",
        json={
            "service_name": "events-api",
            "base_url": "https://api.events.com",
            "generation_id": "gen-events",
            "llm_txt": "# Events API",
        },
        headers=HEADERS,
    )
    job_id = create_resp.json()["job_id"]

    # Get baseline
    baseline = await client.get(f"/v1/broadcast/jobs/{job_id}/metrics", headers=HEADERS)
    base_impressions = baseline.json()["impressions"]

    # Simulate an impression
    resp = await client.post(
        f"/v1/broadcast/jobs/{job_id}/events",
        json={"event_type": "impression", "source": "langchain-hub"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["impressions"] == base_impressions + 1
    assert "langchain-hub" in data["referral_sources"]


@pytest.mark.anyio
async def test_list_directories(client):
    """List available broadcast directories."""
    resp = await client.get("/v1/broadcast/directories", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 6
    assert any(d["id"] == "anthropic-mcp-registry" for d in data["directories"])


@pytest.mark.anyio
async def test_broadcast_job_not_found(client):
    """404 for non-existent broadcast job."""
    resp = await client.get("/v1/broadcast/jobs/bcast-nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_invalid_event_type(client):
    """Reject invalid discovery event types."""
    create_resp = await client.post(
        "/v1/broadcast",
        json={
            "service_name": "invalid-api",
            "base_url": "https://api.invalid.com",
            "generation_id": "gen-invalid",
        },
        headers=HEADERS,
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.post(
        f"/v1/broadcast/jobs/{job_id}/events",
        json={"event_type": "invalid_type", "source": "test"},
        headers=HEADERS,
    )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_broadcast_requires_api_key(client):
    """Broadcast requires authentication."""
    resp = await client.post("/v1/broadcast", json={})
    assert resp.status_code in (401, 403)


# --- Tenant isolation ---
#
# Broadcast jobs belong to the wallet whose key created them. Another wallet's
# key must not be able to read, enumerate, or inject discovery events into a
# job it does not own, and a foreign job must be indistinguishable from a
# missing one (no existence oracle, no owner wallet id in the response).


def _broadcast_body(service_name: str, base_url: str) -> dict:
    return {
        "service_name": service_name,
        "base_url": base_url,
        "generation_id": f"gen-{service_name}",
        "llm_txt": f"# {service_name}",
        "openapi_spec": {"openapi": "3.1.0", "info": {"title": service_name}},
        "agent_json": {"name": service_name, "capabilities": []},
    }


@pytest.mark.proof
@pytest.mark.anyio
async def test_broadcast_job_not_readable_or_mutable_across_tenants(
    client, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    create = await client.post(
        "/v1/broadcast",
        json=_broadcast_body("tenant-a-secret-api", "https://a-private.example"),
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    baseline = await client.get(
        f"/v1/broadcast/jobs/{job_id}/metrics", headers=a["agent_headers"]
    )
    assert baseline.status_code == 200
    base_metrics = baseline.json()

    missing_id = "bcast-doesnotexist"
    missing = await client.get(
        f"/v1/broadcast/jobs/{missing_id}", headers=b["agent_headers"]
    )
    assert missing.status_code == 404
    expected_body = missing.text.replace(missing_id, job_id)

    foreign = [
        await client.get(f"/v1/broadcast/jobs/{job_id}", headers=b["agent_headers"]),
        await client.get(
            f"/v1/broadcast/jobs/{job_id}/metrics", headers=b["agent_headers"]
        ),
        await client.post(
            f"/v1/broadcast/jobs/{job_id}/events",
            json={"event_type": "integration", "source": "tenant-b-forged"},
            headers=b["agent_headers"],
        ),
    ]
    for resp in foreign:
        # Same status and body as a job that does not exist: no existence
        # oracle, and nothing of A's job (or A's wallet id) leaks to B.
        assert resp.status_code == 404
        assert resp.text == expected_body
        assert a["agent_wallet_id"] not in resp.text
        assert "tenant-a-secret-api" not in resp.text
        assert "a-private.example" not in resp.text

    # B's forged event must not have touched A's metrics.
    after = await client.get(
        f"/v1/broadcast/jobs/{job_id}/metrics", headers=a["agent_headers"]
    )
    assert after.status_code == 200
    assert after.json() == base_metrics
    assert "tenant-b-forged" not in after.json()["referral_sources"]

    # Owner keeps full access.
    owned = await client.get(f"/v1/broadcast/jobs/{job_id}", headers=a["agent_headers"])
    assert owned.status_code == 200
    assert owned.json()["service_name"] == "tenant-a-secret-api"
    event = await client.post(
        f"/v1/broadcast/jobs/{job_id}/events",
        json={"event_type": "impression", "source": "tenant-a-source"},
        headers=a["agent_headers"],
    )
    assert event.status_code == 200
    assert event.json()["impressions"] == base_metrics["impressions"] + 1
    assert "tenant-a-source" in event.json()["referral_sources"]

    # Bootstrap admin keeps access to every tenant's job.
    admin = await client.get(f"/v1/broadcast/jobs/{job_id}", headers=HEADERS)
    assert admin.status_code == 200
    admin_metrics = await client.get(
        f"/v1/broadcast/jobs/{job_id}/metrics", headers=HEADERS
    )
    assert admin_metrics.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_broadcast_list_jobs_scoped_to_caller(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    a_job = (
        await client.post(
            "/v1/broadcast",
            json=_broadcast_body("list-tenant-a-api", "https://list-a.example"),
            headers=a["agent_headers"],
        )
    ).json()["job_id"]
    b_job = (
        await client.post(
            "/v1/broadcast",
            json=_broadcast_body("list-tenant-b-api", "https://list-b.example"),
            headers=b["agent_headers"],
        )
    ).json()["job_id"]
    admin_job = (
        await client.post(
            "/v1/broadcast",
            json=_broadcast_body("list-admin-api", "https://list-admin.example"),
            headers=HEADERS,
        )
    ).json()["job_id"]

    b_list = await client.get("/v1/broadcast/jobs", headers=b["agent_headers"])
    assert b_list.status_code == 200
    assert {j["job_id"] for j in b_list.json()["jobs"]} == {b_job}
    assert b_list.json()["total"] == 1
    assert "list-tenant-a-api" not in b_list.text
    assert "list-admin-api" not in b_list.text

    # Filtering by another tenant's service name does not reveal its jobs.
    spoof = await client.get(
        "/v1/broadcast/jobs",
        params={"service_name": "list-tenant-a-api"},
        headers=b["agent_headers"],
    )
    assert spoof.status_code == 200
    assert spoof.json() == {"jobs": [], "total": 0}

    a_list = await client.get("/v1/broadcast/jobs", headers=a["agent_headers"])
    assert a_list.status_code == 200
    assert {j["job_id"] for j in a_list.json()["jobs"]} == {a_job}

    # Bootstrap admin still sees every tenant's jobs.
    admin_list = await client.get("/v1/broadcast/jobs", headers=HEADERS)
    assert admin_list.status_code == 200
    admin_ids = {j["job_id"] for j in admin_list.json()["jobs"]}
    assert {a_job, b_job, admin_job} <= admin_ids


@pytest.mark.proof
@pytest.mark.anyio
async def test_broadcast_job_endpoints_require_auth(client):
    create = await client.post(
        "/v1/broadcast",
        json=_broadcast_body("auth-check-api", "https://auth-check.example"),
        headers=HEADERS,
    )
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    for resp in (
        await client.get("/v1/broadcast/jobs"),
        await client.get(f"/v1/broadcast/jobs/{job_id}"),
        await client.get(f"/v1/broadcast/jobs/{job_id}/metrics"),
        await client.post(
            f"/v1/broadcast/jobs/{job_id}/events",
            json={"event_type": "impression", "source": "anon"},
        ),
        await client.get("/v1/broadcast/directories"),
    ):
        assert resp.status_code == 401
        assert "auth-check-api" not in resp.text
