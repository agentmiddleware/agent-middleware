"""
Autonomous Product Manager — Service Layer
============================================
Ingests telemetry, detects anomalies via statistical analysis,
and generates code fixes as pull requests.

The PM operates on three levels:
1. INGEST — Buffer and index incoming events
2. ANALYZE — Detect anomalies via sliding window statistics
3. ACT — Generate diffs and optionally push PRs

In production, swap the in-memory event store for ClickHouse or TimescaleDB,
and wire the PR generator to an actual LLM + git integration.
"""

import asyncio
import hashlib
import uuid
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, delete, func
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from ..core.durable_state import get_durable_state
from ..core.runtime_mode import require_simulation
from ..core.time import to_naive_utc, utc_now
from ..db.converters import (
    telemetry_event_to_model,
    telemetry_event_model_to_schema,
)
from ..db.database import get_session_factory, is_database_configured
from ..db.models import TelemetryEventModel
from ..schemas.telemetry import (
    TelemetryEvent,
    Severity,
    TelemetryEventType,
    AnomalyReport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tenant ownership
# ---------------------------------------------------------------------------
#
# Telemetry is tenant-scoped: the tenant is the wallet that ingested the
# events. The owner travels in the server-generated ``event_id``, so no schema
# change is needed. A wallet's events are stored as ``<owner>.<uuid4 hex>``,
# where ``<owner>`` is ``owner_for_wallet(wallet_id)``. That is a fixed-length
# digest, so the id always fits the 50-char column and never exposes the
# wallet id. Events ingested by a bootstrap admin, and rows written before
# scoping existed, keep a bare UUID with no separator. They form the unowned
# partition (owner ``None``), which only bootstrap admins can read. Clients
# never choose event ids, so a caller cannot place an event in another
# tenant's partition.

_OWNER_SEPARATOR = "."


class _AnyOwner:
    """Owner-scope sentinel: no tenant filter (bootstrap-admin reads)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ANY_OWNER"


ANY_OWNER = _AnyOwner()

# A read is scoped to one owner partition (a wallet's owner key, or None for
# the unowned partition) or to ANY_OWNER, meaning every partition.
OwnerScope = str | None | _AnyOwner


def owner_for_wallet(wallet_id: str) -> str:
    """Telemetry partition key for a wallet: ``w`` plus 16 hex digest chars."""
    digest = hashlib.sha256(wallet_id.encode("utf-8")).hexdigest()[:16]
    return f"w{digest}"


def event_owner(event_id: str) -> str | None:
    """Owner partition of a stored event id (``None`` = unowned)."""
    owner, separator, _ = event_id.partition(_OWNER_SEPARATOR)
    return owner if separator else None


def _new_event_id(owner: str | None) -> str:
    if owner is None:
        return str(uuid.uuid4())
    return f"{owner}{_OWNER_SEPARATOR}{uuid.uuid4().hex}"


def _owner_visible(owner: str | None, scope: OwnerScope) -> bool:
    return isinstance(scope, _AnyOwner) or owner == scope


def _owner_clauses(scope: OwnerScope) -> list[ColumnElement[bool]]:
    """SQL filter restricting telemetry rows to ``scope``'s partition."""
    if isinstance(scope, _AnyOwner):
        return []
    event_id = col(TelemetryEventModel.event_id)
    if scope is None:
        return [~event_id.contains(_OWNER_SEPARATOR, autoescape=True)]
    return [event_id.startswith(f"{scope}{_OWNER_SEPARATOR}", autoescape=True)]


# ---------------------------------------------------------------------------
# Event Store
# ---------------------------------------------------------------------------


@dataclass
class StoredEvent:
    """An event with storage metadata."""

    event_id: str
    batch_id: str
    event: TelemetryEvent
    ingested_at: datetime


def _row_to_stored(row: TelemetryEventModel) -> StoredEvent:
    """Materialize a DB row back into the StoredEvent shape the anomaly
    detector expects."""
    return StoredEvent(
        event_id=row.event_id,
        batch_id=row.batch_id,
        event=telemetry_event_model_to_schema(row),
        ingested_at=row.ingested_at,
    )


class EventStore:
    """
    Time-series event store backed by PostgreSQL (TimescaleDB-friendly).

    Keeps the public API (ingest / query / stats / _evict_expired) stable
    so the rest of telemetry_pm — notably AnomalyDetector — is unchanged.
    """

    def __init__(self, retention_hours: int = 168):
        self._retention = timedelta(hours=retention_hours)

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "telemetry_pm.EventStore requires a configured database. "
                "Set DATABASE_URL."
            )

    async def ingest(
        self,
        events: list[TelemetryEvent],
        batch_id: str,
        owner: str | None = None,
    ) -> tuple[int, list[dict]]:
        """Persist a batch of events into ``owner``'s partition (``None`` =
        unowned). Returns (ingested_count, errors)."""
        self._require_db()
        factory = get_session_factory()

        errors: list[dict] = []
        now = utc_now()

        rows: list[TelemetryEventModel] = []
        for i, event in enumerate(events):
            try:
                rows.append(
                    telemetry_event_to_model(
                        event_id=_new_event_id(owner),
                        batch_id=batch_id,
                        event=event,
                        ingested_at=now,
                    )
                )
            except Exception as exc:  # pragma: no cover — validation is upstream
                errors.append({"index": i, "error": str(exc)})

        async with factory() as session:
            session.add_all(rows)
            await session.commit()

        # Lazy retention sweep on every ingest.
        await self._evict_expired()

        return len(rows), errors

    async def query(
        self,
        event_type: TelemetryEventType | None = None,
        severity: Severity | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
        owner: OwnerScope = ANY_OWNER,
    ) -> list[StoredEvent]:
        """Query events with optional filters, newest first.

        ``owner`` limits the result to one tenant's partition; the default
        ``ANY_OWNER`` reads every partition.
        """
        self._require_db()
        factory = get_session_factory()

        stmt = select(TelemetryEventModel).where(*_owner_clauses(owner))
        since = to_naive_utc(since) if since else None
        until = to_naive_utc(until) if until else None
        if event_type:
            stmt = stmt.where(
                cast(
                    ColumnElement[bool],
                    TelemetryEventModel.event_type == event_type.value,
                )
            )
        if severity:
            stmt = stmt.where(
                cast(
                    ColumnElement[bool], TelemetryEventModel.severity == severity.value
                )
            )
        if source:
            stmt = stmt.where(
                cast(ColumnElement[bool], TelemetryEventModel.source == source)
            )
        if since:
            # Compare against the client-supplied event_timestamp when
            # present, else fall back to ingested_at so rows without a
            # client ts still match.
            stmt = stmt.where(
                cast(
                    ColumnElement[bool],
                    func.coalesce(
                        TelemetryEventModel.event_timestamp,
                        TelemetryEventModel.ingested_at,
                    )
                    >= since,
                )
            )
        if until:
            stmt = stmt.where(
                cast(
                    ColumnElement[bool],
                    func.coalesce(
                        TelemetryEventModel.event_timestamp,
                        TelemetryEventModel.ingested_at,
                    )
                    <= until,
                )
            )
        stmt = stmt.order_by(
            cast(ColumnElement[Any], TelemetryEventModel.ingested_at).desc()
        ).limit(limit)

        async with factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        return [_row_to_stored(r) for r in rows]

    async def owners(
        self,
        event_type: TelemetryEventType | None = None,
        since: datetime | None = None,
    ) -> list[str | None]:
        """Distinct owner partitions holding matching events (``None`` =
        unowned), so detection can run once per tenant."""
        self._require_db()
        factory = get_session_factory()

        stmt = select(col(TelemetryEventModel.event_id))
        if event_type:
            stmt = stmt.where(col(TelemetryEventModel.event_type) == event_type.value)
        if since:
            stmt = stmt.where(
                func.coalesce(
                    TelemetryEventModel.event_timestamp,
                    TelemetryEventModel.ingested_at,
                )
                >= to_naive_utc(since)
            )

        async with factory() as session:
            event_ids = (await session.execute(stmt)).scalars().all()

        found = {event_owner(event_id) for event_id in event_ids}
        return sorted(found, key=lambda o: (o is not None, o or ""))

    async def stats(self, owner: OwnerScope = ANY_OWNER) -> dict:
        """Aggregate stats by type / severity / source over ``owner``'s
        partition (every partition by default)."""
        self._require_db()
        factory = get_session_factory()
        scoped = _owner_clauses(owner)

        async with factory() as session:
            total = (
                await session.scalar(
                    select(func.count()).select_from(TelemetryEventModel).where(*scoped)
                )
                or 0
            )

            by_type: dict[str, int] = defaultdict(int)
            by_severity: dict[str, int] = defaultdict(int)
            by_source: dict[str, int] = defaultdict(int)

            result = await session.execute(
                select(
                    cast(ColumnElement[str], TelemetryEventModel.event_type),
                    cast(ColumnElement[str], TelemetryEventModel.severity),
                    cast(ColumnElement[str], TelemetryEventModel.source),
                    func.count().label("n"),
                )
                .where(*scoped)
                .group_by(
                    TelemetryEventModel.event_type,
                    TelemetryEventModel.severity,
                    TelemetryEventModel.source,
                )
            )
            for event_type, severity, source, count in result.all():
                by_type[event_type] += count
                by_severity[severity] += count
                by_source[source] += count

        return {
            "total": total,
            "by_type": dict(by_type),
            "by_severity": dict(by_severity),
            "by_source": dict(by_source),
        }

    async def _evict_expired(self) -> int:
        """Delete events older than the retention window. Returns row count."""
        self._require_db()
        factory = get_session_factory()
        cutoff = utc_now() - self._retention

        async with factory() as session:
            result = await session.execute(
                delete(TelemetryEventModel).where(
                    cast(ColumnElement[bool], TelemetryEventModel.ingested_at < cutoff)
                )
            )
            await session.commit()
            # `execute()` on a DELETE returns a CursorResult at runtime (rowcount
            # is always present for DML), but the generic `Result[Any]` return
            # type doesn't expose it statically.
            return int(cast(CursorResult, result).rowcount or 0)


