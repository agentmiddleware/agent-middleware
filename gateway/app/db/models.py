"""
SQLModel database models for Agent Middleware API.
Defines normalized tables for wallets, ledger entries, and billing alerts.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Index, Text, UniqueConstraint, text
from sqlmodel import SQLModel, Field

from app.core.time import utc_now
from app.db.types import NaiveUTCDateTime


class WalletModel(SQLModel, table=True):
    """
    Wallet table for storing agent and sponsor wallets.

    Uses Decimal for all monetary fields to avoid floating-point errors.
    """

    __tablename__ = "wallets"

    wallet_id: str = Field(primary_key=True, max_length=50)
    wallet_type: str = Field(max_length=20, index=True)
    owner_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)

    # Monetary fields - all Decimal for precision
    # Provider-authoritative refunds can leave a sponsor with a negative
    # balance after the corresponding credits have already been delegated or
    # spent. In that state the balance is the durable liability and the Stripe
    # settlement path keeps the wallet non-spendable for operator review.
    balance: Decimal = Field(default=Decimal("0"), decimal_places=8)
    lifetime_credits: Decimal = Field(default=Decimal("0"), decimal_places=8)
    lifetime_debits: Decimal = Field(default=Decimal("0"), decimal_places=8)

    # Hierarchy fields (for agent/child wallets)
    parent_wallet_id: Optional[str] = Field(
        default=None, foreign_key="wallets.wallet_id", index=True
    )
    agent_id: Optional[str] = Field(default=None, max_length=100, index=True)

    # Child wallet specific fields
    child_agent_id: Optional[str] = Field(default=None, max_length=100)
    max_spend: Optional[Decimal] = Field(default=None, decimal_places=8)
    task_description: Optional[str] = Field(default=None, max_length=500)
    ttl_seconds: Optional[int] = Field(default=None)

    # Spending controls
    daily_limit: Optional[Decimal] = Field(default=None, decimal_places=8)
    daily_spent: Decimal = Field(default=Decimal("0"), decimal_places=8)
    daily_reset_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    # Velocity monitoring (spend per hour for anomaly detection)
    hourly_limit: Optional[Decimal] = Field(default=None, decimal_places=8)
    hourly_spent: Decimal = Field(default=Decimal("0"), decimal_places=8)
    hourly_reset_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    # Velocity anomaly detection
    last_charge_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    velocity_alerts_triggered: int = Field(default=0)

    # Auto-refill settings
    auto_refill: bool = Field(default=False)
    auto_refill_threshold: Decimal = Field(default=Decimal("100"), decimal_places=8)
    auto_refill_amount: Decimal = Field(default=Decimal("1000"), decimal_places=8)

    # Status and metadata
    status: str = Field(default="active", max_length=20)
    kyc_status: str = Field(default="not_required", max_length=30)
    kyc_verified_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    metadata_json: Optional[str] = Field(default=None)

    # Timestamps
    created_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    updated_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)

    model_config = {"arbitrary_types_allowed": True}


class LedgerEntryModel(SQLModel, table=True):
    """
    Ledger entry table for immutable transaction history.

    All amounts are Decimal. Positive = credit, negative = debit.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "wallet_id",
            "operation_key",
            name="uq_ledger_wallet_operation_key",
        ),
    )

    entry_id: str = Field(primary_key=True, max_length=50)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)

    # Transaction details
    action: str = Field(max_length=20)
    amount: Decimal = Field(decimal_places=8)  # Positive = credit, negative = debit
    # May be negative only when a provider refund records an unfunded sponsor
    # liability; the associated wallet is frozen in the same transaction.
    balance_after: Decimal = Field(decimal_places=8)

    # Service categorization
    service_category: Optional[str] = Field(default=None, max_length=50, index=True)

    # Description and context
    description: str = Field(default="", max_length=500)
    request_path: Optional[str] = Field(default=None, max_length=255)

    # Cost accounting for arbitrage
    compute_cost: Optional[Decimal] = Field(default=None, decimal_places=8)
    margin: Optional[Decimal] = Field(default=None, decimal_places=8)

    # Metadata
    metadata_json: Optional[str] = Field(default=None)
    correlation_id: Optional[str] = Field(default=None, max_length=100, index=True)
    # Wallet-scoped idempotency identity for governed financial operations.
    # NULL preserves legacy/non-governed ledger behavior.
    operation_key: Optional[str] = Field(default=None, max_length=128, index=True)

    # Stripe payment fields (for fiat top-up idempotency)
    payment_intent_id: Optional[str] = Field(
        default=None,
        max_length=100,
        index=True,
        unique=True,  # Enforces idempotency at DB level
    )
    stripe_session_id: Optional[str] = Field(default=None, max_length=100, index=True)
    # Stripe webhook event id, set on refund entries. UNIQUE so a redelivered
    # charge.refunded event (common on transient timeouts) cannot debit the
    # wallet twice -- the refund path can't reuse payment_intent_id because the
    # original mint entry already holds it under that unique constraint.
    stripe_event_id: Optional[str] = Field(
        default=None,
        max_length=100,
        index=True,
        unique=True,
    )

    # Timestamp
    timestamp: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class BillingAlertModel(SQLModel, table=True):
    """
    Billing alert table for tracking wallet warnings and notifications.
    """

    __tablename__ = "billing_alerts"

    alert_id: str = Field(primary_key=True, max_length=50)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    alert_type: str = Field(max_length=30, index=True)
    threshold_amount: Optional[Decimal] = Field(default=None, decimal_places=8)
    current_balance: Optional[Decimal] = Field(default=None, decimal_places=8)
    message: str = Field(max_length=500)
    severity: str = Field(default="info", max_length=20)
    acknowledged: bool = Field(default=False)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class DailyBalanceSnapshot(SQLModel, table=True):
    """
    Daily balance snapshots for reporting and auditing.
    """

    __tablename__ = "daily_balance_snapshots"

    snapshot_id: str = Field(primary_key=True, max_length=50)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    date: datetime = Field(sa_type=NaiveUTCDateTime, index=True)
    opening_balance: Decimal = Field(decimal_places=8)
    closing_balance: Decimal = Field(decimal_places=8)
    total_credits: Decimal = Field(default=Decimal("0"), decimal_places=8)
    total_debits: Decimal = Field(default=Decimal("0"), decimal_places=8)
    transaction_count: int = Field(default=0)

    model_config = {"arbitrary_types_allowed": True}


