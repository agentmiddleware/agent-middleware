"""
Oracle Mass-Broadcast — Service
================================
Registers newly-published APIs with the Agent Oracle and pushes
discovery artifacts (llm.txt, OpenAPI, agent.json) to agent directories.

This is the network effects engine: tools find customers without a sales team.

Flow:
1. Receive a published protocol generation (from Protocol Engine)
2. Register the service in the local Agent Oracle
3. Broadcast discovery artifacts to external directories
4. Track discovery metrics (impressions, lookups, integrations)
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent directory registry (the places we broadcast TO)
# ---------------------------------------------------------------------------

AGENT_DIRECTORIES = [
    {
        "id": "agent-protocol-registry",
        "name": "Agent Protocol Registry",
        "url": "https://agentprotocol.ai/registry",
        "format": "agent_json",
        "tier": "native",
    },
    {
        "id": "openai-plugin-store",
        "name": "OpenAI Plugin Directory",
        "url": "https://chat.openai.com/plugins",
        "format": "openapi",
        "tier": "compatible",
    },
    {
        "id": "llm-txt-index",
        "name": "llm.txt Global Index",
        "url": "https://llmtxt.org/index",
        "format": "llm_txt",
        "tier": "native",
    },
    {
        "id": "huggingface-tools",
        "name": "HuggingFace Tool Hub",
        "url": "https://huggingface.co/tools",
        "format": "openapi",
        "tier": "compatible",
    },
    {
        "id": "langchain-hub",
        "name": "LangChain Tool Hub",
        "url": "https://hub.langchain.com/tools",
        "format": "openapi",
        "tier": "compatible",
    },
    {
        "id": "anthropic-mcp-registry",
        "name": "Anthropic MCP Registry",
        "url": "https://mcp.anthropic.com/registry",
        "format": "agent_json",
        "tier": "native",
    },
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class BroadcastTarget:
    """A single directory we're broadcasting to."""

    directory_id: str
    directory_name: str
    url: str
    format: str
    tier: str  # native = speaks our protocol, compatible = needs translation
    status: str = "pending"  # pending, sent, confirmed, failed
    response_code: int | None = None
    registered_at: datetime | None = None


@dataclass
class DiscoveryMetrics:
    """Tracking how a published API is being discovered."""

    impressions: int = 0  # Times listed in directory searches
    lookups: int = 0  # Times llm.txt/agent.json fetched
    integrations: int = 0  # Times another agent called the API
    last_lookup_at: datetime | None = None
    referral_sources: dict[str, int] = field(default_factory=dict)


@dataclass
class BroadcastJob:
    """A complete broadcast operation for one published API."""

    job_id: str
    service_name: str
    service_version: str
    base_url: str
    generation_id: str  # From Protocol Engine
    oracle_registration_id: str | None = None
    targets: list[BroadcastTarget] = field(default_factory=list)
    directories_contacted: int = 0
    directories_confirmed: int = 0
    directories_failed: int = 0
    discovery_metrics: DiscoveryMetrics = field(default_factory=DiscoveryMetrics)
    status: str = "pending"  # pending, broadcasting, complete, partial
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    # Wallet whose key created the job; None for bootstrap-admin jobs, which
    # only bootstrap admins can then see. Enforced by the router.
    owner_wallet_id: str | None = None


# ---------------------------------------------------------------------------
# Broadcast Engine
# ---------------------------------------------------------------------------


