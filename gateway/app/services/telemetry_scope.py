"""
Telemetry Scoping — Multi-Tenant Autonomous PM (Pillar 14)
============================================================
When an agent deploys a tool for other agents to use, it needs
to monitor that tool's performance — not just the platform's.

This service extends the core Autonomous PM (Pillar 2) with
tenant-scoped telemetry pipelines: each builder-agent gets its
own isolated event stream, anomaly detection, and auto-PR generation.

Architecture:
  Builder Agent → POST /v1/telemetry-scope/pipelines → TelemetryScope
    → Creates isolated telemetry pipeline for the agent's tool
    → Events from the tool flow into the scoped pipeline
    → Anomaly detection runs per-tenant
    → Auto-PR generation targets the builder-agent's repo

Production wiring:
- Kafka topic-per-tenant for event isolation
- Separate anomaly detection models per pipeline
- GitHub App integration for multi-repo PR generation
"""

import uuid
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline Models
# ---------------------------------------------------------------------------


@dataclass
class TelemetryPipeline:
    """A tenant-scoped telemetry pipeline."""

    pipeline_id: str
    tenant_id: str
    service_name: str
    git_repo_url: str = ""
    webhook_url: str = ""
    events: list[dict] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)
    auto_prs: list[dict] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"  # active, paused, archived


@dataclass
class ScopedAnomaly:
    """Anomaly detected in a tenant's telemetry stream."""

    anomaly_id: str
    pipeline_id: str
    event_type: str
    description: str
    severity: str  # low, medium, high, critical
    suggested_fix: str = ""
    auto_pr_url: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Telemetry Scope Engine
# ---------------------------------------------------------------------------