class ServiceRegistryModel(SQLModel, table=True):
    """
    Service registry for the agent marketplace.

    Allows external developers to register billable services that agents
    can discover and pay for using the B2A billing infrastructure.
    """

    __tablename__ = "service_registry"

    service_id: str = Field(primary_key=True, max_length=100)
    name: str = Field(max_length=255)
    description: str = Field(default="", max_length=1000)
    owner_wallet_id: str = Field(
        max_length=50, foreign_key="wallets.wallet_id", index=True
    )
    category: str = Field(max_length=50, index=True)
    credits_per_unit: Decimal = Field(decimal_places=8)
    unit_name: str = Field(default="request", max_length=50)
    mcp_manifest: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    updated_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)

    model_config = {"arbitrary_types_allowed": True}


class KYCVerificationModel(SQLModel, table=True):
    """
    KYC/Identity verification records for sponsor wallets.

    Tracks Stripe Identity verification sessions and their status.
    Only verified wallets can perform fiat top-ups.
    """

    __tablename__ = "kyc_verifications"

    verification_id: str = Field(primary_key=True, max_length=100)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)

    stripe_session_id: str = Field(max_length=100, unique=True, index=True)

    status: str = Field(max_length=30, default="pending")

    verification_type: str = Field(max_length=20, default="identity")

    document_type: Optional[str] = Field(default=None, max_length=30)

    first_verified_at: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None
    )
    last_verified_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    rejection_reason: Optional[str] = Field(default=None, max_length=500)

    metadata_json: Optional[str] = Field(default=None)

    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    updated_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)

    model_config = {"arbitrary_types_allowed": True}


class APIKeyModel(SQLModel, table=True):
    """
    API key registry for wallet authentication and key rotation.

    Supports multiple keys per wallet with rotation tracking.
    Keys can be rotated manually or automatically on suspicious activity.
    """

    __tablename__ = "api_keys"

    key_id: str = Field(primary_key=True, max_length=50)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)

    key_hash: str = Field(max_length=64, index=True)
    key_prefix: str = Field(max_length=8)

    status: str = Field(default="active", max_length=20)

    rotation_count: int = Field(default=0)
    last_rotated_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    last_used_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    last_used_ip: Optional[str] = Field(default=None, max_length=45)

    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    expires_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    # Bounded-use keys: None = unlimited. use_count only advances through the
    # guarded UPDATE in APIKeyService.validate_key so the cap cannot be
    # overshot by concurrent requests.
    max_uses: Optional[int] = Field(default=None)
    use_count: int = Field(default=0)

    revoked_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    revoke_reason: Optional[str] = Field(default=None, max_length=255)

    metadata_json: Optional[str] = Field(default=None)

    model_config = {"arbitrary_types_allowed": True}


class TelemetryEventModel(SQLModel, table=True):
    """
    Time-series telemetry events for the Autonomous PM.

    Supplants the in-memory EventStore. On PostgreSQL with the TimescaleDB
    extension, the Alembic migration converts this into a hypertable keyed
    on ``event_timestamp`` so retention cleanup and time-range queries
    scale to large volumes.

    ``payload_json`` captures the full TelemetryEvent.metadata dict for
    fidelity — any field the application cares to filter on is promoted
    to a dedicated column for indexability.
    """

    __tablename__ = "telemetry_events"

    event_id: str = Field(primary_key=True, max_length=50)
    batch_id: str = Field(max_length=50, index=True)

    # Promoted fields — dedicated columns so filters don't pay a JSON cost.
    event_type: str = Field(max_length=20, index=True)
    severity: str = Field(max_length=20, index=True)
    source: str = Field(max_length=100, index=True)
    message: str = Field(default="", max_length=1000)
    stack_trace: Optional[str] = Field(default=None)

    # Full metadata dict preserved as JSON for anything not promoted.
    payload_json: Optional[str] = Field(default=None)

    # Two timestamps: when the event actually happened (from the client)
    # vs when we stored it (authoritative for retention).
    event_timestamp: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None, index=True
    )
    ingested_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class IoTDeviceModel(SQLModel, table=True):
    """Registered IoT device state for the protocol bridge."""

    __tablename__ = "iot_devices"

    device_id: str = Field(primary_key=True, max_length=100)
    protocol: str = Field(max_length=20, index=True)
    broker_url: Optional[str] = Field(default=None, max_length=500)
    topic_acl_json: Optional[str] = Field(default=None)
    metadata_json: Optional[str] = Field(default=None)
    status: str = Field(default="registered", max_length=30, index=True)
    registered_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    last_message_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    message_count: int = Field(default=0)

    model_config = {"arbitrary_types_allowed": True}


