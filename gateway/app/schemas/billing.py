"""
Schemas for Agent Financial Gateways (Service Pillar 8).

Two-tiered wallet system:
- Human Sponsor Layer: Fiat ingestion via Stripe/payment rails → ecosystem credits
- Agent Wallet Layer: Pre-paid, programmatic wallets for autonomous spending

IMPORTANT: Numeric fields are retained for backward compatibility, and response
models also expose *_exact decimal strings for agent-safe reconciliation. The
service layer uses Decimal for all monetary calculations to avoid floating-point
precision errors (e.g., 0.1 + 0.2 ≠ 0.3 with floats).
"""

from decimal import Decimal
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from enum import Enum
from datetime import datetime
import re


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WalletType(str, Enum):
    """Wallet types in the three-tier system."""

    SPONSOR = "sponsor"
    AGENT = "agent"
    CHILD = "child"


class WalletStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    FROZEN = "frozen"
    CLOSED = "closed"
    PENDING_KYC = "pending_kyc"


class LedgerAction(str, Enum):
    CREDIT = "credit"
    DEBIT = "debit"
    TRANSFER = "transfer"
    REFUND = "refund"
    ARBITRAGE_MARGIN = "arbitrage_margin"


class ServiceCategory(str, Enum):
    IOT_BRIDGE = "iot_bridge"
    TELEMETRY_PM = "telemetry_pm"
    MEDIA_ENGINE = "media_engine"
    AGENT_COMMS = "agent_comms"
    CONTENT_FACTORY = "content_factory"
    RED_TEAM = "red_team"
    ORACLE = "oracle"
    PLATFORM_FEE = "platform_fee"
    SWARM_DELEGATION = "swarm_delegation"
    PROTOCOL_GEN = "protocol_gen"
    SANDBOX = "sandbox"
    RTAAS = "rtaas"


class TopUpStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class AlertType(str, Enum):
    LOW_BALANCE = "low_balance"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    BUDGET_EXCEEDED = "budget_exceeded"
    ANOMALOUS_SPEND = "anomalous_spend"
    KYC_PENDING = "kyc_pending"
    KYC_REJECTED = "kyc_rejected"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    # Written by PermitService.record_absorbed_release_drift when a
    # post-effects budget release lost its write and the router absorbed it.
    # Every alert read converts stored rows through this enum, so a stored
    # type missing here fails the whole listing for that wallet.
    PERMIT_RELEASE_CONTENDED = "permit_release_contended"
    # Written by PermitService.reserve_budget when a reservation crosses a
    # budget threshold for its permit; only the highest threshold crossed
    # fires. Absent from this enum, they broke that listing the same way.
    PERMIT_BUDGET_EXHAUSTED = "permit_budget_exhausted"
    PERMIT_BUDGET_90PCT = "permit_budget_90pct"
    PERMIT_BUDGET_80PCT = "permit_budget_80pct"


class KYCStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"


class APIKeyStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


