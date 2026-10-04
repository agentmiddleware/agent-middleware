"""
Red-Team-as-a-Service (RTaaS) modeling — dormant simulated proof surface.

Models a tenant-scoped scan-job lifecycle (job intake, tracking, structured
vulnerability reports) for scans an agent would request against its own
external endpoints. It is a simulation: no external service is ever
contacted and no scanning engine exists. Entry points are guarded by
``require_simulation`` and refuse to run unless the simulation flag is
explicitly enabled; the routes that expose this module are proof surfaces,
unmounted by default (see docs/PROOF_SURFACES.md).
"""

import json
import uuid
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from ..core.runtime_mode import require_simulation
from ..db.database import get_session_factory, is_database_configured
from ..db.models import SecurityScanModel, SecurityVulnerabilityModel

from ..schemas.red_team import AttackCategory, Severity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RTaaS Job Model
# ---------------------------------------------------------------------------


@dataclass
class RTaaSTarget:
    """An external endpoint to attack."""

    url: str
    method: str = "GET"
    # A live credential for a third-party endpoint. The simulation never
    # contacts the target, so it is never forwarded and never persisted (see
    # _job_to_models); create_job does not even retain it.
    auth_header: str | None = None
    description: str = ""


@dataclass
class RTaaSVulnerability:
    """A vulnerability found during external scanning."""

    vuln_id: str
    severity: Severity
    category: AttackCategory
    target_url: str
    title: str
    description: str
    evidence: str = ""
    cwe_id: str | None = None
    remediation: str = ""


@dataclass
class RTaaSJob:
    """A multi-tenant Red Team scanning job."""

    job_id: str
    tenant_id: str  # The agent/wallet requesting the scan
    targets: list[RTaaSTarget]
    attack_categories: list[AttackCategory]
    intensity: str = "standard"  # "quick", "standard", "thorough"
    status: str = "pending"  # "pending", "running", "completed", "failed"
    vulnerabilities: list[RTaaSVulnerability] = field(default_factory=list)
    total_tests_run: int = 0
    security_score: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# RTaaS Engine
# ---------------------------------------------------------------------------