class IoTDeviceEventModel(SQLModel, table=True):
    """Append-only audit event for IoT bridge registry and message activity."""

    __tablename__ = "iot_device_events"

    event_id: str = Field(primary_key=True, max_length=64)
    device_id: str = Field(max_length=100, index=True)
    event_type: str = Field(max_length=30, index=True)
    topic: Optional[str] = Field(default=None, max_length=500)
    payload_json: Optional[str] = Field(default=None)
    timestamp: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class OracleCrawlTargetModel(SQLModel, table=True):
    """Crawl target lifecycle row (pending → crawling → indexed|failed)."""

    __tablename__ = "oracle_crawl_targets"

    target_id: str = Field(primary_key=True, max_length=64)
    url: str = Field(max_length=2048, index=True)
    directory_type: str = Field(max_length=30, index=True)
    status: str = Field(max_length=20, index=True)
    api_id: Optional[str] = Field(default=None, max_length=64, index=True)
    queued_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    crawled_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    raw_payload_hash: Optional[str] = Field(default=None, max_length=128)

    model_config = {"arbitrary_types_allowed": True}


class OracleIndexedAPIModel(SQLModel, table=True):
    """An indexed external API with compatibility metadata."""

    __tablename__ = "oracle_indexed_apis"

    api_id: str = Field(primary_key=True, max_length=64)
    url: str = Field(max_length=2048, index=True)
    name: str = Field(max_length=255)
    description: str = Field(default="", max_length=2000)
    directory_type: str = Field(max_length=30, index=True)
    compatibility_tier: str = Field(max_length=20, index=True)
    compatibility_score: float = Field(default=0.0, index=True)
    capabilities_json: Optional[str] = Field(default=None)
    tags_json: Optional[str] = Field(default=None)
    status: str = Field(max_length=20)
    last_crawled: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class OracleRegistrationModel(SQLModel, table=True):
    """One row per attempt to register our API in an external directory."""

    __tablename__ = "oracle_registrations"

    registration_id: str = Field(primary_key=True, max_length=64)
    directory_url: str = Field(max_length=2048, index=True)
    directory_type: str = Field(max_length=30, index=True)
    status: str = Field(max_length=20, index=True)
    message: str = Field(default="", max_length=2000)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class OracleDiscoveryHitModel(SQLModel, table=True):
    """One row per inbound discovery hit for top-referrer aggregation."""

    __tablename__ = "oracle_discovery_hits"

    hit_id: str = Field(primary_key=True, max_length=64)
    referrer: str = Field(default="direct", max_length=2048, index=True)
    timestamp: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class AgentCommsMessageModel(SQLModel, table=True):
    """Durable agent-to-agent message row when SIMULATION_MODE_AGENT_COMMS is false."""

    __tablename__ = "agent_comms_messages"

    message_id: str = Field(primary_key=True, max_length=64)
    from_agent: str = Field(max_length=100, index=True)
    to_agent: str = Field(max_length=100, index=True)
    message_type: str = Field(max_length=30)
    priority: str = Field(max_length=20)
    subject: str = Field(default="", max_length=500)
    body_json: Optional[str] = Field(default=None)
    correlation_id: Optional[str] = Field(default=None, max_length=100, index=True)
    reply_to: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(max_length=20, index=True)
    payload_hash: Optional[str] = Field(default=None, max_length=128)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    delivered_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    model_config = {"arbitrary_types_allowed": True}