class TelemetryScope:
    """
    Multi-tenant telemetry pipeline manager.

    Operations:
    1. create_pipeline()    — Isolated event stream for an agent's tool
    2. ingest_events()      — Route events to the scoped pipeline
    3. detect_anomalies()   — Run anomaly detection on the pipeline
    4. generate_auto_pr()   — Auto-generate fix PRs for the agent's repo
    """

    def __init__(self):
        self._pipelines: dict[str, TelemetryPipeline] = {}

    async def create_pipeline(
        self,
        tenant_id: str,
        service_name: str,
        git_repo_url: str = "",
        webhook_url: str = "",
    ) -> TelemetryPipeline:
        """Create a new tenant-scoped telemetry pipeline."""
        pipeline_id = f"pipe-{uuid.uuid4().hex[:12]}"
        pipeline = TelemetryPipeline(
            pipeline_id=pipeline_id,
            tenant_id=tenant_id,
            service_name=service_name,
            git_repo_url=git_repo_url,
            webhook_url=webhook_url,
        )
        self._pipelines[pipeline_id] = pipeline
        logger.info(
            f"Created telemetry pipeline {pipeline_id} for {tenant_id}/{service_name}"
        )
        return pipeline

    async def ingest_events(
        self,
        pipeline_id: str,
        events: list[dict],
    ) -> dict:
        """Ingest events into a scoped pipeline."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        if pipeline.status != "active":
            raise ValueError(f"Pipeline {pipeline_id} is {pipeline.status}")

        for event in events:
            event["ingested_at"] = datetime.now(timezone.utc).isoformat()
            event["pipeline_id"] = pipeline_id
            pipeline.events.append(event)

        # Auto-detect anomalies on ingest
        new_anomalies = self._detect_anomalies_in_batch(pipeline, events)
        pipeline.anomalies.extend(new_anomalies)

        return {
            "pipeline_id": pipeline_id,
            "events_ingested": len(events),
            "total_events": len(pipeline.events),
            "anomalies_detected": len(new_anomalies),
        }

    def _detect_anomalies_in_batch(
        self, pipeline: TelemetryPipeline, events: list[dict]
    ) -> list[dict]:
        """Detect anomalies in a batch of events."""
        anomalies = []

        # Check for error rate spikes
        error_events = [e for e in events if e.get("level") in ("error", "fatal")]
        if len(error_events) > len(events) * 0.3 and len(events) >= 5:
            anomaly = {
                "anomaly_id": f"anom-{uuid.uuid4().hex[:8]}",
                "pipeline_id": pipeline.pipeline_id,
                "event_type": "error_rate_spike",
                "description": (
                    f"Error rate spike: {len(error_events)}/{len(events)} events "
                    f"are errors."
                ),
                "severity": "high",
                "suggested_fix": (
                    "Check recent deployments. Review error logs for root cause."
                ),
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }
            anomalies.append(anomaly)

        # Check for latency anomalies
        latencies = [e.get("latency_ms", 0) for e in events if "latency_ms" in e]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            if avg_latency > 5000:
                anomaly = {
                    "anomaly_id": f"anom-{uuid.uuid4().hex[:8]}",
                    "pipeline_id": pipeline.pipeline_id,
                    "event_type": "high_latency",
                    "description": (
                        f"Average latency {avg_latency:.0f}ms exceeds 5000ms threshold."
                    ),
                    "severity": "medium",
                    "suggested_fix": (
                        "Profile slow endpoints. Check DB queries and external API "
                        "calls."
                    ),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
                anomalies.append(anomaly)

        # Check for repeated failures on same endpoint
        failed_endpoints: dict[str, int] = defaultdict(int)
        for e in events:
            if e.get("status_code", 200) >= 500:
                failed_endpoints[e.get("path", "unknown")] += 1

        for path, count in failed_endpoints.items():
            if count >= 3:
                anomaly = {
                    "anomaly_id": f"anom-{uuid.uuid4().hex[:8]}",
                    "pipeline_id": pipeline.pipeline_id,
                    "event_type": "endpoint_failure",
                    "description": (
                        f"Endpoint {path} failed {count} times in this batch."
                    ),
                    "severity": "high",
                    "suggested_fix": (
                        f"Investigate {path}. Check handler logic and dependencies."
                    ),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
                anomalies.append(anomaly)

        return anomalies

    async def get_anomalies(self, pipeline_id: str) -> list[dict]:
        """Get all anomalies for a pipeline."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")
        return pipeline.anomalies

    async def generate_auto_pr(self, pipeline_id: str, anomaly_id: str) -> dict:
        """Generate an auto-PR for an anomaly (simulated)."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        anomaly = None
        for a in pipeline.anomalies:
            if a["anomaly_id"] == anomaly_id:
                anomaly = a
                break

        if not anomaly:
            raise ValueError(
                f"Anomaly {anomaly_id} not found in pipeline {pipeline_id}"
            )

        pr = {
            "pr_id": f"pr-{uuid.uuid4().hex[:8]}",
            "pipeline_id": pipeline_id,
            "anomaly_id": anomaly_id,
            "title": (
                f"[Auto-Fix] {anomaly['event_type']}: {anomaly['description'][:60]}"
            ),
            "body": (
                f"## Automated Fix\n\n"
                f"**Anomaly:** {anomaly['description']}\n"
                f"**Severity:** {anomaly['severity']}\n"
                f"**Suggested Fix:** "
                f"{anomaly.get('suggested_fix', 'Review manually')}\n\n"
                f"This PR was auto-generated by the Telemetry Scope engine."
            ),
            "target_repo": pipeline.git_repo_url or "not_configured",
            "status": "draft" if pipeline.git_repo_url else "simulated",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        pipeline.auto_prs.append(pr)
        logger.info(f"Generated auto-PR {pr['pr_id']} for anomaly {anomaly_id}")
        return pr

    async def get_pipeline(self, pipeline_id: str) -> TelemetryPipeline | None:
        return self._pipelines.get(pipeline_id)

    async def list_pipelines(
        self, tenant_id: str | None = None
    ) -> list[TelemetryPipeline]:
        pipelines = list(self._pipelines.values())
        if tenant_id:
            pipelines = [p for p in pipelines if p.tenant_id == tenant_id]
        return pipelines

    async def get_pipeline_stats(self, pipeline_id: str) -> dict:
        """Get aggregated stats for a pipeline."""
        pipeline = self._pipelines.get(pipeline_id)
        if not pipeline:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        events = pipeline.events
        error_count = sum(1 for e in events if e.get("level") in ("error", "fatal"))
        latencies = [e.get("latency_ms", 0) for e in events if "latency_ms" in e]

        return {
            "pipeline_id": pipeline_id,
            "service_name": pipeline.service_name,
            "total_events": len(events),
            "error_events": error_count,
            "error_rate": round(error_count / len(events), 3) if events else 0,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1)
            if latencies
            else 0,
            "max_latency_ms": max(latencies) if latencies else 0,
            "anomalies_detected": len(pipeline.anomalies),
            "auto_prs_generated": len(pipeline.auto_prs),
            "status": pipeline.status,
        }