class RotationType(str, Enum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    EMERGENCY = "emergency"
    SCHEDULED = "scheduled"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Wallet Schemas
# ---------------------------------------------------------------------------

SAFE_WALLET_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

# Wallet, ledger and service money columns are Numeric(20, 8): twelve integer
# digits. Float money request fields stay strictly below 1e12 and refuse the
# Infinity/NaN literals Starlette's JSON parser accepts (ge/gt alone let
# +Infinity through), so an unstorable amount is a 422 here rather than an
# Infinity balance and ledger row (SQLite) or a 500 (Postgres).
MAX_STORABLE_AMOUNT = 1e12


def _exact_decimal(value: Any) -> str | None:
    """Return a JSON-safe exact decimal string without binary float math."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(Decimal(str(value)))


class ExactDecimalFieldsMixin(BaseModel):
    """Populate *_exact string fields before legacy float coercion runs."""

    model_config = ConfigDict(from_attributes=True)

    _decimal_exact_fields: ClassVar[dict[str, str]] = {}

    @model_validator(mode="before")
    @classmethod
    def _populate_exact_fields(cls, data: Any) -> Any:
        if not cls._decimal_exact_fields:
            return data

        if isinstance(data, dict):
            payload = dict(data)
        else:
            payload = {
                field_name: getattr(data, field_name)
                for field_name in cls.model_fields
                if hasattr(data, field_name)
            }

        for source_field, exact_field in cls._decimal_exact_fields.items():
            if payload.get(exact_field) is None and source_field in payload:
                payload[exact_field] = _exact_decimal(payload[source_field])

        return payload


class CreateSponsorWalletRequest(BaseModel):
    """Create a human sponsor (liability sink) root account."""

    sponsor_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human or organization name for the liability sink.",
        examples=["Acme Corp Engineering"],
    )
    email: str = Field(
        ...,
        description="Contact email for billing alerts and top-up requests.",
        examples=["billing@acme.com"],
    )
    initial_credits: float = Field(
        default=0.0,
        ge=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Initial credit balance (in ecosystem credits).",
    )
    currency: str = Field(
        default="USD",
        description="Fiat currency for external payment rails.",
    )
    require_kyc: bool | None = Field(
        default=None,
        description=(
            "Require KYC verification before allowing fiat top-ups. "
            "Defaults to system setting."
        ),
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Arbitrary metadata (Stripe customer ID, org info, etc.).",
    )


class CreateAgentWalletRequest(BaseModel):
    """Provision a pre-paid agent wallet under a sponsor."""

    sponsor_wallet_id: str = Field(
        ...,
        description="ID of the sponsor wallet funding this agent.",
    )
    agent_id: str = Field(
        ...,
        description="Agent ID from the comms registry.",
    )
    budget_credits: float = Field(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Credits to provision from the sponsor's balance.",
    )
    daily_limit: float | None = Field(
        default=None,
        ge=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Optional daily spend cap.",
    )
    auto_refill: bool = Field(
        default=False,
        description="Auto-refill from sponsor when balance drops below threshold.",
    )
    auto_refill_threshold: float = Field(
        default=100.0,
        ge=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Refill trigger threshold.",
    )
    auto_refill_amount: float = Field(
        default=1000.0,
        ge=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Amount to refill.",
    )


class CreateChildWalletRequest(BaseModel):
    """Spawn a sub-agent child wallet from an agent wallet."""

    parent_wallet_id: str = Field(
        ...,
        description="ID of the parent agent wallet funding this child.",
    )
    child_agent_id: str = Field(
        ...,
        description="Identifier for the child sub-agent.",
    )
    budget_credits: float = Field(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Credits to provision from parent's balance.",
    )
    max_spend: float = Field(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Hard lifetime spend cap in credits.",
    )
    task_description: str = Field(
        default="",
        description="What this child agent is supposed to accomplish.",
    )
    ttl_seconds: int | None = Field(
        default=None,
        gt=0,
        le=2_147_483_647,
        description="Time-to-live in seconds.",
    )
    auto_reclaim: bool = Field(
        default=True,
        description="Reclaim unspent credits when child completes.",
    )

    @field_validator("ttl_seconds", mode="before")
    @classmethod
    def _reject_boolean_ttl(cls, value: Any) -> Any:
        # bool is an int subclass, so lax coercion would turn true into a
        # 1-second TTL before the service-layer guard ever sees it.
        if isinstance(value, bool):
            raise ValueError("ttl_seconds must be an integer, not a boolean")
        return value


class ChildWalletResponse(ExactDecimalFieldsMixin):
    """Child wallet details with parent lineage."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "balance": "balance_exact",
        "max_spend": "max_spend_exact",
        "spent": "spent_exact",
    }

    wallet_id: str
    wallet_type: WalletType
    parent_wallet_id: str | None = None
    child_agent_id: str | None = None
    balance: float
    balance_exact: str | None = None
    max_spend: float | None = None
    max_spend_exact: str | None = None
    spent: float = 0.0
    spent_exact: str | None = None
    task_description: str = ""
    ttl_seconds: int | None = None
    auto_reclaim: bool = True
    status: WalletStatus
    created_at: datetime