class SecurityScanModel(SQLModel, table=True):
    """Shared scan table for red_team (internal) and rtaas (external)."""

    __tablename__ = "security_scans"

    scan_id: str = Field(primary_key=True, max_length=64)
    scan_type: str = Field(max_length=20, index=True)
    tenant_id: Optional[str] = Field(default=None, max_length=100, index=True)

    targets_json: Optional[str] = Field(default=None)
    attack_categories_json: Optional[str] = Field(default=None)
    intensity: str = Field(default="standard", max_length=20)

    status: str = Field(max_length=20, index=True)
    total_tests_run: int = Field(default=0)
    total_passed: Optional[int] = Field(default=None)
    total_failed: Optional[int] = Field(default=None)
    security_score: float = Field(default=0.0)

    recommendations_json: Optional[str] = Field(default=None)

    started_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    completed_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class SecurityVulnerabilityModel(SQLModel, table=True):
    """Shared vulnerability rows for both scan types."""

    __tablename__ = "security_vulnerabilities"

    vuln_id: str = Field(primary_key=True, max_length=64)
    scan_id: str = Field(
        max_length=64,
        foreign_key="security_scans.scan_id",
        index=True,
    )

    category: str = Field(max_length=50, index=True)
    severity: str = Field(max_length=20, index=True)
    title: str = Field(max_length=500)
    description: str = Field(default="", max_length=4000)

    endpoint: str = Field(max_length=2048)
    method: Optional[str] = Field(default=None, max_length=10)

    evidence_json: Optional[str] = Field(default=None)
    remediation: str = Field(default="", max_length=4000)
    remediation_status: str = Field(default="open", max_length=30)
    cwe_id: Optional[str] = Field(default=None, max_length=20)

    discovered_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class ContentPipelineModel(SQLModel, table=True):
    """Content factory pipeline instance."""

    __tablename__ = "content_pipelines"

    pipeline_id: str = Field(primary_key=True, max_length=64)
    title: str = Field(max_length=500)
    source_clip_id: Optional[str] = Field(default=None, max_length=100)
    source_url: Optional[str] = Field(default=None, max_length=2048)
    target_formats_json: Optional[str] = Field(default=None)
    brand_config_json: Optional[str] = Field(default=None)
    language: str = Field(default="en", max_length=10)
    auto_schedule: bool = Field(default=True)
    owner_key: str = Field(default="", max_length=255, index=True)
    status: str = Field(default="queued", max_length=30, index=True)
    hook_json: Optional[str] = Field(default=None)
    caption_style: str = Field(default="bold_impact", max_length=30)
    aspect_ratio: str = Field(default="9:16", max_length=10)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class ContentPieceModel(SQLModel, table=True):
    """One generated content piece. download_url holds the blob ref."""

    __tablename__ = "content_pieces"

    content_id: str = Field(primary_key=True, max_length=64)
    pipeline_id: str = Field(
        max_length=64,
        foreign_key="content_pipelines.pipeline_id",
        index=True,
    )
    format: str = Field(max_length=30, index=True)
    title: str = Field(max_length=500)
    description: Optional[str] = Field(default=None, max_length=2000)
    download_url: str = Field(max_length=2048)
    thumbnail_url: Optional[str] = Field(default=None, max_length=2048)
    duration_seconds: Optional[float] = Field(default=None)
    dimensions: Optional[str] = Field(default=None, max_length=30)
    file_size_bytes: Optional[int] = Field(default=None)
    status: str = Field(max_length=20, index=True)
    metadata_json: Optional[str] = Field(default=None)
    generated_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class ContentCampaignModel(SQLModel, table=True):
    """Top-level live campaign linking many pipelines + hooks."""

    __tablename__ = "content_campaigns"

    campaign_id: str = Field(primary_key=True, max_length=64)
    campaign_title: str = Field(max_length=500)
    source_url: str = Field(max_length=2048)
    hooks_json: Optional[str] = Field(default=None)
    pipeline_ids_json: Optional[str] = Field(default=None)
    status: str = Field(default="running", max_length=30, index=True)
    owner_key: str = Field(default="", max_length=255, index=True)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class ContentScheduleModel(SQLModel, table=True):
    """Persisted scheduler recommendations."""

    __tablename__ = "content_schedules"

    schedule_id: str = Field(primary_key=True, max_length=64)
    content_id: str = Field(
        max_length=64,
        foreign_key="content_pieces.content_id",
        index=True,
    )
    platform: str = Field(max_length=50, index=True)
    recommended_time: datetime = Field(sa_type=NaiveUTCDateTime, index=True)
    confidence: float = Field(default=0.0)
    reasoning: str = Field(default="", max_length=2000)
    estimated_views: Optional[int] = Field(default=None)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class ContentFactoryGenerationModel(SQLModel, table=True):
    """LLM text generation row when ``SIMULATION_MODE_CONTENT_FACTORY`` is false."""

    __tablename__ = "content_factory_generations"

    content_id: str = Field(primary_key=True, max_length=36)
    prompt_hash: Optional[str] = Field(default=None, max_length=64, index=True)
    output_hash: Optional[str] = Field(default=None, max_length=64)
    model: Optional[str] = Field(default=None, max_length=128)
    provenance_json: Optional[str] = Field(default=None)
    output_text: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    updated_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    model_config = {"arbitrary_types_allowed": True}


