"""
Tests for the shared security_scans / security_vulnerabilities tables
introduced in #30. Exercises both ScanStore (red_team) and RTaaSEngine
storage to prove the discriminator ('internal' vs 'rtaas') keeps the
two scan types isolated while sharing one schema.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.db.database import get_session_factory
from app.db.models import SecurityScanModel, SecurityVulnerabilityModel
from app.schemas.red_team import (
    AttackCategory,
    RemediationStatus,
    ScanReport,
    ScanStatus,
    Severity,
    Vulnerability,
)
from app.services.red_team import ScanStore
from app.services.rtaas import (
    RTaaSEngine,
    RTaaSJob,
    RTaaSTarget,
    RTaaSVulnerability,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_security_tables():
    factory = get_session_factory()
    async with factory() as session:
        # Vulns reference scans via FK — purge them first.
        await session.execute(delete(SecurityVulnerabilityModel))
        await session.execute(delete(SecurityScanModel))
        await session.commit()
    yield


def _make_report(
    scan_id: str = "scan-1",
    with_vulns: int = 1,
) -> ScanReport:
    vulns = [
        Vulnerability(
            vuln_id=f"{scan_id}-v-{i}",
            scan_id=scan_id,
            category=AttackCategory.ACL_BYPASS,
            severity=Severity.HIGH,
            title=f"vuln {i}",
            description="test",
            endpoint="/v1/test",
            method="POST",
            evidence={"status": 200, "leaked": True},
            remediation="fix it",
            remediation_status=RemediationStatus.OPEN,
            cwe_id="CWE-285",
            discovered_at=datetime.now(timezone.utc),
        )
        for i in range(with_vulns)
    ]
    return ScanReport(
        scan_id=scan_id,
        status=ScanStatus.COMPLETED,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=5.0,
        target_services=["iot", "media"],
        attack_categories=["acl_bypass"],
        intensity="standard",
        total_tests_run=10,
        total_passed=9,
        total_failed=1,
        vulnerabilities_found=len(vulns),
        severity_breakdown={"high": len(vulns)},
        vulnerabilities=vulns,
        recommendations=["harden ACLs"],
        score=92.5,
    )


# ---------------------------------------------------------------------------
# Red Team (internal) ScanStore
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_scan_store_save_and_get_round_trip():
    store = ScanStore()
    report = _make_report()
    await store.save(report)

    got = await store.get("scan-1")
    assert got is not None
    assert got.scan_id == "scan-1"
    assert got.score == 92.5
    assert got.total_tests_run == 10
    assert got.target_services == ["iot", "media"]
    assert got.recommendations == ["harden ACLs"]
    assert len(got.vulnerabilities) == 1
    vuln = got.vulnerabilities[0]
    assert vuln.cwe_id == "CWE-285"
    assert vuln.evidence == {"status": 200, "leaked": True}


@pytest.mark.anyio
async def test_scan_store_save_is_idempotent():
    """Re-saving the same scan replaces vulnerabilities, doesn't duplicate."""
    store = ScanStore()
    await store.save(_make_report(with_vulns=3))
    await store.save(_make_report(with_vulns=1))  # resave with fewer vulns

    got = await store.get("scan-1")
    assert got is not None
    assert len(got.vulnerabilities) == 1

    # Confirm at the raw-row level too.
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(SecurityVulnerabilityModel).where(
                SecurityVulnerabilityModel.scan_id == "scan-1"
            )
        )
        rows = list(result.scalars().all())
        assert len(rows) == 1


@pytest.mark.anyio
async def test_scan_store_list_all_returns_every_internal_scan():
    """list_all is ordered newest-first, but saves made microseconds apart
    can have identical created_at on some backends — assert membership,
    not tie-breaking order."""
    store = ScanStore()
    await store.save(_make_report(scan_id="scan-old"))
    await store.save(_make_report(scan_id="scan-new"))
    reports = await store.list_all()
    assert {r.scan_id for r in reports} == {"scan-old", "scan-new"}


@pytest.mark.anyio
async def test_scan_store_reads_scoped_to_owning_wallet():
    """An owner-scoped read sees only that wallet's scans; an unscoped
    (bootstrap-admin) read sees all, including ownerless admin scans."""
    store = ScanStore()
    await store.save(_make_report(scan_id="scan-a"), owner_wallet_id="wallet-a")
    await store.save(_make_report(scan_id="scan-b"), owner_wallet_id="wallet-b")
    await store.save(_make_report(scan_id="scan-admin"))

    assert (await store.get("scan-a", "wallet-a")) is not None
    assert await store.get("scan-a", "wallet-b") is None
    assert await store.get("scan-admin", "wallet-a") is None
    assert (await store.get("scan-admin")) is not None

    assert [r.scan_id for r in await store.list_all("wallet-a")] == ["scan-a"]
    assert [r.scan_id for r in await store.list_all("wallet-b")] == ["scan-b"]
    assert await store.list_all("wallet-c") == []
    assert {r.scan_id for r in await store.list_all()} == {
        "scan-a",
        "scan-b",
        "scan-admin",
    }