class SwarmBudgetSummary(ExactDecimalFieldsMixin):
    """Hierarchical budget summary for an agent's child swarm."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "parent_balance": "parent_balance_exact",
        "total_delegated": "total_delegated_exact",
        "total_reclaimed": "total_reclaimed_exact",
    }

    parent_wallet_id: str
    parent_balance: float
    parent_balance_exact: str | None = None
    total_delegated: float
    total_delegated_exact: str | None = None
    total_reclaimed: float
    total_reclaimed_exact: str | None = None
    active_children: int
    completed_children: int
    frozen_children: int
    children: list["WalletResponse"]


class ReclaimResponse(ExactDecimalFieldsMixin):
    """Result of reclaiming unspent credits from a child wallet."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "credits_reclaimed": "credits_reclaimed_exact",
        "parent_balance_after": "parent_balance_after_exact",
    }

    child_wallet_id: str
    parent_wallet_id: str
    credits_reclaimed: float
    credits_reclaimed_exact: str | None = None
    parent_balance_after: float
    parent_balance_after_exact: str | None = None
    child_status: WalletStatus


class WalletResponse(ExactDecimalFieldsMixin):
    """Wallet details."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "balance": "balance_exact",
        "lifetime_credits": "lifetime_credits_exact",
        "lifetime_debits": "lifetime_debits_exact",
        "daily_limit": "daily_limit_exact",
        "auto_refill_threshold": "auto_refill_threshold_exact",
        "auto_refill_amount": "auto_refill_amount_exact",
        "max_spend": "max_spend_exact",
    }

    wallet_id: str
    wallet_type: WalletType
    owner_name: str | None = None
    email: str | None = None
    balance: float = Field(..., description="Current credit balance.")
    balance_exact: str | None = Field(
        None, description="Exact current credit balance as a decimal string."
    )
    lifetime_credits: float = Field(..., description="Total credits ever deposited.")
    lifetime_credits_exact: str | None = None
    lifetime_debits: float = Field(..., description="Total credits ever consumed.")
    lifetime_debits_exact: str | None = None
    status: WalletStatus
    kyc_status: KYCStatus = Field(default=KYCStatus.NOT_REQUIRED)
    daily_limit: float | None = None
    daily_limit_exact: str | None = None
    auto_refill: bool = False
    auto_refill_threshold: float | None = None
    auto_refill_threshold_exact: str | None = None
    auto_refill_amount: float | None = None
    auto_refill_amount_exact: str | None = None
    sponsor_wallet_id: str | None = None
    agent_id: str | None = None
    child_agent_id: str | None = None
    max_spend: float | None = None
    max_spend_exact: str | None = None
    task_description: str | None = None
    ttl_seconds: int | None = None
    created_at: datetime
    metadata: dict = Field(default_factory=dict)


class WalletListResponse(BaseModel):
    wallets: list[WalletResponse]
    total: int


# ---------------------------------------------------------------------------
# Ledger Schemas
# ---------------------------------------------------------------------------


class LedgerEntry(ExactDecimalFieldsMixin):
    """A single atomic transaction in the billing ledger."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "amount": "amount_exact",
        "balance_after": "balance_after_exact",
        "compute_cost": "compute_cost_exact",
        "margin": "margin_exact",
    }

    entry_id: str
    wallet_id: str
    action: LedgerAction
    amount: float = Field(
        ...,
        description="Credit amount (positive=credit, negative=debit).",
    )
    amount_exact: str | None = Field(
        None, description="Exact transaction amount as a decimal string."
    )
    balance_after: float = Field(
        ...,
        description="Wallet balance after this transaction.",
    )
    balance_after_exact: str | None = Field(
        None, description="Exact post-transaction balance as a decimal string."
    )
    service_category: ServiceCategory | None = None
    description: str = ""
    request_path: str | None = None
    compute_cost: float | None = None
    compute_cost_exact: str | None = None
    margin: float | None = None
    margin_exact: str | None = None
    timestamp: datetime
    metadata: dict = Field(default_factory=dict)


