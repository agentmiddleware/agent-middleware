"""
Tests for Pillar 12: Red-Team-as-a-Service.
Validates multi-tenant external security scanning.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from tests.test_trust_helpers import provision_agent_wallet

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "test-key"}


@pytest.mark.anyio
async def test_create_rtaas_job(client):
    """Create an RTaaS scanning job against external endpoints."""
    resp = await client.post(
        "/v1/rtaas/jobs",
        json={
            "tenant_id": "agent-builder-01",
            "targets": [
                {"url": "https://api.external-tool.com/v1/users", "method": "GET"},
                {"url": "https://api.external-tool.com/v1/users", "method": "POST"},
            ],
            "intensity": "standard",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "completed"
    assert data["targets_count"] == 2
    assert data["total_tests_run"] > 0
    assert 0 <= data["security_score"] <= 100


@pytest.mark.anyio
async def test_rtaas_returns_vulnerabilities(client):
    """RTaaS jobs should return structured vulnerability reports."""
    resp = await client.post(
        "/v1/rtaas/jobs",
        json={
            "tenant_id": "vuln-test",
            "targets": [
                {"url": "https://api.test-tool.io/auth", "method": "POST"},
                {"url": "https://api.test-tool.io/data", "method": "GET"},
                {"url": "https://api.test-tool.io/admin", "method": "DELETE"},
            ],
            "intensity": "thorough",
        },
        headers=HEADERS,
    )
    data = resp.json()

    for vuln in data["vulnerabilities"]:
        assert "vuln_id" in vuln
        assert "severity" in vuln
        assert "category" in vuln
        assert "remediation" in vuln
        assert vuln["cwe_id"].startswith("CWE-")


@pytest.mark.anyio
async def test_rtaas_list_jobs(client):
    """Can list RTaaS jobs filtered by tenant."""
    await client.post(
        "/v1/rtaas/jobs",
        json={
            "tenant_id": "list-test-tenant",
            "targets": [{"url": "https://example.com/api"}],
        },
        headers=HEADERS,
    )

    resp = await client.get(
        "/v1/rtaas/jobs?tenant_id=list-test-tenant", headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


@pytest.mark.anyio
async def test_rtaas_get_job_by_id(client):
    """Retrieve a specific job by ID."""
    create = await client.post(
        "/v1/rtaas/jobs",
        json={
            "tenant_id": "id-test",
            "targets": [{"url": "https://example.com/api"}],
        },
        headers=HEADERS,
    )
    job_id = create.json()["job_id"]

    resp = await client.get(f"/v1/rtaas/jobs/{job_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["job_id"] == job_id


@pytest.mark.anyio
async def test_rtaas_get_vulnerabilities_endpoint(client):
    """Dedicated vulnerabilities endpoint works."""
    create = await client.post(
        "/v1/rtaas/jobs",
        json={
            "tenant_id": "vuln-endpoint-test",
            "targets": [{"url": "https://api.vulnerable.io/login"}],
        },
        headers=HEADERS,
    )
    job_id = create.json()["job_id"]

    resp = await client.get(f"/v1/rtaas/jobs/{job_id}/vulnerabilities", headers=HEADERS)
    assert resp.status_code == 200
    assert "vulnerabilities" in resp.json()


@pytest.mark.anyio
async def test_rtaas_job_not_found(client):
    resp = await client.get("/v1/rtaas/jobs/rtaas-nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_rtaas_requires_api_key(client):
    resp = await client.post(
        "/v1/rtaas/jobs",
        json={
            "tenant_id": "test",
            "targets": [{"url": "https://example.com"}],
        },
    )
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------
# Tenant isolation: jobs are wallet-scoped (tenant_id is the owning wallet)
# --------------------------------------------------------------------------


def _job_body(tenant_id: str, **target_extra) -> dict:
    return {
        "tenant_id": tenant_id,
        "targets": [
            {"url": "https://api.tenant-a-private.example/v1/users", **target_extra}
        ],
        "intensity": "quick",
    }


@pytest.mark.proof
@pytest.mark.anyio
async def test_rtaas_job_not_readable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    create = await client.post(
        "/v1/rtaas/jobs",
        json=_job_body(a["agent_wallet_id"]),
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    missing = await client.get(
        "/v1/rtaas/jobs/rtaas-doesnotexist", headers=b["agent_headers"]
    )
    assert missing.status_code == 404

    for path in (
        f"/v1/rtaas/jobs/{job_id}",
        f"/v1/rtaas/jobs/{job_id}/vulnerabilities",
    ):
        resp = await client.get(path, headers=b["agent_headers"])
        # Same answer as a job that does not exist: no existence oracle.
        assert resp.status_code == 404, path
        assert resp.json() == missing.json()
        assert a["agent_wallet_id"] not in resp.text
        assert "tenant-a-private" not in resp.text

    # Owner keeps access to its own job and report.
    own = await client.get(f"/v1/rtaas/jobs/{job_id}", headers=a["agent_headers"])
    assert own.status_code == 200
    assert own.json()["tenant_id"] == a["agent_wallet_id"]
    own_vulns = await client.get(
        f"/v1/rtaas/jobs/{job_id}/vulnerabilities", headers=a["agent_headers"]
    )
    assert own_vulns.status_code == 200

    # Bootstrap admin keeps access.
    admin = await client.get(f"/v1/rtaas/jobs/{job_id}", headers=HEADERS)
    assert admin.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_rtaas_list_jobs_scoped_to_caller(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    create = await client.post(
        "/v1/rtaas/jobs",
        json=_job_body(a["agent_wallet_id"]),
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    # B lists with no filter, and while spoofing A's tenant_id, and sees
    # nothing of A's.
    for path in (
        "/v1/rtaas/jobs",
        f"/v1/rtaas/jobs?tenant_id={a['agent_wallet_id']}",
    ):
        resp = await client.get(path, headers=b["agent_headers"])
        assert resp.status_code == 200, path
        assert resp.json()["total"] == 0
        assert job_id not in resp.text
        assert a["agent_wallet_id"] not in resp.text

    # A sees only its own jobs, even with no filter.
    own = await client.get("/v1/rtaas/jobs", headers=a["agent_headers"])
    assert own.status_code == 200
    own_jobs = own.json()["jobs"]
    assert job_id in {j["job_id"] for j in own_jobs}
    assert {j["tenant_id"] for j in own_jobs} == {a["agent_wallet_id"]}

    # Bootstrap admin may still filter by any tenant.
    admin = await client.get(
        f"/v1/rtaas/jobs?tenant_id={a['agent_wallet_id']}", headers=HEADERS
    )
    assert admin.status_code == 200
    assert job_id in {j["job_id"] for j in admin.json()["jobs"]}


@pytest.mark.proof
@pytest.mark.anyio
async def test_rtaas_create_job_for_other_wallet_denied(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    resp = await client.post(
        "/v1/rtaas/jobs",
        json=_job_body(a["agent_wallet_id"]),
        headers=b["agent_headers"],
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "wallet_access_denied"

    # Nothing was recorded under A's tenant.
    listed = await client.get(
        f"/v1/rtaas/jobs?tenant_id={a['agent_wallet_id']}", headers=HEADERS
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 0


@pytest.mark.proof
@pytest.mark.anyio
async def test_rtaas_reads_require_api_key(client):
    for path in (
        "/v1/rtaas/jobs",
        "/v1/rtaas/jobs/rtaas-anything",
        "/v1/rtaas/jobs/rtaas-anything/vulnerabilities",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401, path


# --------------------------------------------------------------------------
# Target credentials are never forwarded, so they must never be stored
# --------------------------------------------------------------------------


@pytest.mark.proof
@pytest.mark.anyio
async def test_rtaas_target_auth_header_is_not_persisted(client, clean_database):
    from app.db.database import get_session_factory
    from app.db.models import SecurityScanModel

    secret = "Bearer rtaas-target-credential-do-not-store"
    a = await provision_agent_wallet(client)
    create = await client.post(
        "/v1/rtaas/jobs",
        json=_job_body(a["agent_wallet_id"], auth_header=secret),
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    job_id = create.json()["job_id"]
    assert "rtaas-target-credential" not in create.text

    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(SecurityScanModel, job_id)
    assert row is not None
    stored = row.targets_json or ""
    assert "rtaas-target-credential" not in stored
    assert "auth_header" not in stored
    # The rest of the target is still recorded.
    assert "tenant-a-private" in stored

    got = await client.get(f"/v1/rtaas/jobs/{job_id}", headers=a["agent_headers"])
    assert got.status_code == 200
    assert "rtaas-target-credential" not in got.text


# --------------------------------------------------------------------------
# The simulation is deterministic across processes (no salted str hash())
# --------------------------------------------------------------------------


_SIMULATE_SCRIPT = """
import json
from app.schemas.red_team import AttackCategory
from app.services.rtaas import RTaaSEngine, RTaaSTarget

targets = [RTaaSTarget(url=f"https://api.example-{i}.com/v1") for i in range(12)]
vulns, tests_run, score = RTaaSEngine()._simulate_external_scan(
    targets, list(AttackCategory), "standard"
)
print(json.dumps({
    "findings": sorted(
        [v.target_url, v.category.value, v.severity.value] for v in vulns
    ),
    "tests_run": tests_run,
    "score": score,
}))
"""


def _simulate_in_subprocess(hash_seed: str) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=hash_seed)
    proc = subprocess.run(
        [sys.executable, "-c", _SIMULATE_SCRIPT],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.proof
def test_rtaas_simulation_is_stable_across_hash_seeds():
    first = _simulate_in_subprocess("1")
    second = _simulate_in_subprocess("2")
    assert first["findings"], "fixture URLs should yield at least one finding"
    assert first == second
