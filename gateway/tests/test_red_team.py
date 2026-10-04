"""
Tests for the Red Team Security Swarm endpoints.
Validates scan initiation, report retrieval, vulnerability filtering,
and the quick-scan CI/CD gate.
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


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


# --- Scan Initiation ---


@pytest.mark.anyio
async def test_launch_full_scan(client, api_headers):
    resp = await client.post(
        "/v1/security/scans",
        json={
            "target_services": ["iot", "telemetry", "media", "comms", "factory"],
            "intensity": "standard",
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert "scan_id" in data
    assert data["status"] == "completed"
    assert data["total_attack_vectors"] > 0
    assert "iot" in data["target_services"]


@pytest.mark.anyio
async def test_launch_targeted_scan(client, api_headers):
    resp = await client.post(
        "/v1/security/scans",
        json={
            "target_services": ["iot"],
            "attack_categories": ["acl_bypass", "auth_probe"],
        },
        headers=api_headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["target_services"] == ["iot"]
    assert "acl_bypass" in data["attack_categories"]


@pytest.mark.anyio
async def test_quick_scan(client, api_headers):
    resp = await client.post(
        "/v1/security/scans/quick",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["total_tests_run"] > 0
    assert "score" in data
    assert 0 <= data["score"] <= 100


# --- Report Retrieval ---


@pytest.mark.anyio
async def test_get_scan_report(client, api_headers):
    # Launch scan first
    create_resp = await client.post(
        "/v1/security/scans",
        json={"target_services": ["iot", "comms"]},
        headers=api_headers,
    )
    scan_id = create_resp.json()["scan_id"]

    resp = await client.get(
        f"/v1/security/scans/{scan_id}",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scan_id"] == scan_id
    assert "vulnerabilities" in data
    assert "recommendations" in data
    assert "severity_breakdown" in data
    assert data["total_tests_run"] == data["total_passed"] + data["total_failed"]


@pytest.mark.anyio
async def test_get_scan_not_found(client, api_headers):
    resp = await client.get(
        "/v1/security/scans/nonexistent-scan",
        headers=api_headers,
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_scans(client, api_headers):
    # Run a scan so there's at least one
    await client.post(
        "/v1/security/scans",
        json={"target_services": ["media"]},
        headers=api_headers,
    )

    resp = await client.get("/v1/security/scans", headers=api_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["scans"]) >= 1


# --- Vulnerabilities ---


@pytest.mark.anyio
async def test_get_vulnerabilities(client, api_headers):
    # Full scan should find vulns (auth probes on empty keys, privilege escalation, etc.)
    create_resp = await client.post(
        "/v1/security/scans",
        json={},
        headers=api_headers,
    )
    scan_id = create_resp.json()["scan_id"]

    resp = await client.get(
        f"/v1/security/scans/{scan_id}/vulnerabilities",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    # After patching all findings, zero vulns = fortress mode
    assert data["total"] >= 0
    assert "critical_count" in data
    assert "high_count" in data


@pytest.mark.anyio
async def test_filter_vulnerabilities_by_severity(client, api_headers):
    create_resp = await client.post(
        "/v1/security/scans",
        json={},
        headers=api_headers,
    )
    scan_id = create_resp.json()["scan_id"]

    resp = await client.get(
        f"/v1/security/scans/{scan_id}/vulnerabilities?severity=high",
        headers=api_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    for vuln in data["vulnerabilities"]:
        assert vuln["severity"] == "high"


@pytest.mark.anyio
async def test_vulnerability_has_remediation(client, api_headers):
    create_resp = await client.post(
        "/v1/security/scans",
        json={},
        headers=api_headers,
    )
    scan_id = create_resp.json()["scan_id"]

    resp = await client.get(
        f"/v1/security/scans/{scan_id}/vulnerabilities",
        headers=api_headers,
    )
    vulns = resp.json()["vulnerabilities"]
    for vuln in vulns:
        assert "remediation" in vuln
        assert len(vuln["remediation"]) > 0
        assert "evidence" in vuln
        assert "endpoint" in vuln


# --- Security Score ---


@pytest.mark.anyio
async def test_security_score_within_range(client, api_headers):
    create_resp = await client.post(
        "/v1/security/scans",
        json={},
        headers=api_headers,
    )
    scan_id = create_resp.json()["scan_id"]

    resp = await client.get(
        f"/v1/security/scans/{scan_id}",
        headers=api_headers,
    )
    score = resp.json()["score"]
    assert 0 <= score <= 100
    # After patching all Red Team findings, score should be 100 (fortress mode)
    assert score == 100.0


# --- Auth ---


@pytest.mark.anyio
async def test_security_requires_api_key(client):
    resp = await client.post(
        "/v1/security/scans",
        json={},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_every_security_route_requires_credentials(client):
    for method, path in (
        ("GET", "/v1/security/scans"),
        ("GET", "/v1/security/scans/some-scan"),
        ("GET", "/v1/security/scans/some-scan/vulnerabilities"),
        ("POST", "/v1/security/scans/quick"),
    ):
        resp = await client.request(method, path)
        assert resp.status_code == 401, (method, path)


# --- Tenant isolation ---
#
# Scans are owned by the wallet whose key launched them. A wallet-scoped key
# may read and list only its own scans; a foreign scan is indistinguishable
# from a missing one (404, no existence oracle). Bootstrap admins see all.

BOOTSTRAP_HEADERS = {"X-API-Key": "test-key"}


async def _launch_scan(client, headers, target="iot") -> str:
    resp = await client.post(
        "/v1/security/scans",
        json={"target_services": [target]},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["scan_id"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_scan_report_not_readable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    scan_a = await _launch_scan(client, a["agent_headers"])

    for path in (
        f"/v1/security/scans/{scan_a}",
        f"/v1/security/scans/{scan_a}/vulnerabilities",
        f"/v1/security/scans/{scan_a}/vulnerabilities?severity=high",
    ):
        foreign = await client.get(path, headers=b["agent_headers"])
        missing = await client.get(
            path.replace(scan_a, "nonexistent-scan"), headers=b["agent_headers"]
        )
        # Same answer as a scan that does not exist: no existence oracle.
        assert foreign.status_code == 404, (path, foreign.text)
        assert foreign.json() == missing.json()
        # Nothing of A's leaks into the denial.
        assert scan_a not in foreign.text
        assert a["agent_wallet_id"] not in foreign.text

    # The owner still reads its own scan and vulnerabilities.
    own = await client.get(f"/v1/security/scans/{scan_a}", headers=a["agent_headers"])
    assert own.status_code == 200
    assert own.json()["scan_id"] == scan_a
    own_vulns = await client.get(
        f"/v1/security/scans/{scan_a}/vulnerabilities", headers=a["agent_headers"]
    )
    assert own_vulns.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_list_scans_scoped_to_caller(client, clean_database):
    # A bootstrap-admin scan exists alongside both tenants' scans.
    admin_scan = await _launch_scan(client, BOOTSTRAP_HEADERS, "media")
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    scan_a = await _launch_scan(client, a["agent_headers"])

    # B owns nothing yet, so B's listing is empty -- not everyone's scans.
    empty = await client.get("/v1/security/scans", headers=b["agent_headers"])
    assert empty.status_code == 200
    assert empty.json() == {"scans": [], "total": 0}

    scan_b = await _launch_scan(client, b["agent_headers"], "comms")
    listed_b = await client.get("/v1/security/scans", headers=b["agent_headers"])
    assert listed_b.status_code == 200
    assert [s["scan_id"] for s in listed_b.json()["scans"]] == [scan_b]
    assert listed_b.json()["total"] == 1
    assert scan_a not in listed_b.text
    assert admin_scan not in listed_b.text

    listed_a = await client.get("/v1/security/scans", headers=a["agent_headers"])
    assert [s["scan_id"] for s in listed_a.json()["scans"]] == [scan_a]


@pytest.mark.proof
@pytest.mark.anyio
async def test_quick_scan_owned_by_launching_wallet(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    resp = await client.post("/v1/security/scans/quick", headers=a["agent_headers"])
    assert resp.status_code == 200
    quick_a = resp.json()["scan_id"]

    foreign = await client.get(
        f"/v1/security/scans/{quick_a}", headers=b["agent_headers"]
    )
    assert foreign.status_code == 404
    assert quick_a not in foreign.text
    own = await client.get(f"/v1/security/scans/{quick_a}", headers=a["agent_headers"])
    assert own.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_bootstrap_admin_sees_every_tenants_scans(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    scan_a = await _launch_scan(client, a["agent_headers"])
    scan_b = await _launch_scan(client, b["agent_headers"])

    listed = await client.get("/v1/security/scans", headers=BOOTSTRAP_HEADERS)
    assert listed.status_code == 200
    assert {scan_a, scan_b} <= {s["scan_id"] for s in listed.json()["scans"]}

    for scan_id in (scan_a, scan_b):
        report = await client.get(
            f"/v1/security/scans/{scan_id}", headers=BOOTSTRAP_HEADERS
        )
        assert report.status_code == 200
        vulns = await client.get(
            f"/v1/security/scans/{scan_id}/vulnerabilities",
            headers=BOOTSTRAP_HEADERS,
        )
        assert vulns.status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_admin_scan_not_readable_by_wallet_key(client, clean_database):
    admin_scan = await _launch_scan(client, BOOTSTRAP_HEADERS)
    a = await provision_agent_wallet(client)
    resp = await client.get(
        f"/v1/security/scans/{admin_scan}", headers=a["agent_headers"]
    )
    assert resp.status_code == 404
    assert admin_scan not in resp.text