# ---------------------------------------------------------------------------
# Anomaly Detector
# ---------------------------------------------------------------------------


@dataclass
class AnomalyCandidate:
    """Internal anomaly representation before promotion to report."""

    category: str
    severity: Severity
    summary: str
    affected_endpoints: list[str]
    event_ids: list[str]
    first_seen: datetime
    last_seen: datetime


class AnomalyDetector:
    """
    Statistical anomaly detection over sliding windows.

    Detection strategies:
    - Error rate spike: >3x baseline error rate in a 5-minute window
    - Latency regression: p95 latency >2x baseline
    - Missing feature signal: repeated 404s on non-existent endpoints
    - Source concentration: >80% of errors from a single source

    Detection runs per owner partition (tenant = ingesting wallet): each
    anomaly is derived from one tenant's events only and is recorded with that
    owner, so it is visible to that tenant and to bootstrap admins alone.
    """

    def __init__(self, event_store: EventStore):
        self._store = event_store
        self._anomalies: dict[str, AnomalyReport] = {}
        # anomaly_id -> owner partition (None = unowned: bootstrap-admin
        # telemetry, or an anomaly persisted before tenant scoping).
        self._owners: dict[str, str | None] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._hydrated = False
        self._state = get_durable_state()

    async def _hydrate_if_needed(self):
        if self._hydrated:
            return

        async with self._init_lock:
            if self._hydrated:
                return

            payload = await self._state.load_json("telemetry.anomalies")
            if isinstance(payload, dict):
                loaded: dict[str, AnomalyReport] = {}
                owners: dict[str, str | None] = {}
                for anomaly_id, record in payload.items():
                    try:
                        loaded[anomaly_id] = AnomalyReport.model_validate(record)
                    except Exception:
                        logger.exception(
                            "Skipping corrupt telemetry anomaly: %s",
                            anomaly_id,
                        )
                        continue
                    # A record without a valid owner fails closed to the
                    # unowned (bootstrap-admin-only) partition.
                    owner = record.get("owner") if isinstance(record, dict) else None
                    owners[anomaly_id] = owner if isinstance(owner, str) else None
                self._anomalies = loaded
                self._owners = owners

            self._hydrated = True

    async def _persist_locked(self):
        if not self._state.enabled:
            return
        await self._state.save_json(
            "telemetry.anomalies",
            {
                k: {**v.model_dump(mode="json"), "owner": self._owners.get(k)}
                for k, v in self._anomalies.items()
            },
        )

    async def analyze(self) -> list[AnomalyReport]:
        """
        Run all detection strategies and return new/updated anomalies.

        Strategies run separately over each owner partition with recent
        errors, so one tenant's events never feed another tenant's anomaly.

        Call this periodically (e.g., every 60 seconds).
        """
        await self._hydrate_if_needed()
        reports: list[AnomalyReport] = []

        owners = await self._store.owners(
            event_type=TelemetryEventType.ERROR,
            since=utc_now() - timedelta(hours=1),
        )
        for owner in owners:
            # Strategy 1: Error rate spike; Strategy 2: Source concentration
            for detect in (
                self._detect_error_spike,
                self._detect_source_concentration,
            ):
                candidate = await detect(owner)
                if candidate:
                    reports.append(await self._record(candidate, owner))

        return reports

    async def _record(
        self, candidate: AnomalyCandidate, owner: str | None
    ) -> AnomalyReport:
        anomaly_id = f"anom-{uuid.uuid4().hex[:8]}"
        report = AnomalyReport(
            anomaly_id=anomaly_id,
            severity=candidate.severity,
            category=candidate.category,
            summary=candidate.summary,
            affected_endpoints=candidate.affected_endpoints,
            event_count=len(candidate.event_ids),
            first_seen=candidate.first_seen,
            last_seen=candidate.last_seen,
        )
        async with self._lock:
            self._anomalies[anomaly_id] = report
            self._owners[anomaly_id] = owner
            await self._persist_locked()
        logger.warning(f"Anomaly detected: [{report.severity}] {report.summary}")
        return report

    async def get_anomalies(
        self,
        severity: Severity | None = None,
        page: int = 1,
        per_page: int = 50,
        owner: OwnerScope = ANY_OWNER,
    ) -> tuple[list[AnomalyReport], int]:
        await self._hydrate_if_needed()
        anomalies = [
            a
            for anomaly_id, a in self._anomalies.items()
            if _owner_visible(self._owners.get(anomaly_id), owner)
        ]
        if severity:
            anomalies = [a for a in anomalies if a.severity == severity]
        anomalies.sort(key=lambda a: a.last_seen, reverse=True)
        total = len(anomalies)
        start = (page - 1) * per_page
        return anomalies[start : start + per_page], total

    async def get_anomaly(
        self, anomaly_id: str, owner: OwnerScope = ANY_OWNER
    ) -> AnomalyReport | None:
        found = await self.get_owned_anomaly(anomaly_id, owner)
        return found[0] if found else None

    async def get_owned_anomaly(
        self, anomaly_id: str, owner: OwnerScope = ANY_OWNER
    ) -> tuple[AnomalyReport, str | None] | None:
        """Return ``(report, anomaly_owner)`` when the anomaly exists and is
        visible to ``owner``; otherwise ``None``, so a foreign anomaly is
        indistinguishable from a missing one."""
        await self._hydrate_if_needed()
        report = self._anomalies.get(anomaly_id)
        anomaly_owner = self._owners.get(anomaly_id)
        if report is None or not _owner_visible(anomaly_owner, owner):
            return None
        return report, anomaly_owner

    async def _detect_error_spike(self, owner: str | None) -> AnomalyCandidate | None:
        """Detect if ``owner``'s error rate exceeds 3x its baseline in the
        last 5 minutes."""
        now = utc_now()
        window = timedelta(minutes=5)
        baseline_window = timedelta(hours=1)

        recent_errors = await self._store.query(
            event_type=TelemetryEventType.ERROR,
            since=now - window,
            owner=owner,
        )
        baseline_errors = await self._store.query(
            event_type=TelemetryEventType.ERROR,
            since=now - baseline_window,
            until=now - window,
            owner=owner,
        )

        if not recent_errors:
            return None

        recent_rate = len(recent_errors) / window.total_seconds()
        baseline_seconds = (baseline_window - window).total_seconds()
        baseline_rate = (
            len(baseline_errors) / baseline_seconds if baseline_seconds > 0 else 0
        )

        if baseline_rate > 0 and recent_rate > baseline_rate * 3:
            sources = set(se.event.source for se in recent_errors)
            return AnomalyCandidate(
                category="error_spike",
                severity=Severity.HIGH,
                summary=(
                    f"Error rate spike: {recent_rate:.2f}/s vs baseline "
                    f"{baseline_rate:.2f}/s across {', '.join(sources)}"
                ),
                affected_endpoints=list(sources),
                event_ids=[se.event_id for se in recent_errors],
                first_seen=recent_errors[-1].ingested_at,
                last_seen=recent_errors[0].ingested_at,
            )
        return None

    async def _detect_source_concentration(
        self, owner: str | None
    ) -> AnomalyCandidate | None:
        """Detect if >80% of ``owner``'s errors come from a single source."""
        now = utc_now()
        recent_errors = await self._store.query(
            event_type=TelemetryEventType.ERROR,
            since=now - timedelta(hours=1),
            owner=owner,
        )

        if len(recent_errors) < 10:  # Need minimum sample
            return None

        source_counts: dict[str, int] = defaultdict(int)
        for se in recent_errors:
            source_counts[se.event.source] += 1

        total = len(recent_errors)
        for source, count in source_counts.items():
            if count / total > 0.8:
                return AnomalyCandidate(
                    category="source_concentration",
                    severity=Severity.MEDIUM,
                    summary=(
                        f"{count}/{total} errors ({count / total:.0%}) "
                        f"originate from '{source}'"
                    ),
                    affected_endpoints=[source],
                    event_ids=[
                        se.event_id for se in recent_errors if se.event.source == source
                    ],
                    first_seen=recent_errors[-1].ingested_at,
                    last_seen=recent_errors[0].ingested_at,
                )
        return None