class KeyRotationLogModel(SQLModel, table=True):
    """
    Audit log for API key rotations.

    Tracks rotation history for security auditing and compliance.
    """

    __tablename__ = "key_rotation_logs"

    log_id: str = Field(primary_key=True, max_length=50)
    key_id: str = Field(max_length=50, index=True)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)

    rotation_type: str = Field(max_length=30)

    old_key_id: Optional[str] = Field(default=None, max_length=50)
    new_key_id: Optional[str] = Field(default=None, max_length=50)

    trigger_reason: str = Field(max_length=255)
    triggered_by: str = Field(max_length=50)

    ip_address: Optional[str] = Field(default=None, max_length=45)
    user_agent: Optional[str] = Field(default=None, max_length=500)

    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class ControlPlaneAuditEventModel(SQLModel, table=True):
    """Durable control-plane audit event for agent operations."""

    __tablename__ = "control_plane_audit_events"

    event_id: str = Field(primary_key=True, max_length=50)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    # Per-wallet monotonic sequence: a stable ordering key for the hash chain so
    # equal/clock-skewed created_at values can't reorder the chain on read.
    seq: int = Field(default=0, index=True)
    event: str = Field(max_length=128, index=True)
    wallet_id: Optional[str] = Field(default=None, max_length=64, index=True)
    tool: Optional[str] = Field(default=None, max_length=128, index=True)
    endpoint: Optional[str] = Field(default=None, max_length=256, index=True)
    auth_source: Optional[str] = Field(default=None, max_length=32)
    key_id: Optional[str] = Field(default=None, max_length=64, index=True)
    policy_decision_id: Optional[str] = Field(default=None, max_length=64, index=True)
    request_id: Optional[str] = Field(default=None, max_length=100, index=True)
    ok: bool = Field(default=True, index=True)
    error: Optional[str] = Field(default=None)
    metadata_json: Optional[str] = Field(default=None)
    payload_hash: Optional[str] = Field(default=None, max_length=64, index=True)
    previous_hash: Optional[str] = Field(default=None, max_length=64)
    chain_hash: Optional[str] = Field(default=None, max_length=64, index=True)
    signature: Optional[str] = Field(default=None)
    signature_key_id: Optional[str] = Field(
        default=None,
        max_length=64,
        foreign_key="signing_keys.key_id",
        index=True,
    )


class AuditChainHeadModel(SQLModel, table=True):
    """Per-wallet head pointer for the audit hash chain.

    A single row per wallet serializes concurrent writers so they cannot read
    the same predecessor and fork the chain. Serialization is optimistic, not
    lock-based: an append reads ``last_seq``/``last_chain_hash``, then advances
    the head with a conditional ``UPDATE ... WHERE last_seq = <observed>``. A
    writer whose update matches no rows lost the race and retries against the
    new head. This avoids ``SELECT ... FOR UPDATE`` so the behaviour is
    identical on SQLite and Postgres; see ``append_chained_audit_event`` in
    ``app/services/audit_chain.py``. ``wallet_key`` is the wallet id, or "" for
    wallet-less events.
    """

    __tablename__ = "audit_chain_heads"

    wallet_key: str = Field(primary_key=True, max_length=64)
    last_chain_hash: Optional[str] = Field(default=None, max_length=64)
    last_seq: int = Field(default=0)
    updated_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)


class SigningKeyModel(SQLModel, table=True):
    """Public signing-key metadata for permits, receipts, and audit events."""

    __tablename__ = "signing_keys"

    key_id: str = Field(primary_key=True, max_length=64)
    alg: str = Field(default="Ed25519", max_length=20)
    public_key_b64: str
    status: str = Field(default="active", max_length=20, index=True)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    activated_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    retired_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)


class PermitModel(SQLModel, table=True):
    """Signed wallet-scoped authority for a governed agent action."""

    __tablename__ = "permits"
    __table_args__ = (
        UniqueConstraint("permit_id", "nonce", name="uq_permits_permit_nonce"),
    )

    permit_id: str = Field(primary_key=True, max_length=64)
    issuer_wallet_id: str = Field(
        max_length=50,
        foreign_key="wallets.wallet_id",
        index=True,
    )
    subject_wallet_id: str = Field(
        max_length=50,
        foreign_key="wallets.wallet_id",
        index=True,
    )
    subject_key_id: Optional[str] = Field(
        default=None,
        max_length=50,
        foreign_key="api_keys.key_id",
        index=True,
    )
    scopes_json: str
    allowed_tools_json: str
    max_credits: Decimal = Field(decimal_places=8)
    spent_credits: Decimal = Field(default=Decimal("0"), decimal_places=8)
    expires_at: datetime = Field(sa_type=NaiveUTCDateTime, index=True)
    nonce: str = Field(max_length=64, index=True)
    status: str = Field(default="active", max_length=20, index=True)
    # Governed invokes under this permit block on a human decision (Sentinel)
    # before any budget is reserved or charged. Included in the signed permit
    # payload only when true, so pre-existing permit signatures stay valid.
    requires_human_approval: bool = Field(default=False)
    signature: str
    key_id: str = Field(max_length=64, foreign_key="signing_keys.key_id", index=True)
    issued_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    revoked_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    # Last time budget was reserved/released; used to distinguish a live
    # in-flight reservation from one orphaned by a crash during reconciliation.
    updated_at: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None, index=True
    )
    # Permit schema v2 constraints — all optional for backwards compatibility
    max_calls_per_tool_json: Optional[str] = Field(default=None)
    aggregate_value_cap: Optional[Decimal] = Field(default=None, decimal_places=8)
    forbidden_fields_json: Optional[str] = Field(default=None)
    recipient_domain: Optional[str] = Field(default=None, max_length=255)
    # Opt-out from cross-key duplicate detection. When true, identical requests
    # under different idempotency keys are allowed.
    allow_identical_repeats: bool = Field(default=False)
    # Nullable preserves the signed payload of permits issued before schema 040.
    repeat_window_seconds: Optional[int] = Field(default=None)
    action_contract_version: Optional[int] = Field(default=None)
    action_payload_hash: Optional[str] = Field(default=None)
    action_schema_id: Optional[str] = Field(default=None)
    action_schema_version: Optional[str] = Field(default=None)
    action_public_tool_id: Optional[str] = Field(default=None)
    action_upstream_binding_hash: Optional[str] = Field(default=None)
    # Atomic call-count tracking for local max_calls_per_tool enforcement.
    # Maps tool name -> reserved local call count (e.g., {"partner.echo": 2}).
    tool_call_counts_json: Optional[str] = Field(default=None)

    model_config = {"arbitrary_types_allowed": True}