@pytest.mark.anyio
async def test_scan_store_resave_never_reassigns_owner():
    store = ScanStore()
    await store.save(_make_report(scan_id="scan-a"), owner_wallet_id="wallet-a")
    await store.save(_make_report(scan_id="scan-a"), owner_wallet_id="wallet-b")

    assert await store.get("scan-a", "wallet-b") is None
    assert (await store.get("scan-a", "wallet-a")) is not None


# ---------------------------------------------------------------------------
# Discriminator isolation — the shared-schema property
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_internal_and_rtaas_isolated_by_discriminator():
    """
    Writing an rtaas job and an internal scan into the same table must
    keep them invisible to each other's query paths.
    """
    store = ScanStore()
    engine = RTaaSEngine()

    # Internal scan
    await store.save(_make_report(scan_id="internal-1"))

    # Hand-construct an rtaas job (skips require_simulation guard in create_job).
    job = RTaaSJob(
        job_id="rtaas-1",
        tenant_id="tenant-a",
        targets=[RTaaSTarget(url="https://api.example.com/")],
        attack_categories=[AttackCategory.AUTH_PROBE],
        intensity="quick",
        status="completed",
        vulnerabilities=[
            RTaaSVulnerability(
                vuln_id="rv-1",
                severity=Severity.MEDIUM,
                category=AttackCategory.AUTH_PROBE,
                target_url="https://api.example.com/",
                title="auth probe",
                description="desc",
                evidence="trace",
                cwe_id="CWE-287",
                remediation="fix auth",
            )
        ],
        total_tests_run=8,
        security_score=75.0,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    await engine._persist_job(job)

    # ScanStore sees only the internal row.
    internals = await store.list_all()
    assert [r.scan_id for r in internals] == ["internal-1"]

    # get() discriminates too.
    assert await store.get("rtaas-1") is None

    # RTaaS sees only the rtaas row.
    rtaas_jobs = await engine.list_jobs()
    assert [j.job_id for j in rtaas_jobs] == ["rtaas-1"]

    # And cross-discriminator lookups return None.
    assert await engine.get_job("internal-1") is None


# ---------------------------------------------------------------------------
# RTaaS persistence details
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rtaas_job_round_trip_preserves_targets_and_vulns():
    engine = RTaaSEngine()
    job = RTaaSJob(
        job_id="rtaas-rt",
        tenant_id="tenant-x",
        targets=[
            RTaaSTarget(
                url="https://a.example.com",
                method="POST",
                auth_header="Bearer x",
                description="primary",
            ),
            RTaaSTarget(url="https://b.example.com"),
        ],
        attack_categories=[
            AttackCategory.INJECTION,
            AttackCategory.RATE_LIMIT_EVASION,
        ],
        intensity="thorough",
        status="completed",
        vulnerabilities=[
            RTaaSVulnerability(
                vuln_id="rv-1",
                severity=Severity.HIGH,
                category=AttackCategory.INJECTION,
                target_url="https://a.example.com",
                title="SQLi",
                description="boom",
                evidence="curl trace",
                cwe_id="CWE-89",
                remediation="parameterize",
            )
        ],
        total_tests_run=30,
        security_score=60.0,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    await engine._persist_job(job)

    got = await engine.get_job("rtaas-rt")
    assert got is not None
    assert got.tenant_id == "tenant-x"
    assert got.intensity == "thorough"
    assert len(got.targets) == 2
    # The target credential is never forwarded, so it is never persisted.
    assert got.targets[0].auth_header is None
    assert got.targets[0].method == "POST"
    assert got.targets[0].description == "primary"
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(SecurityScanModel, "rtaas-rt")
    assert row is not None
    assert "Bearer x" not in (row.targets_json or "")
    assert "auth_header" not in (row.targets_json or "")
    assert {c for c in got.attack_categories} == {
        AttackCategory.INJECTION,
        AttackCategory.RATE_LIMIT_EVASION,
    }
    assert len(got.vulnerabilities) == 1
    assert got.vulnerabilities[0].evidence == "curl trace"


@pytest.mark.anyio
async def test_rtaas_list_filters_by_tenant():
    engine = RTaaSEngine()
    for tenant, job_id in [("a", "j1"), ("a", "j2"), ("b", "j3")]:
        await engine._persist_job(
            RTaaSJob(
                job_id=job_id,
                tenant_id=tenant,
                targets=[RTaaSTarget(url="https://ex.com")],
                attack_categories=[AttackCategory.AUTH_PROBE],
                status="completed",
            )
        )

    a_jobs = await engine.list_jobs(tenant_id="a")
    assert {j.job_id for j in a_jobs} == {"j1", "j2"}

    b_jobs = await engine.list_jobs(tenant_id="b")
    assert [j.job_id for j in b_jobs] == ["j3"]

    all_jobs = await engine.list_jobs()
    assert len(all_jobs) == 3