# ---------------------------------------------------------------------------
# Auto-PR Generator
# ---------------------------------------------------------------------------


class AutoPRGenerator:
    """
    Generates a placeholder code fix for an anomaly (simulated proof surface).

    A real adapter would:
    1. Gather context from the anomaly + related telemetry
    2. Send context to an LLM to generate a fix
    3. Run the test suite against the fix
    4. Push a PR if tests pass and dry_run=False

    Only step 1 exists. Steps 2-4 are not implemented, so results never
    report a created PR or a test outcome.
    """

    def __init__(self, git_remote: str = "", branch_prefix: str = "auto-pm/"):
        self.git_remote = git_remote
        self.branch_prefix = branch_prefix

    async def generate_fix(
        self,
        anomaly: AnomalyReport,
        related_events: list[StoredEvent],
        dry_run: bool = True,
    ) -> dict:
        """
        Generate a placeholder code fix for an anomaly.
        Returns diff and files_changed; pr_url and tests_passed are always
        None, and status is "dry_run" or (when dry_run=False) "simulated".
        """
        require_simulation("telemetry_pm")
        # Build context for the LLM
        _context = self._build_context(anomaly, related_events)

        # In production: call LLM API to generate fix
        # For now, return a structured placeholder
        diff = self._generate_placeholder_diff(anomaly)
        files = self._infer_affected_files(anomaly)

        # Nothing here runs a test suite, pushes a branch, or opens a PR, so
        # the result must never claim otherwise: tests_passed stays unknown,
        # pr_url stays null, and a non-dry-run request is marked "simulated"
        # rather than "pr_created" with a fabricated URL.
        result = {
            "anomaly_id": anomaly.anomaly_id,
            "diff": diff,
            "files_changed": files,
            "tests_passed": None,
            "context_events": len(related_events),
            "pr_url": None,
            "status": "dry_run" if dry_run else "simulated",
        }

        if not dry_run:
            logger.info(
                "Auto-PR simulated for %s: no PR opened (would-be branch %s%s)",
                anomaly.anomaly_id,
                self.branch_prefix,
                anomaly.anomaly_id,
            )

        return result

    def _build_context(self, anomaly: AnomalyReport, events: list[StoredEvent]) -> str:
        """Build LLM context from anomaly and events."""
        lines = [
            f"Anomaly: {anomaly.summary}",
            f"Category: {anomaly.category}",
            f"Severity: {anomaly.severity}",
            f"Affected: {', '.join(anomaly.affected_endpoints)}",
            f"Event count: {anomaly.event_count}",
            "",
            "Sample events:",
        ]
        for se in events[:10]:
            lines.append(
                f"  [{se.event.severity}] {se.event.source}: {se.event.message}"
            )
            if se.event.stack_trace:
                # Include first 5 lines of stack trace
                trace_lines = se.event.stack_trace.strip().split("\n")[:5]
                for tl in trace_lines:
                    lines.append(f"    {tl}")
        return "\n".join(lines)

    def _generate_placeholder_diff(self, anomaly: AnomalyReport) -> str:
        """Generate a placeholder diff. Replace with LLM output in production."""
        return (
            f"--- a/PLACEHOLDER\n"
            f"+++ b/PLACEHOLDER\n"
            f"@@ -1,3 +1,5 @@\n"
            f" # Auto-generated fix for {anomaly.anomaly_id}\n"
            f" # Category: {anomaly.category}\n"
            f"+# Fix: Address {anomaly.summary}\n"
            f"+# TODO: Replace this placeholder with LLM-generated fix\n"
            f" # Affected: {', '.join(anomaly.affected_endpoints)}\n"
        )

    def _infer_affected_files(self, anomaly: AnomalyReport) -> list[str]:
        """Infer which files to modify based on affected endpoints."""
        file_map = {
            "iot-bridge": "app/routers/iot.py",
            "media-engine": "app/routers/media.py",
            "auth-service": "app/core/auth.py",
            "telemetry": "app/routers/telemetry.py",
        }
        files = []
        for endpoint in anomaly.affected_endpoints:
            if endpoint in file_map:
                files.append(file_map[endpoint])
            else:
                files.append(f"app/services/{endpoint.replace('-', '_')}.py")
        return files or ["app/main.py"]