class LedgerResponse(ExactDecimalFieldsMixin):
    """Paginated ledger entries."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "period_credits": "period_credits_exact",
        "period_debits": "period_debits_exact",
    }

    entries: list[LedgerEntry]
    total: int
    wallet_id: str
    period_credits: float
    period_credits_exact: str | None = None
    period_debits: float
    period_debits_exact: str | None = None


# ---------------------------------------------------------------------------
# Top-Up / Payment Schemas
# ---------------------------------------------------------------------------


class TopUpRequest(BaseModel):
    """Request to add credits to a sponsor wallet via fiat payment."""

    wallet_id: str = Field(..., description="Sponsor wallet to top up.")
    amount_fiat: float = Field(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Amount in fiat currency (e.g., USD).",
    )
    payment_method: str = Field(default="stripe", description="Payment rail to use.")
    payment_token: str | None = Field(default=None, description="Payment method token.")


class TopUpResponse(ExactDecimalFieldsMixin):
    """Result of a top-up request."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "amount_fiat": "amount_fiat_exact",
        "credits_added": "credits_added_exact",
        "exchange_rate": "exchange_rate_exact",
    }

    top_up_id: str
    wallet_id: str
    amount_fiat: float
    amount_fiat_exact: str | None = None
    credits_added: float
    credits_added_exact: str | None = None
    exchange_rate: float
    exchange_rate_exact: str | None = None
    status: TopUpStatus
    payment_url: str | None = None


class InsufficientFundsResponse(ExactDecimalFieldsMixin):
    """Structured 402 Payment Required response."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "current_balance": "current_balance_exact",
        "required_amount": "required_amount_exact",
        "shortfall": "shortfall_exact",
    }

    error: str = "insufficient_funds"
    wallet_id: str
    current_balance: float
    current_balance_exact: str | None = None
    required_amount: float
    required_amount_exact: str | None = None
    shortfall: float
    shortfall_exact: str | None = None
    top_up_url: str
    message: str = "Wallet balance insufficient."


# ---------------------------------------------------------------------------
# Metering & Pricing Schemas
# ---------------------------------------------------------------------------


class ServicePricing(ExactDecimalFieldsMixin):
    """Per-action pricing for a service category."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "credits_per_unit": "credits_per_unit_exact",
    }

    service_category: ServiceCategory
    unit: str
    credits_per_unit: float
    credits_per_unit_exact: str | None = None
    description: str = ""


class PricingTableResponse(ExactDecimalFieldsMixin):
    """Full pricing table for all services."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "exchange_rate": "exchange_rate_exact",
    }

    pricing: list[ServicePricing]
    exchange_rate: float
    exchange_rate_exact: str | None = None
    last_updated: datetime


# ---------------------------------------------------------------------------
# Arbitrage / Margin Schemas
# ---------------------------------------------------------------------------


class ArbitrageReport(ExactDecimalFieldsMixin):
    """Swarm arbitrage profitability report."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "total_revenue": "total_revenue_exact",
        "total_compute_cost": "total_compute_cost_exact",
        "gross_margin": "gross_margin_exact",
        "margin_percentage": "margin_percentage_exact",
    }

    period: str
    total_revenue: float
    total_revenue_exact: str | None = None
    total_compute_cost: float
    total_compute_cost_exact: str | None = None
    gross_margin: float
    gross_margin_exact: str | None = None
    margin_percentage: float
    margin_percentage_exact: str | None = None
    by_service: dict[str, dict] = Field(default_factory=dict)
    top_profitable_actions: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Alert Schemas
# ---------------------------------------------------------------------------