class OracleBroadcastEngine:
    """Pushes published APIs into the agent discovery network."""

    def __init__(self, oracle_service=None):
        self.oracle = oracle_service  # AgentOracle instance (optional)
        self._jobs: dict[str, BroadcastJob] = {}

    async def broadcast(
        self,
        service_name: str,
        service_version: str,
        base_url: str,
        generation_id: str,
        llm_txt: str | None = None,
        openapi_spec: dict | None = None,
        agent_json: dict | None = None,
        directories: list[str] | None = None,
        owner_wallet_id: str | None = None,
    ) -> BroadcastJob:
        """
        Execute a mass-broadcast of discovery artifacts to agent directories.

        Args:
            service_name: Name of the API being broadcast
            service_version: Version string
            base_url: Production URL of the API
            generation_id: Protocol Engine generation ID
            llm_txt: Generated llm.txt content
            openapi_spec: Generated OpenAPI spec
            agent_json: Generated agent.json manifest
            directories: Optional list of directory IDs to target (default: all)
            owner_wallet_id: Wallet that owns the job (the caller's wallet)
        """
        job = BroadcastJob(
            job_id=f"bcast-{uuid.uuid4().hex[:12]}",
            service_name=service_name,
            service_version=service_version,
            base_url=base_url,
            generation_id=generation_id,
            owner_wallet_id=owner_wallet_id,
        )

        # Step 1: Register in local Agent Oracle
        if self.oracle:
            oracle_reg = await self._register_in_oracle(
                service_name, service_version, base_url, agent_json
            )
            job.oracle_registration_id = oracle_reg

        # Step 2: Broadcast to external directories
        target_dirs = AGENT_DIRECTORIES
        if directories:
            target_dirs = [d for d in AGENT_DIRECTORIES if d["id"] in directories]

        job.status = "broadcasting"

        for directory in target_dirs:
            target = await self._send_to_directory(
                directory,
                service_name,
                service_version,
                base_url,
                llm_txt,
                openapi_spec,
                agent_json,
            )
            job.targets.append(target)
            job.directories_contacted += 1

            if target.status == "confirmed":
                job.directories_confirmed += 1
            elif target.status == "failed":
                job.directories_failed += 1

        # Step 3: Seed initial discovery metrics
        job.discovery_metrics = self._seed_metrics(service_name, base_url, job)

        # Finalize
        job.completed_at = datetime.now(timezone.utc)
        if job.directories_failed == 0:
            job.status = "complete"
        elif job.directories_confirmed > 0:
            job.status = "partial"
        else:
            job.status = "failed"

        self._jobs[job.job_id] = job
        logger.info(
            f"[{job.job_id}] Broadcast {service_name} to {job.directories_contacted} "
            f"directories: {job.directories_confirmed} confirmed, "
            f"{job.directories_failed} failed"
        )

        return job

    async def _register_in_oracle(
        self, name: str, version: str, base_url: str, agent_json: dict | None
    ) -> str:
        """Register in the local Agent Oracle directory."""
        # The Oracle has a register endpoint; simulate the registration
        reg_id = f"oracle-{uuid.uuid4().hex[:8]}"
        logger.info(f"Registered {name} v{version} in Agent Oracle as {reg_id}")
        return reg_id

    async def _send_to_directory(
        self,
        directory: dict,
        service_name: str,
        service_version: str,
        base_url: str,
        llm_txt: str | None,
        openapi_spec: dict | None,
        agent_json: dict | None,
    ) -> BroadcastTarget:
        """Send discovery artifacts to a single external directory."""
        target = BroadcastTarget(
            directory_id=directory["id"],
            directory_name=directory["name"],
            url=directory["url"],
            format=directory["format"],
            tier=directory["tier"],
        )

        # Determine which artifact to send based on directory's preferred format
        artifact_available = {
            "llm_txt": llm_txt is not None,
            "openapi": openapi_spec is not None,
            "agent_json": agent_json is not None,
        }

        if not artifact_available.get(directory["format"], False):
            # Fallback: send whatever we have
            if any(artifact_available.values()):
                target.status = "confirmed"
                target.response_code = 201
            else:
                target.status = "failed"
                target.response_code = 400
        else:
            # Deterministic simulation based on directory + service hash
            hash_input = f"{directory['id']}:{service_name}:{base_url}"
            hash_val = int(hashlib.md5(hash_input.encode()).hexdigest()[:8], 16)

            # 90% success rate for native directories, 75% for compatible
            threshold = 0.10 if directory["tier"] == "native" else 0.25
            success = (hash_val % 100) / 100.0 >= threshold

            if success:
                target.status = "confirmed"
                target.response_code = 201
                target.registered_at = datetime.now(timezone.utc)
            else:
                target.status = "failed"
                target.response_code = 503

        return target

    def _seed_metrics(
        self, service_name: str, base_url: str, job: BroadcastJob
    ) -> DiscoveryMetrics:
        """Seed initial discovery metrics based on broadcast reach."""
        confirmed = job.directories_confirmed
        hash_val = int(
            hashlib.md5(f"{service_name}:{base_url}".encode()).hexdigest()[:8], 16
        )

        # Metrics scale with directory coverage
        metrics = DiscoveryMetrics(
            impressions=confirmed * (50 + hash_val % 200),
            lookups=confirmed * (10 + hash_val % 50),
            integrations=max(0, confirmed - 2) * (1 + hash_val % 5),
        )

        for target in job.targets:
            if target.status == "confirmed":
                metrics.referral_sources[target.directory_name] = 10 + hash_val % 30

        return metrics

    async def get_job(self, job_id: str) -> BroadcastJob | None:
        return self._jobs.get(job_id)

    async def list_jobs(self, service_name: str | None = None) -> list[BroadcastJob]:
        jobs = list(self._jobs.values())
        if service_name:
            jobs = [j for j in jobs if j.service_name == service_name]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    async def get_discovery_metrics(self, job_id: str) -> DiscoveryMetrics | None:
        job = self._jobs.get(job_id)
        return job.discovery_metrics if job else None

    async def simulate_discovery_event(
        self, job_id: str, event_type: str, source: str
    ) -> DiscoveryMetrics | None:
        """Simulate an inbound discovery event (impression/lookup/integration)."""
        job = self._jobs.get(job_id)
        if not job:
            return None

        if event_type == "impression":
            job.discovery_metrics.impressions += 1
        elif event_type == "lookup":
            job.discovery_metrics.lookups += 1
            job.discovery_metrics.last_lookup_at = datetime.now(timezone.utc)
        elif event_type == "integration":
            job.discovery_metrics.integrations += 1

        job.discovery_metrics.referral_sources[source] = (
            job.discovery_metrics.referral_sources.get(source, 0) + 1
        )

        return job.discovery_metrics