# ---------------------------------------------------------------------------
# Autonomous PM Orchestrator
# ---------------------------------------------------------------------------


class AutonomousPM:
    """
    Top-level orchestrator for the Autonomous Product Manager.
    Coordinates event ingestion, anomaly detection, and auto-PR generation.
    """

    def __init__(
        self,
        retention_hours: int = 168,
        git_remote: str = "",
        branch_prefix: str = "auto-pm/",
    ):
        self.event_store = EventStore(retention_hours=retention_hours)
        self.detector = AnomalyDetector(self.event_store)
        self.pr_generator = AutoPRGenerator(git_remote, branch_prefix)
        self._analysis_task: asyncio.Task | None = None

    async def start_background_analysis(self, interval_seconds: int = 60):
        """Start periodic anomaly analysis in the background."""

        async def _loop():
            while True:
                try:
                    new_anomalies = await self.detector.analyze()
                    if new_anomalies:
                        logger.info(f"Detected {len(new_anomalies)} new anomalies")
                except Exception as e:
                    logger.error(f"Anomaly analysis error: {e}")
                await asyncio.sleep(interval_seconds)

        self._analysis_task = asyncio.create_task(_loop())
        logger.info(
            f"Background anomaly analysis started (interval={interval_seconds}s)"
        )

    async def stop_background_analysis(self):
        if self._analysis_task:
            self._analysis_task.cancel()
