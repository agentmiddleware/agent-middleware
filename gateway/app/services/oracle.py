"""
Agent Oracle Infiltration — Service Layer
==========================================
Crawls agent directories, indexes external API capabilities,
computes compatibility scores, and registers our API in agent
networks to drive inbound discovery traffic.

The three pillars:
1. CRAWL  — Discover external APIs via /.well-known/agent.json, /llm.txt, OpenAPI specs
2. INDEX  — Extract capabilities, compute compatibility scores, build the network graph
3. REGISTER — Push our agent profile into external directories for inbound traffic

Production wiring:
- httpx/aiohttp for async crawling
- PostgreSQL + pgvector for capability embeddings
- Redis for crawl queue and rate limiting
- Scheduled workers for periodic re-crawls
"""

import asyncio
import json
import uuid
import logging
import hashlib
from collections import defaultdict
from typing import Any, cast
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.sql.elements import ColumnElement

from ..audit.lightweight import record_audit
from ..core.runtime_mode import is_simulation, require_simulation
from ..db.converters import (
    indexed_api_to_model,
    indexed_api_model_to_schema,
    registration_result_to_model,
    registration_model_to_schema,
)
from ..db.database import get_session_factory, is_database_configured
from ..db.models import (
    OracleCrawlTargetModel,
    OracleDiscoveryHitModel,
    OracleIndexedAPIModel,
    OracleRegistrationModel,
)
from ..schemas.oracle import (
    OracleStatus,
    DirectoryType,
    CompatibilityTier,
    IndexedCapability,
    IndexedAPI,
    RegistrationResult,
    VisibilityScore,
    NetworkGraphNode,
    NetworkGraphResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compatibility Engine
# ---------------------------------------------------------------------------

# Keywords that indicate high compatibility with this control plane
COMPATIBILITY_KEYWORDS = {
    "native": [
        "agent",
        "api",
        "headless",
        "programmatic",
        "machine-to-machine",
        "webhook",
        "mqtt",
        "iot",
        "telemetry",
        "media",
        "content",
        "scheduling",
        "automation",
        "b2a",
        "mcp",
    ],
    "compatible": [
        "rest",
        "json",
        "openapi",
        "oauth",
        "api-key",
        "webhook",
        "streaming",
        "async",
        "batch",
        "graphql",
    ],
    "bridgeable": [
        "gui",
        "dashboard",
        "web-app",
        "saas",
        "portal",
        "manual",
        "interactive",
        "form",
    ],
}

# Capability overlap categories
CAPABILITY_CATEGORIES = {
    "iot": ["device", "sensor", "mqtt", "coap", "zigbee", "protocol", "bridge"],
    "media": ["video", "image", "audio", "clip", "render", "caption", "stream"],
    "comms": ["message", "agent", "chat", "notification", "webhook", "queue"],
    "analytics": ["telemetry", "metric", "event", "anomaly", "monitor", "log"],
    "content": ["content", "post", "publish", "schedule", "social", "marketing"],
    "security": ["auth", "security", "scan", "vulnerability", "penetration", "audit"],
}


class CompatibilityEngine:
    """Computes how well an external API fits this control plane."""

    @staticmethod
    def compute_score(
        name: str,
        description: str,
        capabilities: list[dict],
    ) -> tuple[CompatibilityTier, float]:
        """
        Compute compatibility tier and score based on API metadata.

        Returns (tier, score) where score is 0.0-1.0.
        """
        text = f"{name} {description} ".lower()
        for cap in capabilities:
            text += f"{cap.get('name', '')} {cap.get('description', '')} "

        # Score based on keyword overlap
        native_keywords = COMPATIBILITY_KEYWORDS["native"]
        compat_keywords = COMPATIBILITY_KEYWORDS["compatible"]
        bridge_keywords = COMPATIBILITY_KEYWORDS["bridgeable"]
        native_hits = sum(1 for kw in native_keywords if kw in text)
        compat_hits = sum(1 for kw in compat_keywords if kw in text)
        bridge_hits = sum(1 for kw in bridge_keywords if kw in text)

        total_keywords = len(native_keywords) + len(compat_keywords)
        raw_score = (native_hits * 2 + compat_hits) / max(total_keywords, 1)
        score = min(1.0, raw_score)

        # Determine tier
        if native_hits >= 4:
            tier = CompatibilityTier.NATIVE
        elif native_hits >= 2 or compat_hits >= 3:
            tier = CompatibilityTier.COMPATIBLE
        elif bridge_hits >= 2 or compat_hits >= 1:
            tier = CompatibilityTier.BRIDGEABLE
        else:
            tier = CompatibilityTier.INCOMPATIBLE

        return tier, round(score, 3)

    @staticmethod
    def categorize_capabilities(capabilities: list[dict]) -> dict[str, list[str]]:
        """Map capabilities to our category taxonomy."""
        result: dict[str, list[str]] = defaultdict(list)
        for cap in capabilities:
            text = f"{cap.get('name', '')} {cap.get('description', '')}".lower()
            for category, keywords in CAPABILITY_CATEGORIES.items():
                if any(kw in text for kw in keywords):
                    result[category].append(cap.get("name", "unknown"))
        return dict(result)


# ---------------------------------------------------------------------------
# Crawl Simulator
# ---------------------------------------------------------------------------

# Simulated external API directory (production: real HTTP crawling)
SIMULATED_DIRECTORY: list[dict] = [
    {
        "url": "https://api.openai.com",
        "name": "OpenAI API",
        "description": (
            "Large language model API with GPT-4, embeddings, and assistants."
        ),
        "directory_type": DirectoryType.OPENAPI,
        "capabilities": [
            {
                "name": "chat-completion",
                "description": "Generate text with LLMs",
                "endpoint": "/v1/chat/completions",
                "method": "POST",
            },
            {
                "name": "embeddings",
                "description": "Generate text embeddings",
                "endpoint": "/v1/embeddings",
                "method": "POST",
            },
            {
                "name": "assistants",
                "description": "Programmatic agent creation and management",
                "endpoint": "/v1/assistants",
                "method": "POST",
            },
            {
                "name": "function-calling",
                "description": "Structured tool use for agents",
                "endpoint": "/v1/chat/completions",
                "method": "POST",
            },
        ],
    },
    {
        "url": "https://api.anthropic.com",
        "name": "Anthropic API",
        "description": (
            "Claude models with tool use, MCP integration, and agent-native features."
        ),
        "directory_type": DirectoryType.OPENAPI,
        "capabilities": [
            {
                "name": "messages",
                "description": "Generate responses with Claude models",
                "endpoint": "/v1/messages",
                "method": "POST",
            },
            {
                "name": "tool-use",
                "description": "Structured tool calling for agent workflows",
                "endpoint": "/v1/messages",
                "method": "POST",
            },
            {
                "name": "mcp-integration",
                "description": "Model Context Protocol server support",
                "endpoint": "/v1/messages",
                "method": "POST",
            },
            {
                "name": "batch-api",
                "description": "Async batch processing for high-volume agent tasks",
                "endpoint": "/v1/messages/batches",
                "method": "POST",
            },
        ],
    },
    {
        "url": "https://api.stripe.com",
        "name": "Stripe API",
        "description": (
            "Payment processing, billing, and financial infrastructure "
            "for the internet."
        ),
        "directory_type": DirectoryType.OPENAPI,
        "capabilities": [
            {
                "name": "payments",
                "description": "Process payments programmatically",
                "endpoint": "/v1/payment_intents",
                "method": "POST",
            },
            {
                "name": "subscriptions",
                "description": "Recurring billing and subscription management",
                "endpoint": "/v1/subscriptions",
                "method": "POST",
            },
            {
                "name": "webhooks",
                "description": "Event-driven webhook notifications",
                "endpoint": "/v1/webhook_endpoints",
                "method": "POST",
            },
            {
                "name": "usage-billing",
                "description": "Metered API billing for agent consumption",
                "endpoint": "/v1/billing/meters",
                "method": "POST",
            },
        ],
    },
    {
        "url": "https://api.twilio.com",
        "name": "Twilio API",
        "description": "Communication APIs for messaging, voice, and video.",
        "directory_type": DirectoryType.OPENAPI,
        "capabilities": [
            {
                "name": "sms",
                "description": "Send and receive SMS messages programmatically",
                "endpoint": "/2010-04-01/Accounts/{sid}/Messages",
                "method": "POST",
            },
            {
                "name": "voice",
                "description": "Programmable voice calls",
                "endpoint": "/2010-04-01/Accounts/{sid}/Calls",
                "method": "POST",
            },
            {
                "name": "webhooks",
                "description": "Event webhooks for message status",
                "endpoint": "/v1/webhooks",
                "method": "POST",
            },
        ],
    },
    {
        "url": "https://api.github.com",
        "name": "GitHub API",
        "description": (
            "REST and GraphQL API for repositories, issues, "
            "pull requests, and automation."
        ),
        "directory_type": DirectoryType.OPENAPI,
        "capabilities": [
            {
                "name": "repositories",
                "description": "Repository management and code access",
                "endpoint": "/repos/{owner}/{repo}",
                "method": "GET",
            },
            {
                "name": "pull-requests",
                "description": "Automated PR creation and review",
                "endpoint": "/repos/{owner}/{repo}/pulls",
                "method": "POST",
            },
            {
                "name": "actions",
                "description": "CI/CD pipeline automation",
                "endpoint": "/repos/{owner}/{repo}/actions",
                "method": "GET",
            },
            {
                "name": "webhooks",
                "description": "Repository event notifications",
                "endpoint": "/repos/{owner}/{repo}/hooks",
                "method": "POST",
            },
        ],
    },
    {
        "url": "https://api.cloudflare.com",
        "name": "Cloudflare API",
        "description": (
            "Edge computing, DNS, CDN, and security for agent-deployed infrastructure."
        ),
        "directory_type": DirectoryType.OPENAPI,
        "capabilities": [
            {
                "name": "workers",
                "description": "Edge compute for agent workloads",
                "endpoint": "/client/v4/accounts/{id}/workers/scripts",
                "method": "PUT",
            },
            {
                "name": "dns",
                "description": "Programmatic DNS management",
                "endpoint": "/client/v4/zones/{id}/dns_records",
                "method": "POST",
            },
            {
                "name": "r2-storage",
                "description": "S3-compatible object storage",
                "endpoint": "/client/v4/accounts/{id}/r2/buckets",
                "method": "POST",
            },
        ],
    },
]


class CrawlEngine:
    """
    Crawls external API directories and extracts metadata.
    Production: async HTTP client with rate limiting, retry logic,
    and respect for robots.txt / crawl-delay headers.
    """

    def __init__(self):
        self._compatibility = CompatibilityEngine()

    async def crawl_target(
        self,
        url: str,
        directory_type: DirectoryType,
        tags: list[str],
    ) -> IndexedAPI | None:
        """Crawl a URL and extract API metadata."""
        # The frozen proof surface still uses deterministic/synthetic extraction;
        # simulation only gates durable hash + integration metadata, not this path.
        # Simulate network crawl (production: real HTTP requests)
        match = next((d for d in SIMULATED_DIRECTORY if d["url"] == url), None)

        if match:
            # Use simulated data
            capabilities = match["capabilities"]
            name = match["name"]
            description = match["description"]
        else:
            # Generate synthetic metadata for unknown URLs
            domain = url.split("//")[-1].split("/")[0]
            name = f"{domain.split('.')[0].title()} API"
            description = f"API service discovered at {domain}"
            capabilities = [
                {
                    "name": "api-access",
                    "description": f"REST API at {domain}",
                    "endpoint": "/",
                    "method": "GET",
                },
            ]

        # Compute compatibility
        tier, score = self._compatibility.compute_score(name, description, capabilities)

        api_id = hashlib.sha256(url.encode()).hexdigest()[:16]

        indexed_caps = [
            IndexedCapability(
                name=c["name"],
                description=c["description"],
                endpoint=c.get("endpoint"),
                method=c.get("method"),
            )
            for c in capabilities
        ]

        return IndexedAPI(
            api_id=api_id,
            url=url,
            name=name,
            description=description,
            directory_type=directory_type,
            capabilities=indexed_caps,
            compatibility_tier=tier,
            compatibility_score=score,
            tags=tags,
            last_crawled=datetime.now(timezone.utc),
            status=OracleStatus.INDEXED,
        )


# ---------------------------------------------------------------------------
# Registration Engine
# ---------------------------------------------------------------------------

# Our API profile for registration
OUR_PROFILE = {
    "name": "Agent Middleware API",
    "description": (
        "Operational control plane for autonomous agents: identity, billing, "
        "discovery, policy, and execution governance for machine-native "
        "software tenants."
    ),
    "url": "https://api.yourdomain.com",
    "capabilities": [
        "iot-protocol-bridging",
        "autonomous-code-repair",
        "programmatic-media-distribution",
        "agent-to-agent-messaging",
        "content-factory-scheduling",
        "live-campaign-orchestration",
        "red-team-security-swarm",
    ],
    "auth": {"type": "api_key", "header": "X-API-Key"},
    "discovery": {
        "well_known": "/.well-known/agent.json",
        "llm_txt": "/llm.txt",
        "openapi": "/openapi.json",
    },
}


class RegistrationEngine:
    """
    Registers our API in external agent directories.
    Production: actual HTTP POST/PUT to directory registration endpoints.
    """

    async def register(
        self,
        directory_url: str,
        directory_type: DirectoryType,
        custom_payload: dict | None = None,
        profile_overrides: dict | None = None,
    ) -> RegistrationResult:
        """Register our API with an external directory."""
        require_simulation("oracle")
        profile = {**OUR_PROFILE}
        if profile_overrides:
            profile.update(profile_overrides)

        # Simulate registration (production: real HTTP requests)
        # For now, all registrations succeed
        registration_id = str(uuid.uuid4())[:12]

        logger.info(f"Registered with {directory_url} ({directory_type.value})")

        return RegistrationResult(
            directory_url=directory_url,
            directory_type=directory_type,
            status=OracleStatus.REGISTERED,
            registration_id=registration_id,
            message=(
                f"Successfully registered as '{profile['name']}' in "
                f"{directory_type.value} directory."
            ),
        )


def domain_host_from_url(url: str) -> str:
    """Lowercase hostname from a URL (no scheme, no path)."""
    return url.split("//", 1)[-1].split("/")[0].lower()


# ---------------------------------------------------------------------------
# Oracle Store
# ---------------------------------------------------------------------------


class OracleStore:
    """PostgreSQL-backed store for crawl targets, indexed APIs, registrations, and discovery hits."""

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "OracleStore requires a configured database. Set DATABASE_URL."
            )

    async def store_target(self, target_id: str, data: dict):
        """Insert a crawl target row. Idempotent: re-storing same id is a no-op."""
        self._require_db()
        factory = get_session_factory()
        queued_at = _parse_iso(data.get("queued_at")) or datetime.now(timezone.utc)
        async with factory() as session:
            existing = await session.get(OracleCrawlTargetModel, target_id)
            if existing is not None:
                return
            session.add(
                OracleCrawlTargetModel(
                    target_id=target_id,
                    url=data.get("url", ""),
                    directory_type=data.get("directory_type", ""),
                    status=data.get("status", OracleStatus.PENDING.value),
                    api_id=data.get("api_id"),
                    queued_at=queued_at,
                )
            )
            await session.commit()

    async def update_target(
        self,
        target_id: str,
        *,
        status: str | None = None,
        api_id: str | None = None,
        crawled_at: datetime | None = None,
        raw_payload_hash: str | None = None,
    ) -> None:
        """Partial update — in-memory store used to expose the dict by
        reference; the PG-backed equivalent requires an explicit write."""
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(OracleCrawlTargetModel, target_id)
            if row is None:
                return
            if status is not None:
                row.status = status
            if api_id is not None:
                row.api_id = api_id
            if crawled_at is not None:
                row.crawled_at = crawled_at
            if raw_payload_hash is not None:
                row.raw_payload_hash = raw_payload_hash
            await session.commit()

    async def get_target(self, target_id: str) -> dict | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(OracleCrawlTargetModel, target_id)
        if row is None:
            return None
        return {
            "target_id": row.target_id,
            "url": row.url,
            "directory_type": row.directory_type,
            "status": row.status,
            "api_id": row.api_id,
            "queued_at": row.queued_at.isoformat() if row.queued_at else None,
            "crawled_at": row.crawled_at.isoformat() if row.crawled_at else None,
            "raw_payload_hash": row.raw_payload_hash,
        }

    async def store_indexed(self, api: IndexedAPI):
        """Upsert — re-indexing the same API replaces its row."""
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            existing = await session.get(OracleIndexedAPIModel, api.api_id)
            new_row = indexed_api_to_model(api)
            if existing is None:
                session.add(new_row)
            else:
                for field in (
                    "url",
                    "name",
                    "description",
                    "directory_type",
                    "compatibility_tier",
                    "compatibility_score",
                    "capabilities_json",
                    "tags_json",
                    "status",
                    "last_crawled",
                ):
                    setattr(existing, field, getattr(new_row, field))
            await session.commit()

    async def get_indexed(self, api_id: str) -> IndexedAPI | None:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(OracleIndexedAPIModel, api_id)
        return indexed_api_model_to_schema(row) if row else None

    async def list_indexed(
        self,
        tier: CompatibilityTier | None = None,
        directory_type: DirectoryType | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[IndexedAPI], int]:
        """Return (page of indexed APIs, total matching filters)."""
        self._require_db()
        factory = get_session_factory()
        filters: list = []
        if tier:
            filters.append(OracleIndexedAPIModel.compatibility_tier == tier.value)
        if directory_type:
            filters.append(OracleIndexedAPIModel.directory_type == directory_type.value)
        count_stmt = select(func.count()).select_from(OracleIndexedAPIModel)
        stmt = select(OracleIndexedAPIModel)
        if filters:
            count_stmt = count_stmt.where(*filters)
            stmt = stmt.where(*filters)
        stmt = stmt.order_by(
            cast(ColumnElement[Any], OracleIndexedAPIModel.compatibility_score).desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        async with factory() as session:
            total = int((await session.execute(count_stmt)).scalar_one())
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [indexed_api_model_to_schema(r) for r in rows], total

    async def list_crawl_targets(
        self,
        *,
        domain: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Crawl target rows whose URL matches domain (substring, case-insensitive)."""
        self._require_db()
        factory = get_session_factory()
        needle = f"%{domain.strip()}%"
        filt = cast(ColumnElement[str], OracleCrawlTargetModel.url).ilike(needle)
        count_stmt = (
            select(func.count()).select_from(OracleCrawlTargetModel).where(filt)
        )
        stmt = (
            select(OracleCrawlTargetModel)
            .where(filt)
            .order_by(cast(ColumnElement[Any], OracleCrawlTargetModel.queued_at).desc())
            .limit(limit)
            .offset(offset)
        )
        async with factory() as session:
            total = int((await session.execute(count_stmt)).scalar_one())
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "target_id": row.target_id,
                    "url": row.url,
                    "domain": domain_host_from_url(row.url),
                    "directory_type": row.directory_type,
                    "status": row.status,
                    "api_id": row.api_id,
                    "queued_at": row.queued_at,
                    "crawled_at": row.crawled_at,
                    "raw_payload_hash": row.raw_payload_hash,
                }
            )
        return out, total

    async def store_registration(self, result: RegistrationResult):
        self._require_db()
        factory = get_session_factory()
        # RegistrationResult may omit registration_id on failure — synthesize
        # one so we still have a stable PK for audit.
        if not result.registration_id:
            result = result.model_copy(
                update={"registration_id": f"failed-{uuid.uuid4().hex[:12]}"}
            )
        async with factory() as session:
            session.add(registration_result_to_model(result))
            await session.commit()

    async def get_registrations(self) -> list[RegistrationResult]:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(OracleRegistrationModel).order_by(
                    cast(ColumnElement[Any], OracleRegistrationModel.created_at).desc()
                )
            )
            rows = list(result.scalars().all())
        return [registration_model_to_schema(r) for r in rows]

    async def record_discovery_hit(self, referrer: str = "direct"):
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                OracleDiscoveryHitModel(
                    hit_id=uuid.uuid4().hex,
                    referrer=referrer or "direct",
                )
            )
            await session.commit()

    async def get_stats(self) -> dict:
        self._require_db()
        factory = get_session_factory()
        async with factory() as session:
            targets = (
                await session.scalar(
                    select(func.count()).select_from(OracleCrawlTargetModel)
                )
                or 0
            )
            indexed = (
                await session.scalar(
                    select(func.count()).select_from(OracleIndexedAPIModel)
                )
                or 0
            )
            registrations = (
                await session.scalar(
                    select(func.count()).select_from(OracleRegistrationModel)
                )
                or 0
            )
            hits = (
                await session.scalar(
                    select(func.count()).select_from(OracleDiscoveryHitModel)
                )
                or 0
            )

            referrer_rows = await session.execute(
                select(
                    cast(ColumnElement[str], OracleDiscoveryHitModel.referrer),
                    func.count().label("n"),
                )
                .group_by(OracleDiscoveryHitModel.referrer)
                .order_by(func.count().desc())
                .limit(10)
            )
            top_referrers = {r.referrer: int(r.n) for r in referrer_rows}

        return {
            "targets_crawled": int(targets),
            "apis_indexed": int(indexed),
            "registrations": int(registrations),
            "discovery_hits": int(hits),
            "top_referrers": top_referrers,
        }


def _parse_iso(value: object) -> datetime | None:
    """Best-effort parse of ISO-8601 timestamps passed into store_target."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Agent Oracle Orchestrator
# ---------------------------------------------------------------------------


class AgentOracle:
    """
    Top-level orchestrator for Agent Oracle Infiltration.

    Three operations:
    1. crawl()    — Discover and index external APIs
    2. register() — Push our profile into agent directories
    3. score()    — Compute our visibility across the agent network
    """

    def __init__(self):
        self.store = OracleStore()
        self.crawler = CrawlEngine()
        self.registrar = RegistrationEngine()

    async def crawl(
        self,
        url: str,
        directory_type: DirectoryType = DirectoryType.WELL_KNOWN,
        tags: list[str] | None = None,
        priority: int = 5,
        audit_context: dict[str, Any] | None = None,
    ) -> IndexedAPI | None:
        """Crawl a URL, index it, and compute compatibility."""
        target_id = str(uuid.uuid4())
        raw_hash: str | None = None

        await self.store.store_target(
            target_id,
            {
                "target_id": target_id,
                "url": url,
                "directory_type": directory_type.value,
                "status": OracleStatus.CRAWLING.value,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Execute crawl
        indexed = await self.crawler.crawl_target(url, directory_type, tags or [])

        if indexed:
            await self.store.store_indexed(indexed)
            if not is_simulation("oracle"):
                payload_for_hash: dict[str, Any] = {
                    "url": url,
                    "directory_type": directory_type.value,
                    "tags": sorted(tags or []),
                    "api_id": indexed.api_id,
                    "name": indexed.name,
                    "compatibility_tier": indexed.compatibility_tier.value,
                    "compatibility_score": indexed.compatibility_score,
                }
                raw_hash = hashlib.sha256(
                    json.dumps(payload_for_hash, sort_keys=True).encode()
                ).hexdigest()
            await self.store.update_target(
                target_id,
                status=OracleStatus.INDEXED.value,
                api_id=indexed.api_id,
                crawled_at=datetime.now(timezone.utc),
                raw_payload_hash=raw_hash,
            )
        else:
            await self.store.update_target(
                target_id,
                status=OracleStatus.FAILED.value,
                crawled_at=datetime.now(timezone.utc),
            )

        if audit_context is not None:
            record_audit(
                "oracle.crawl",
                actor_source=audit_context.get("source"),
                key_id=audit_context.get("key_id"),
                wallet_id=audit_context.get("wallet_id"),
                url=url,
                outcome="indexed" if indexed else "failed",
                payload_hash=raw_hash or "",
            )

        return indexed  # type: ignore[no-any-return]

    async def batch_crawl(
        self,
        urls: list[str],
        audit_context: dict[str, Any] | None = None,
    ) -> list[IndexedAPI]:
        """Crawl multiple URLs concurrently."""
        tasks = [self.crawl(url, audit_context=audit_context) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, IndexedAPI)]

    async def register_in_directories(
        self,
        targets: list[dict],
        profile_overrides: dict | None = None,
    ) -> list[RegistrationResult]:
        """Register our API in multiple external directories."""
        results = []
        for target in targets:
            result = await self.registrar.register(
                directory_url=target["directory_url"],
                directory_type=DirectoryType(target["directory_type"]),
                custom_payload=target.get("registration_payload"),
                profile_overrides=profile_overrides,
            )
            await self.store.store_registration(result)
            results.append(result)
        return results

    async def compute_visibility(self) -> VisibilityScore:
        """Compute our overall visibility score across agent networks."""
        stats = await self.store.get_stats()
        await self.store.get_registrations()
        indexed_apis, _ = await self.store.list_indexed()

        # Compute compatibility distribution
        compat_map: dict[str, int] = defaultdict(int)
        for api in indexed_apis:
            compat_map[api.compatibility_tier.value] += 1

        # Compute visibility score (0-100)
        # Up to 40 pts for registrations
        reg_score = min(40, stats["registrations"] * 10)
        # Up to 30 pts for indexed APIs
        index_score = min(30, stats["apis_indexed"] * 5)
        # Up to 30 pts for discovery hits
        discovery_score = min(30, stats["discovery_hits"] * 3)
        overall = min(100.0, float(reg_score + index_score + discovery_score))

        # Generate recommendations
        recommendations = []
        if stats["registrations"] < 3:
            recommendations.append(
                "Register in more agent directories to increase inbound discovery. "
                "Target: /.well-known/agent.json directories, MCP server listings, "
                "and plugin stores."
            )
        if stats["apis_indexed"] < 5:
            recommendations.append(
                "Crawl more external APIs to build your network graph. "
                "Focus on APIs with 'native' or 'compatible' tiers "
                "for integration partnerships."
            )
        native_count = compat_map.get("native", 0)
        if native_count < 2:
            recommendations.append(
                "Seek out more agent-native APIs "
                "(those with /llm.txt, /.well-known/agent.json). "
                "These are your highest-value integration partners."
            )
        if stats["discovery_hits"] == 0:
            recommendations.append(
                "No inbound discovery traffic yet. "
                "Ensure your /.well-known/agent.json and /llm.txt "
                "are publicly accessible and register in at least 3 directories."
            )

        # Top referrers
        top_referrers = [
            {"directory": k, "hits": v}
            for k, v in sorted(
                stats.get("top_referrers", {}).items(),
                key=lambda x: x[1],
                reverse=True,
            )[:5]
        ]

        return VisibilityScore(
            overall_score=overall,
            directories_registered=stats["registrations"],
            directories_crawled=stats["targets_crawled"],
            inbound_discovery_requests=stats["discovery_hits"],
            top_referrers=top_referrers,
            compatibility_map=dict(compat_map),
            recommendations=recommendations,
        )

    async def get_network_graph(self) -> NetworkGraphResponse:
        """Build a network graph of all indexed APIs centered on our API."""
        indexed, _ = await self.store.list_indexed()
        registrations = await self.store.get_registrations()

        nodes: list[NetworkGraphNode] = []
        edges: list[dict] = []

        # Our API is the center node
        self_node = NetworkGraphNode(
            node_id="self",
            name="Agent Middleware API",
            url="https://api.yourdomain.com",
            node_type="self",
            connections=[],
        )
        nodes.append(self_node)

        # Add indexed APIs
        for api in indexed:
            node = NetworkGraphNode(
                node_id=api.api_id,
                name=api.name,
                url=api.url,
                node_type="indexed_api",
                compatibility_tier=api.compatibility_tier,
                connections=["self"],
            )
            nodes.append(node)
            self_node.connections.append(api.api_id)
            edges.append(
                {
                    "source": "self",
                    "target": api.api_id,
                    "relationship": "indexed",
                    "compatibility": api.compatibility_tier.value,
                    "score": api.compatibility_score,
                }
            )

        # Add registered directories
        for reg in registrations:
            reg_id = f"dir-{hashlib.sha256(reg.directory_url.encode()).hexdigest()[:8]}"
            if not any(n.node_id == reg_id for n in nodes):
                node = NetworkGraphNode(
                    node_id=reg_id,
                    name=reg.directory_url.split("//")[-1].split("/")[0],
                    url=reg.directory_url,
                    node_type="directory",
                    connections=["self"],
                )
                nodes.append(node)
                self_node.connections.append(reg_id)
                edges.append(
                    {
                        "source": "self",
                        "target": reg_id,
                        "relationship": "registered",
                    }
                )

        return NetworkGraphResponse(
            nodes=nodes,
            edges=edges,
            total_nodes=len(nodes),
            total_edges=len(edges),
            center_node="self",
        )

    async def record_discovery(self, referrer: str = "direct"):
        """Record an inbound discovery hit (called when agents find us)."""
        await self.store.record_discovery_hit(referrer)