class ReceiptModel(SQLModel, table=True):
    """Signed receipt for a governed MCP invocation attempt."""

    __tablename__ = "receipts"

    receipt_id: str = Field(primary_key=True, max_length=64)
    idempotency_record_id: Optional[str] = Field(
        default=None,
        max_length=64,
        foreign_key="idempotency_records.record_id",
        index=True,
        unique=True,
    )
    dispatch_attempt_id: Optional[str] = Field(
        default=None,
        max_length=64,
        foreign_key="mcp_dispatch_attempts.attempt_id",
        index=True,
        unique=True,
    )
    permit_id: str = Field(max_length=64, foreign_key="permits.permit_id", index=True)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    key_id: Optional[str] = Field(default=None, max_length=50, index=True)
    tool: str = Field(max_length=128, index=True)
    request_hash: str = Field(max_length=64, index=True)
    response_hash: Optional[str] = Field(default=None, max_length=64)
    ledger_entry_id: Optional[str] = Field(
        default=None,
        max_length=50,
        foreign_key="ledger_entries.entry_id",
        index=True,
    )
    credits_authorized: Decimal = Field(decimal_places=8)
    credits_charged: Decimal = Field(default=Decimal("0"), decimal_places=8)
    outcome: str = Field(max_length=32, index=True)
    # Stable machine-readable reason for a governed non-success outcome.
    # Signed only when present so receipts created before migration 032 keep
    # verifying byte-for-byte.
    reason_code: Optional[str] = Field(default=None, max_length=128)
    audit_event_id: Optional[str] = Field(
        default=None,
        max_length=50,
        foreign_key="control_plane_audit_events.event_id",
        index=True,
    )
    # Human approval that authorized this invoke, when the permit required one.
    # Included in the signed receipt payload only when set, so pre-existing
    # receipt signatures stay valid. Plain string (no FK) — the column is
    # ALTER-added and SQLite cannot add FK constraints via ALTER (see 020).
    approval_id: Optional[str] = Field(default=None, max_length=64, index=True)
    # Permit schema v2: snapshot of constraints that were evaluated at invoke time
    constraints_evaluated_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    signature: str
    signature_key_id: str = Field(
        max_length=64,
        foreign_key="signing_keys.key_id",
        index=True,
    )

    action_contract_version: Optional[int] = Field(default=None)
    action_payload_hash: Optional[str] = Field(default=None)
    action_schema_id: Optional[str] = Field(default=None)
    action_schema_version: Optional[str] = Field(default=None)
    action_public_tool_id: Optional[str] = Field(default=None)
    action_upstream_binding_hash: Optional[str] = Field(default=None)

    model_config = {"arbitrary_types_allowed": True}


class HumanApprovalModel(SQLModel, table=True):
    """Human decision gating one governed invoke (Sentinel-backed).

    One row per (wallet, permit, tool, idempotency_key) — the idempotency key
    names the invoke attempt, so retries with the same key re-check the same
    approval instead of paging a human again. Sentinel never expires an
    approval server-side (a timed-out approval stays "pending" there), so
    ``expires_at`` is enforced here.
    """

    __tablename__ = "human_approvals"
    __table_args__ = (
        UniqueConstraint(
            "wallet_id",
            "permit_id",
            "tool",
            "idempotency_key",
            name="uq_human_approvals_invoke",
        ),
    )

    approval_id: str = Field(primary_key=True, max_length=64)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    permit_id: str = Field(max_length=64, foreign_key="permits.permit_id", index=True)
    tool: str = Field(max_length=128)
    idempotency_key: str = Field(max_length=128)
    # sha256 of the (tool, arguments, estimated_credits) the human reviewed.
    # Binds the approval to the exact call, so a retry reusing the idempotency
    # key with different arguments or price cannot ride this approval.
    request_hash: Optional[str] = Field(default=None, max_length=64)
    # pending | approved | rejected | expired | consumed
    status: str = Field(default="pending", max_length=16, index=True)
    simulated: bool = Field(default=False)
    # Sentinel action id (act_<hex>); None for simulated approvals.
    sentinel_action_id: Optional[str] = Field(default=None, max_length=64, index=True)
    requested_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    expires_at: datetime = Field(sa_type=NaiveUTCDateTime)
    decided_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    decided_by: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None)

    model_config = {"arbitrary_types_allowed": True}