class RTaaSEngine:
    """
    Multi-tenant Red Team scanning engine.

    Operations:
    1. create_job()   — Submit external targets for penetration testing
    2. get_job()      — Check job status and results
    3. list_jobs()    — View all jobs for a tenant
    """

    def __init__(self):
        # Kept for backward compatibility with any tests that still poke at
        # the dict directly; the authoritative source is PostgreSQL.
        self._jobs: dict[str, RTaaSJob] = {}

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "RTaaSEngine requires a configured database. Set DATABASE_URL."
            )

    @staticmethod
    def _job_to_models(
        job: RTaaSJob,
    ) -> tuple[SecurityScanModel, list[SecurityVulnerabilityModel]]:
        scan = SecurityScanModel(
            scan_id=job.job_id,
            scan_type="rtaas",
            tenant_id=job.tenant_id,
            # Drop auth_header: a target credential must never land in the
            # database in plaintext, and nothing ever reads it back.
            targets_json=json.dumps(
                [
                    {k: v for k, v in asdict(t).items() if k != "auth_header"}
                    for t in job.targets
                ],
                default=str,
            ),
            attack_categories_json=json.dumps([c.value for c in job.attack_categories]),
            intensity=job.intensity,
            status=job.status,
            total_tests_run=job.total_tests_run,
            total_passed=None,
            total_failed=None,
            security_score=job.security_score,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )
        vulns = [
            SecurityVulnerabilityModel(
                vuln_id=v.vuln_id,
                scan_id=job.job_id,
                category=v.category.value,
                severity=v.severity.value,
                title=v.title,
                description=v.description,
                endpoint=v.target_url,
                method=None,
                evidence_json=(
                    json.dumps({"text": v.evidence}) if v.evidence else None
                ),
                remediation=v.remediation,
                remediation_status="open",
                cwe_id=v.cwe_id,
                discovered_at=job.completed_at or datetime.now(timezone.utc),
            )
            for v in job.vulnerabilities
        ]
        return scan, vulns

    @staticmethod
    def _models_to_job(
        scan: SecurityScanModel,
        vulns: list[SecurityVulnerabilityModel],
    ) -> RTaaSJob:
        target_records: list[object] = []
        if scan.targets_json:
            try:
                target_records = json.loads(scan.targets_json) or []
            except json.JSONDecodeError:
                target_records = []
        targets = [
            # auth_header is deliberately not rehydrated, even from a row
            # written before it stopped being persisted.
            RTaaSTarget(
                url=t.get("url", ""),
                method=t.get("method", "GET"),
                description=t.get("description", ""),
            )
            for t in target_records
            if isinstance(t, dict)
        ]

        cats: list[AttackCategory] = []
        if scan.attack_categories_json:
            try:
                for c in json.loads(scan.attack_categories_json) or []:
                    try:
                        cats.append(AttackCategory(c))
                    except ValueError:
                        continue
            except json.JSONDecodeError:
                pass

        rtaas_vulns = []
        for v in vulns:
            evidence_text = ""
            if v.evidence_json:
                try:
                    parsed = json.loads(v.evidence_json)
                    if isinstance(parsed, dict):
                        evidence_text = parsed.get("text", "")
                except json.JSONDecodeError:
                    pass
            rtaas_vulns.append(
                RTaaSVulnerability(
                    vuln_id=v.vuln_id,
                    severity=Severity(v.severity),
                    category=AttackCategory(v.category),
                    target_url=v.endpoint,
                    title=v.title,
                    description=v.description,
                    evidence=evidence_text,
                    cwe_id=v.cwe_id,
                    remediation=v.remediation,
                )
            )

        return RTaaSJob(
            job_id=scan.scan_id,
            tenant_id=scan.tenant_id or "",
            targets=targets,
            attack_categories=cats,
            intensity=scan.intensity,
            status=scan.status,
            vulnerabilities=rtaas_vulns,
            total_tests_run=scan.total_tests_run,
            security_score=scan.security_score,
            started_at=scan.started_at,
            completed_at=scan.completed_at,
            created_at=scan.created_at,
        )

    async def _persist_job(self, job: RTaaSJob) -> None:
        """Upsert job + replace its vulnerabilities for idempotent rewrites."""
        self._require_db()
        factory = get_session_factory()
        scan_row, vuln_rows = self._job_to_models(job)

        async with factory() as session:
            existing = await session.get(SecurityScanModel, job.job_id)
            if existing is None:
                session.add(scan_row)
            else:
                for field_name in (
                    "scan_type",
                    "tenant_id",
                    "targets_json",
                    "attack_categories_json",
                    "intensity",
                    "status",
                    "total_tests_run",
                    "total_passed",
                    "total_failed",
                    "security_score",
                    "started_at",
                    "completed_at",
                ):
                    setattr(existing, field_name, getattr(scan_row, field_name))

            from sqlalchemy import delete as sa_delete

            await session.execute(
                sa_delete(SecurityVulnerabilityModel).where(
                    cast(
                        ColumnElement[bool],
                        SecurityVulnerabilityModel.scan_id == job.job_id,
                    )
                )
            )
            session.add_all(vuln_rows)
            await session.commit()

    async def create_job(
        self,
        tenant_id: str,
        targets: list[dict],
        attack_categories: list[str] | None = None,
        intensity: str = "standard",
    ) -> RTaaSJob:
        """Create and execute an RTaaS scanning job against external endpoints."""
        require_simulation("rtaas")
        job_id = f"rtaas-{uuid.uuid4().hex[:12]}"

        parsed_targets = [
            # Any caller-supplied auth_header is discarded: the simulation
            # contacts no target, so there is nothing to forward it to.
            RTaaSTarget(
                url=t["url"],
                method=t.get("method", "GET"),
                description=t.get("description", ""),
            )
            for t in targets
        ]

        categories = (
            [AttackCategory(c) for c in attack_categories]
            if attack_categories
            else list(AttackCategory)
        )

        job = RTaaSJob(
            job_id=job_id,
            tenant_id=tenant_id,
            targets=parsed_targets,
            attack_categories=categories,
            intensity=intensity,
            status="running",
            started_at=datetime.now(timezone.utc),
        )

        # Simulate scanning
        vulns, tests_run, score = self._simulate_external_scan(
            parsed_targets, categories, intensity
        )
        job.vulnerabilities = vulns
        job.total_tests_run = tests_run
        job.security_score = score
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)

        self._jobs[job_id] = job
        await self._persist_job(job)
        logger.info(
            f"RTaaS job {job_id}: {len(vulns)} vulns found across "
            f"{len(parsed_targets)} targets ({tests_run} tests)"
        )
        return job

    def _simulate_external_scan(
        self,
        targets: list[RTaaSTarget],
        categories: list[AttackCategory],
        intensity: str,
    ) -> tuple[list[RTaaSVulnerability], int, float]:
        """Simulate scanning external endpoints.

        Production: this would use OWASP ZAP, Nuclei, or custom fuzzers
        against actual HTTP endpoints.
        """
        import hashlib

        vulns = []
        tests_per_target = {"quick": 8, "standard": 15, "thorough": 25}
        num_tests = tests_per_target.get(intensity, 15)
        total_tests = len(targets) * len(categories) * num_tests

        # Deterministic simulation based on target URLs. Use a stable digest
        # for the category too: the builtin hash() of a str is salted per
        # process (PYTHONHASHSEED), so it would give different findings for the
        # same URL after a restart or on another worker.
        cat_hashes = {
            cat: int(hashlib.md5(cat.value.encode()).hexdigest()[:8], 16)
            for cat in categories
        }
        for target in targets:
            url_hash = int(hashlib.md5(target.url.encode()).hexdigest()[:8], 16)

            for cat in categories:
                # Simulate finding vulnerabilities probabilistically
                if (url_hash + cat_hashes[cat]) % 7 == 0:
                    severity = Severity.HIGH if url_hash % 3 == 0 else Severity.MEDIUM
                    vulns.append(
                        RTaaSVulnerability(
                            vuln_id=f"v-{uuid.uuid4().hex[:8]}",
                            severity=severity,
                            category=cat,
                            target_url=target.url,
                            title=(
                                f"{cat.value.replace('_', ' ').title()} "
                                "vulnerability detected"
                            ),
                            description=f"Potential {cat.value} issue at {target.url}",
                            evidence=(
                                f"Simulated attack against {target.method} {target.url}"
                            ),
                            cwe_id=self._cwe_for_category(cat),
                            remediation=self._remediation_for_category(cat),
                        )
                    )

                if (url_hash + cat_hashes[cat]) % 11 == 0:
                    vulns.append(
                        RTaaSVulnerability(
                            vuln_id=f"v-{uuid.uuid4().hex[:8]}",
                            severity=Severity.LOW,
                            category=cat,
                            target_url=target.url,
                            title=f"Minor {cat.value.replace('_', ' ')} finding",
                            description=f"Low-risk {cat.value} observation",
                            evidence="Informational finding from automated scan",
                            cwe_id=self._cwe_for_category(cat),
                            remediation="Consider hardening as a best practice.",
                        )
                    )

        # Score: 100 - penalty per vuln
        penalty = sum(
            {"critical": 25, "high": 15, "medium": 8, "low": 3}.get(v.severity.value, 5)
            for v in vulns
        )
        score = max(0, min(100, 100 - penalty))

        return vulns, total_tests, score

    def _cwe_for_category(self, cat: AttackCategory) -> str:
        mapping = {
            AttackCategory.ACL_BYPASS: "CWE-285",
            AttackCategory.AUTH_PROBE: "CWE-287",
            AttackCategory.INJECTION: "CWE-89",
            AttackCategory.RATE_LIMIT_EVASION: "CWE-770",
            AttackCategory.PRIVILEGE_ESCALATION: "CWE-269",
            AttackCategory.SCHEMA_ABUSE: "CWE-20",
            AttackCategory.ENUMERATION: "CWE-200",
            AttackCategory.REPLAY: "CWE-294",
            AttackCategory.DOS_PATTERN: "CWE-400",
        }
        return mapping.get(cat, "CWE-000")

    def _remediation_for_category(self, cat: AttackCategory) -> str:
        mapping = {
            AttackCategory.ACL_BYPASS: (
                "Implement topic-level ACLs. Validate resource ownership per request."
            ),
            AttackCategory.AUTH_PROBE: (
                "Enforce authentication on all endpoints. Validate API keys "
                "server-side."
            ),
            AttackCategory.INJECTION: (
                "Sanitize all user inputs. Use parameterized queries."
            ),
            AttackCategory.RATE_LIMIT_EVASION: (
                "Add sliding-window rate limiting per API key."
            ),
            AttackCategory.PRIVILEGE_ESCALATION: (
                "Enforce principle of least privilege. Check role on every request."
            ),
            AttackCategory.SCHEMA_ABUSE: (
                "Validate all input schemas strictly. Reject unexpected types."
            ),
            AttackCategory.ENUMERATION: (
                "Use non-sequential IDs. Audit response payloads for data leaks."
            ),
            AttackCategory.REPLAY: (
                "Add nonce or timestamp validation. Use short-lived tokens."
            ),
            AttackCategory.DOS_PATTERN: (
                "Add request timeouts and resource limits. Cap expensive operations."
            ),
        }
        return mapping.get(cat, "Review and harden the affected endpoint.")

    async def get_job(self, job_id: str) -> RTaaSJob | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            scan = await session.get(SecurityScanModel, job_id)
            if scan is None or scan.scan_type != "rtaas":
                return None
            vuln_result = await session.execute(
                select(SecurityVulnerabilityModel).where(
                    cast(
                        ColumnElement[bool],
                        SecurityVulnerabilityModel.scan_id == job_id,
                    )
                )
            )
            vulns = list(vuln_result.scalars().all())
        return self._models_to_job(scan, vulns)

    async def list_jobs(self, tenant_id: str | None = None) -> list[RTaaSJob]:
        self._require_db()
        factory = get_session_factory()

        stmt = select(SecurityScanModel).where(
            cast(ColumnElement[bool], SecurityScanModel.scan_type == "rtaas")
        )
        if tenant_id:
            stmt = stmt.where(
                cast(ColumnElement[bool], SecurityScanModel.tenant_id == tenant_id)
            )
        stmt = stmt.order_by(
            cast(ColumnElement[Any], SecurityScanModel.created_at).desc()
        )

        async with factory() as session:
            result = await session.execute(stmt)
            scans = list(result.scalars().all())

            jobs: list[RTaaSJob] = []
            for scan in scans:
                vuln_result = await session.execute(
                    select(SecurityVulnerabilityModel).where(
                        cast(
                            ColumnElement[bool],
                            SecurityVulnerabilityModel.scan_id == scan.scan_id,
                        )
                    )
                )
                vulns = list(vuln_result.scalars().all())
                jobs.append(self._models_to_job(scan, vulns))
        return jobs