class BillingAlert(ExactDecimalFieldsMixin):
    """Billing alert for sponsors or agents."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "threshold_amount": "threshold_amount_exact",
        "current_balance": "current_balance_exact",
    }

    alert_id: str
    alert_type: AlertType
    wallet_id: str
    message: str
    threshold_amount: float | None = None
    threshold_amount_exact: str | None = None
    current_balance: float | None = None
    current_balance_exact: str | None = None
    severity: AlertSeverity = AlertSeverity.INFO
    acknowledged: bool = False
    created_at: datetime


class AlertListResponse(BaseModel):
    alerts: list[BillingAlert]
    total: int
    unacknowledged: int


class RegisterServiceRequest(BaseModel):
    """Register a new billable service in the marketplace."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    category: ServiceCategory
    credits_per_unit: float = Field(
        ..., gt=0, lt=MAX_STORABLE_AMOUNT, allow_inf_nan=False
    )
    unit_name: str = Field(default="request", max_length=50)
    mcp_manifest: dict | None = None


class ServiceRegistration(ExactDecimalFieldsMixin):
    """A registered billable service in the marketplace."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "credits_per_unit": "credits_per_unit_exact",
    }

    service_id: str
    name: str
    description: str
    owner_wallet_id: str
    category: ServiceCategory
    credits_per_unit: float
    credits_per_unit_exact: str | None = None
    unit_name: str
    mcp_manifest: dict | None = None
    is_active: bool
    created_at: datetime


class TransferResponse(ExactDecimalFieldsMixin):
    """Response for a wallet-to-wallet transfer."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "amount": "amount_exact",
        "from_balance_after": "from_balance_after_exact",
        "to_balance_after": "to_balance_after_exact",
    }

    transfer_id: str
    from_wallet_id: str
    to_wallet_id: str
    amount: float
    amount_exact: str | None = None
    from_balance_after: float
    from_balance_after_exact: str | None = None
    to_balance_after: float
    to_balance_after_exact: str | None = None
    status: str


# Stripe Identity's document allowed_types vocabulary, verbatim. Anything else
# (including the old "document" default and "driver_license") is refused by
# Stripe, so it is refused here first.
KYCDocumentType = Literal["driving_license", "id_card", "passport"]

_KYC_RETURN_URL_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class CreateKYCSessionRequest(BaseModel):
    """Request to create a KYC verification session."""

    wallet_id: str = Field(..., description="Wallet ID requiring KYC verification.")
    return_url: str = Field(
        ...,
        max_length=2048,
        description=(
            "Absolute https URL to redirect to after verification completes "
            "(plain http is accepted only for localhost)."
        ),
        examples=["https://yourapp.com/kyc-callback"],
    )
    document_type: KYCDocumentType | None = Field(
        default=None,
        description=(
            "Restrict verification to one Stripe Identity document type "
            "(driving_license, id_card, passport). Omit to accept any of them."
        ),
    )

    @field_validator("return_url")
    @classmethod
    def _validate_return_url(cls, v: str) -> str:
        """Only a real web origin may receive the post-verification redirect."""
        parts = urlsplit(v)
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("return_url must be an absolute URL with a host")
        if parts.scheme == "https":
            return v
        if parts.scheme == "http" and host in _KYC_RETURN_URL_LOOPBACK_HOSTS:
            return v
        raise ValueError(
            "return_url must use https (plain http is allowed only for localhost)"
        )


class KYCSessionResponse(BaseModel):
    """Response with Stripe Identity session details."""

    verification_id: str
    wallet_id: str
    session_id: str
    session_url: str
    status: KYCStatus
    created_at: datetime
    expires_at: datetime


class KYCStatusResponse(BaseModel):
    """Current KYC verification status for a wallet."""

    wallet_id: str
    kyc_status: KYCStatus
    verification_id: str | None = None
    stripe_session_id: str | None = None
    last_verified_at: datetime | None = None
    rejection_reason: str | None = None
    requires_verification: bool = False
    message: str = ""