class PermitRequestModel(SQLModel, table=True):
    """An agent's request for authority it cannot mint itself.

    The subject agent asks the issuer for a scoped, budgeted permit; a human
    approves or rejects in Sentinel; the middleware mints the permit from the
    terms stored here — never from the polling request — and the agent polls
    until ``permit_id`` appears.

    ``reserved_permit_id`` is allocated at request time and used as the minted
    permit's primary key, which makes minting idempotent: a mint retried after
    a crash collides on the permits primary key instead of issuing a second
    permit carrying the same authority.
    """

    __tablename__ = "permit_requests"
    __table_args__ = (
        UniqueConstraint(
            "subject_wallet_id",
            "idempotency_key",
            name="uq_permit_requests_idempotency",
        ),
    )

    request_id: str = Field(primary_key=True, max_length=64)
    issuer_wallet_id: str = Field(
        max_length=50, foreign_key="wallets.wallet_id", index=True
    )
    subject_wallet_id: str = Field(
        max_length=50, foreign_key="wallets.wallet_id", index=True
    )
    subject_key_id: Optional[str] = Field(default=None, max_length=50, index=True)
    idempotency_key: str = Field(max_length=128)
    # The requested permit terms, frozen at request time.
    scopes_json: str
    allowed_tools_json: str
    max_credits: Decimal = Field(decimal_places=8)
    permit_expires_at: datetime = Field(sa_type=NaiveUTCDateTime)
    requires_human_approval: bool = Field(default=False)
    justification: str = Field(sa_column=Column(Text, nullable=False))
    # sha256 of the terms above — what the human actually reviewed.
    request_hash: str = Field(max_length=64)
    # Immutable anchor of the original request_hash, set at create time.
    # Minting verifies request_hash == original_request_hash to prevent an
    # attacker from coherently tampering with both the terms and the hash.
    original_request_hash: str = Field(max_length=64)
    # pending | minting | approved | rejected | expired | failed
    status: str = Field(default="pending", max_length=16, index=True)
    simulated: bool = Field(default=False)
    sentinel_action_id: Optional[str] = Field(default=None, max_length=64, index=True)
    approval_url: Optional[str] = Field(default=None, max_length=512)
    reserved_permit_id: str = Field(max_length=64)
    # Set once the permit exists. No FK: the column is written in the same
    # transaction shape as receipts.approval_id (see migration 023).
    permit_id: Optional[str] = Field(default=None, max_length=64, index=True)
    requested_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    # Local decision deadline; Sentinel never expires an approval server-side.
    expires_at: datetime = Field(sa_type=NaiveUTCDateTime)
    decided_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    decided_by: Optional[str] = Field(default=None, max_length=128)
    reason: Optional[str] = Field(default=None)
    # When the single mint claim was taken; recovers a mint interrupted by a
    # crash without letting two workers mint concurrently.
    mint_started_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)

    model_config = {"arbitrary_types_allowed": True}


class QuoteModel(SQLModel, table=True):
    """A signed, single-use price commitment for one tool call.

    An agent asks what a call will cost and gets back a signed quote. Presenting
    it on the governed invoke charges the quoted credits even if the tool's
    registered price has moved since — the lock is the product. Single use and
    short-lived, so the exposure is one call inside the quoted window rather
    than an open-ended right to the old price.
    """

    __tablename__ = "quotes"

    quote_id: str = Field(primary_key=True, max_length=64)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    tool: str = Field(max_length=128, index=True)
    # Price in credits, fixed at issue time. What the charge will use.
    quoted_credits: Decimal = Field(decimal_places=8)
    # Category the price was derived from, kept for drift diagnosis.
    category: str = Field(max_length=50)
    # active | consumed | expired
    status: str = Field(default="active", max_length=16, index=True)
    issued_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    expires_at: datetime = Field(sa_type=NaiveUTCDateTime, index=True)
    consumed_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    # Set when the quote is spent, so a receipt can be traced back to the
    # commitment that priced it. No FK: written after the invoke completes.
    consumed_by_idempotency_key: Optional[str] = Field(default=None, max_length=128)
    signature: str
    key_id: str = Field(max_length=64, foreign_key="signing_keys.key_id", index=True)

    model_config = {"arbitrary_types_allowed": True}