class KYCVerificationDetails(BaseModel):
    """Detailed verification information."""

    verification_id: str
    wallet_id: str
    status: KYCStatus
    stripe_session_id: str | None = None
    document_type: str | None = None
    first_verified_at: datetime | None = None
    last_verified_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class CreateAPIKeyRequest(BaseModel):
    """Request to create a new API key for a wallet."""

    wallet_id: str = Field(..., description="Wallet ID to create key for.")
    key_name: str = Field(
        default="default",
        max_length=50,
        description="Human-readable name for this key.",
    )
    expires_in_days: int | None = Field(
        default=None,
        gt=0,
        description="Optional expiration in days. None = no expiration.",
    )
    max_uses: int | None = Field(
        default=None,
        gt=0,
        description="Optional max uses. None = unlimited.",
    )


class APIKeyResponse(BaseModel):
    """API key response with masked key for display."""

    key_id: str
    wallet_id: str
    key_prefix: str
    masked_key: str
    status: APIKeyStatus
    key_name: str = "default"
    rotation_count: int = 0
    last_used_at: datetime | None = None
    created_at: datetime
    expires_at: datetime | None = None
    max_uses: int | None = None
    use_count: int = 0


class APIKeyWithSecret(BaseModel):
    """API key response including the actual key (only shown once)."""

    key_id: str
    wallet_id: str
    api_key: str
    key_prefix: str
    status: APIKeyStatus
    key_name: str = "default"
    created_at: datetime
    expires_at: datetime | None = None
    max_uses: int | None = None
    warning: str = "Store this key securely. It will not be shown again."


class RotateAPIKeyRequest(BaseModel):
    """Request to rotate an API key."""

    wallet_id: str = Field(..., description="Wallet ID owning the key.")
    key_id: str | None = Field(
        default=None,
        description="Specific key ID to rotate. None = create new key only.",
    )
    revoke_old: bool = Field(
        default=False,
        description="Whether to revoke the old key after rotation.",
    )
    reason: str = Field(
        default="manual_rotation",
        max_length=255,
        description="Reason for rotation.",
    )


class RotationResponse(BaseModel):
    """Response after key rotation."""

    rotation_id: str
    wallet_id: str
    old_key_id: str | None = None
    new_key: APIKeyWithSecret | None = None
    rotation_type: RotationType
    revoked_keys: list[str] = []
    created_at: datetime


class APIKeyListResponse(BaseModel):
    """List of API keys for a wallet."""

    wallet_id: str
    keys: list[APIKeyResponse]
    total_active: int
    total_revoked: int


class KeyRotationLogEntry(BaseModel):
    """Audit log entry for key rotation."""

    log_id: str
    key_id: str
    wallet_id: str
    rotation_type: RotationType
    old_key_id: str | None = None
    new_key_id: str | None = None
    trigger_reason: str
    triggered_by: str
    created_at: datetime


class EmergencyKeyRevocationRequest(BaseModel):
    """Request to immediately revoke all keys for a wallet."""

    wallet_id: str = Field(..., description="Wallet ID to revoke keys for.")
    reason: str = Field(
        default="security_incident",
        max_length=255,
        description="Reason for emergency revocation.",
    )
    create_new_key: bool = Field(
        default=True,
        description="Whether to create a new emergency key.",
    )


class SandboxCommitRequest(BaseModel):
    """Request to commit a sandbox session to real billing."""

    session_id: str = Field(..., description="Sandbox session ID to commit.")


class SandboxCommitResponse(ExactDecimalFieldsMixin):
    """Response after committing a sandbox session."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "total_credits_deducted": "total_credits_deducted_exact",
        "real_balance_before": "real_balance_before_exact",
        "real_balance_after": "real_balance_after_exact",
    }

    session_id: str
    wallet_id: str
    committed_charges: int
    total_credits_deducted: float
    total_credits_deducted_exact: str | None = None
    real_balance_before: float
    real_balance_before_exact: str | None = None
    real_balance_after: float
    real_balance_after_exact: str | None = None
    ledger_entries: list[dict]
    success: bool
    message: str


class SandboxRevertResponse(BaseModel):
    """Response after reverting a sandbox session."""

    session_id: str
    wallet_id: str
    reverted: bool
    message: str