class IdempotencyRecordModel(SQLModel, table=True):
    """Wallet-scoped replay protection for state-changing trust endpoints."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "wallet_id",
            "endpoint",
            "idempotency_key",
            name="uq_idempotency_wallet_endpoint_key",
        ),
        # Pre-023 workers keyed governed MCP calls by their physical transport
        # endpoint. Current workers use /mcp/invoke. Keep both generations in
        # one database identity so a rolling deployment cannot create one row
        # (and therefore one debit/dispatch) under each endpoint spelling.
        Index(
            "uq_idempotency_governed_mcp_identity",
            "wallet_id",
            text(
                "(CASE WHEN endpoint = '/mcp/invoke' "
                "OR endpoint = '/mcp/messages' "
                "OR endpoint LIKE '/mcp/tools/%/invoke' "
                "THEN '/mcp/invoke' ELSE endpoint END)"
            ),
            "idempotency_key",
            unique=True,
        ),
    )

    record_id: str = Field(primary_key=True, max_length=64)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    endpoint: str = Field(max_length=256, index=True)
    idempotency_key: str = Field(max_length=128, index=True)
    request_hash: str = Field(max_length=64)
    # Internal execution class used only for safe crash reconciliation. It is
    # not part of the public idempotency identity or response contract.
    operation_kind: Optional[str] = Field(default=None, max_length=32, index=True)
    response_reference: Optional[str] = Field(default=None, max_length=128)
    response_json: Optional[str] = Field(default=None)
    status_code: int = Field(default=200)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    expires_at: Optional[datetime] = Field(sa_type=NaiveUTCDateTime, default=None)
    # Set right after a governed invoke charges a wallet, before the
    # receipt/audit/complete finalization sequence runs. Lets a reconciliation
    # sweep tell "never charged" apart from "charged but never finalized".
    ledger_entry_id: Optional[str] = Field(default=None, max_length=64, index=True)


class McpDispatchAttemptModel(SQLModel, table=True):
    """Durable state for one governed dispatch to an upstream MCP server."""

    __tablename__ = "mcp_dispatch_attempts"

    attempt_id: str = Field(primary_key=True, max_length=64)
    idempotency_record_id: str = Field(
        max_length=64,
        foreign_key="idempotency_records.record_id",
        index=True,
        unique=True,
    )
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    permit_id: str = Field(max_length=64, foreign_key="permits.permit_id", index=True)
    approval_id: Optional[str] = Field(
        default=None,
        max_length=64,
        foreign_key="human_approvals.approval_id",
        index=True,
    )
    key_id: Optional[str] = Field(
        default=None,
        max_length=50,
        foreign_key="api_keys.key_id",
        index=True,
    )
    public_tool_id: str = Field(max_length=128, index=True)
    upstream_tool_name: str = Field(max_length=128)
    # Scheme + host + optional port only. Credentials, paths, query strings,
    # and fragments are deliberately excluded by the upstream adapter.
    upstream_origin: str = Field(max_length=512)
    request_hash: str = Field(max_length=64, index=True)
    ledger_entry_id: Optional[str] = Field(
        default=None,
        max_length=50,
        foreign_key="ledger_entries.entry_id",
        index=True,
    )
    credits_authorized: Decimal = Field(decimal_places=8)
    credits_charged: Decimal = Field(default=Decimal("0"), decimal_places=8)
    state: str = Field(default="prepared", max_length=32, index=True)
    # Hash of the process-local dispatch claim. The raw claim is never stored.
    # NULL preserves attempts created before claim fencing was introduced.
    dispatch_claim_hash: Optional[str] = Field(default=None, max_length=64)
    # Tracks whether this attempt holds a per-tool call slot from the permit's
    # max_calls_per_tool cap. The slot is reserved atomically with budget at
    # preparation and released only when compensated before dispatch.
    call_slot_reserved: bool = Field(default=False)
    # Canonical JSON is capped by McpDispatchAttemptService before persistence.
    result_json: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    result_size_bytes: Optional[int] = Field(default=None)
    response_hash: Optional[str] = Field(default=None, max_length=64)
    error_code: Optional[str] = Field(default=None, max_length=64, index=True)
    # Compensation checkpoints make reconciliation retry-safe. A ledger
    # refund is independently idempotent by correlation_id; permit budget
    # release is not, so that release and this marker are committed together.
    debit_refunded_at: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None, index=True
    )
    budget_released_at: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None, index=True
    )
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    updated_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    dispatched_at: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None, index=True
    )
    completed_at: Optional[datetime] = Field(
        sa_type=NaiveUTCDateTime, default=None, index=True
    )

    model_config = {"arbitrary_types_allowed": True}


class RefreshTokenModel(SQLModel, table=True):
    """Refresh token registry for JWT revocation."""

    __tablename__ = "refresh_tokens"

    jti: str = Field(primary_key=True, max_length=64)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    # The API key this token chain was minted from. Soft reference (no FK): a
    # revoked key stays in the table, and a missing key must fail closed rather
    # than error. NULL means "issued before binding existed" — see migration 025.
    key_id: Optional[str] = Field(default=None, max_length=50, index=True)
    revoked: bool = Field(default=False, index=True)
    created_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)
    expires_at: datetime = Field(sa_type=NaiveUTCDateTime)

    model_config = {"arbitrary_types_allowed": True}


class PolicyBundleModel(SQLModel, table=True):
    """Wallet-scoped execution policy bundle."""

    __tablename__ = "policy_bundles"

    policy_id: str = Field(primary_key=True, max_length=64)
    wallet_id: str = Field(max_length=50, foreign_key="wallets.wallet_id", index=True)
    name: str = Field(max_length=255)
    allowed_tools_json: Optional[str] = Field(default=None)
    allowed_service_categories_json: Optional[str] = Field(default=None)
    max_cost_per_action: Optional[Decimal] = Field(default=None, decimal_places=8)
    daily_spend_limit: Optional[Decimal] = Field(default=None, decimal_places=8)
    require_real_effects: bool = Field(default=False)
    risk_tier: str = Field(default="medium", max_length=20)
    human_approval_required: bool = Field(default=False)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(
        sa_type=NaiveUTCDateTime, default_factory=utc_now, index=True
    )
    updated_at: datetime = Field(sa_type=NaiveUTCDateTime, default_factory=utc_now)

    model_config = {"arbitrary_types_allowed": True}
