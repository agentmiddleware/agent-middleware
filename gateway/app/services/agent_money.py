"""Agent financial gateways compatibility facade.

AgentMoney remains the stable public API while wallet lifecycle and billing
implementation live in focused internal engines.
"""

import json
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.durable_state import get_durable_state
from ..db.database import get_session_factory
from ..db.models import WalletModel
from ..schemas.billing import (
    ArbitrageReport,
    BillingAlert,
    InsufficientFundsResponse,
    LedgerEntry,
    ServiceCategory,
    ServicePricing,
    ServiceRegistration,
    TopUpResponse,
    WalletResponse,
)
from .billing_engine import BillingEngine

# DEFAULT_PRICING lives in .pricing so the governed price path, this facade,
# and the dry-run shadow ledger all read one table. Re-exported here because
# callers already import it from this module.
from .pricing import DEFAULT_PRICING
from .shadow_ledger import SimulatedChargeResult
from .wallet_engine import WalletEngine

settings = get_settings()

# ---------------------------------------------------------------------------
# Pricing Table
# ---------------------------------------------------------------------------


# Internal compute costs (what it actually costs us to serve)
COMPUTE_COSTS: dict[ServiceCategory, Decimal] = {
    ServiceCategory.IOT_BRIDGE: Decimal("0.3"),
    ServiceCategory.TELEMETRY_PM: Decimal("0.15"),
    ServiceCategory.MEDIA_ENGINE: Decimal("0.08"),
    ServiceCategory.AGENT_COMMS: Decimal("0.2"),
    ServiceCategory.CONTENT_FACTORY: Decimal("8.0"),
    ServiceCategory.RED_TEAM: Decimal("15.0"),
    ServiceCategory.ORACLE: Decimal("4.0"),
    ServiceCategory.PLATFORM_FEE: Decimal("0.01"),
    ServiceCategory.SWARM_DELEGATION: Decimal("0.5"),
    ServiceCategory.PROTOCOL_GEN: Decimal("30.0"),
    ServiceCategory.SANDBOX: Decimal("25.0"),
    ServiceCategory.RTAAS: Decimal("15.0"),
}


# The fiat → credits exchange rate is NOT defined here. It lives solely on
# settings.EXCHANGE_RATE, which Stripe settlement uses to mint credits. A
# module constant here previously shadowed it, so an operator who set the env
# var moved the rate money actually converted at while /v1/billing/pricing kept
# advertising the hardcoded default. Read the setting at the point of use.


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InsufficientFundsError(Exception):
    """Raised when a wallet has insufficient funds for a transaction."""

    def __init__(self, wallet_id: str, current: Decimal, required: Decimal):
        self.wallet_id = wallet_id
        self.current_balance = current
        self.required_amount = required
        self.shortfall = required - current
        super().__init__(f"Insufficient funds: {current} < {required}")


class WalletNotFoundError(Exception):
    """Raised when a wallet is not found."""

    def __init__(self, wallet_id: str):
        self.wallet_id = wallet_id
        super().__init__(f"Wallet not found: {wallet_id}")


class KYCVerificationRequiredError(Exception):
    """Raised when KYC verification is required but not completed."""

    def __init__(self, wallet_id: str, kyc_status: str):
        self.wallet_id = wallet_id
        self.kyc_status = kyc_status
        super().__init__(
            f"KYC verification required for wallet {wallet_id}. "
            f"Current status: {kyc_status}. Complete verification at /v1/kyc/sessions"
        )


class AgentMoney:
    """Stable facade for wallet lifecycle, billing, metering, and reporting."""

    def __init__(self):
        self._session_factory = get_session_factory
        # Preserve the existing durable-state initialization contract even
        # though the extracted engines currently use the database directly.
        self._state = get_durable_state()
        self._wallet_engine = WalletEngine(
            session_factory=self._session_factory,
            settings=settings,
            wallet_not_found_error=WalletNotFoundError,
            insufficient_funds_error=InsufficientFundsError,
            metadata_to_json=_metadata_to_json,
        )
        self._billing_engine = BillingEngine(
            session_factory=self._session_factory,
            settings=settings,
            wallet_engine=self._wallet_engine,
            default_pricing=DEFAULT_PRICING,
            compute_costs=COMPUTE_COSTS,
            wallet_not_found_error=WalletNotFoundError,
            kyc_required_error=KYCVerificationRequiredError,
        )

    async def _get_session(self) -> AsyncSession:
        """Return a concrete database session."""
        return self._session_factory()()

    async def _lock_wallets_in_order(
        self, session: AsyncSession, wallet_ids: list[str]
    ) -> dict[str, WalletModel]:
        return await self._wallet_engine._lock_wallets_in_order(session, wallet_ids)

    async def create_sponsor_wallet(
        self,
        sponsor_name: str,
        email: str,
        initial_credits: Decimal = Decimal("0"),
        currency: str = "USD",
        metadata: dict | None = None,
        require_kyc: bool | None = None,
        session: AsyncSession | None = None,
    ) -> WalletResponse:
        return await self._wallet_engine.create_sponsor_wallet(
            sponsor_name=sponsor_name,
            email=email,
            initial_credits=initial_credits,
            currency=currency,
            metadata=metadata,
            require_kyc=require_kyc,
            session=session,
        )

    async def create_agent_wallet(
        self,
        sponsor_wallet_id: str,
        agent_id: str,
        budget_credits: Decimal,
        daily_limit: Decimal | None = None,
        auto_refill: bool = False,
        auto_refill_threshold: Decimal = Decimal("100.0"),
        auto_refill_amount: Decimal = Decimal("1000.0"),
        session: AsyncSession | None = None,
    ) -> WalletResponse:
        return await self._wallet_engine.create_agent_wallet(
            sponsor_wallet_id=sponsor_wallet_id,
            agent_id=agent_id,
            budget_credits=budget_credits,
            daily_limit=daily_limit,
            auto_refill=auto_refill,
            auto_refill_threshold=auto_refill_threshold,
            auto_refill_amount=auto_refill_amount,
            session=session,
        )

    async def create_child_wallet(
        self,
        parent_wallet_id: str,
        child_agent_id: str,
        budget_credits: Decimal,
        max_spend: Decimal,
        task_description: str = "",
        ttl_seconds: int | None = None,
        auto_reclaim: bool = True,
    ) -> WalletResponse:
        return await self._wallet_engine.create_child_wallet(
            parent_wallet_id=parent_wallet_id,
            child_agent_id=child_agent_id,
            budget_credits=budget_credits,
            max_spend=max_spend,
            task_description=task_description,
            ttl_seconds=ttl_seconds,
            auto_reclaim=auto_reclaim,
        )

    async def reclaim_child_wallet(self, child_wallet_id: str) -> dict:
        return await self._wallet_engine.reclaim_child_wallet(child_wallet_id)

    async def transfer(
        self,
        from_wallet_id: str,
        to_wallet_id: str,
        amount: Decimal,
        description: str = "",
        correlation_id: str | None = None,
    ) -> dict:
        return await self._wallet_engine.transfer(
            from_wallet_id=from_wallet_id,
            to_wallet_id=to_wallet_id,
            amount=amount,
            description=description,
            correlation_id=correlation_id,
        )

    async def is_wallet_or_descendant(
        self, wallet_id: str, ancestor_wallet_id: str, *, max_depth: int = 32
    ) -> bool:
        """True if ``wallet_id`` is ``ancestor_wallet_id`` or below it in the
        sponsor -> agent -> child hierarchy.

        Used to authorize which wallet a permit may encumber: an issuer may name
        itself or any wallet it funds as the permit subject, but not an
        unrelated wallet. Walks the ``parent_wallet_id`` chain with a depth cap
        so a malformed cycle cannot loop forever.
        """
        if wallet_id == ancestor_wallet_id:
            return True
        async with self._session_factory()() as session:
            current = wallet_id
            seen: set[str] = set()
            for _ in range(max_depth):
                if current in seen:
                    return False
                seen.add(current)
                wallet = await session.get(WalletModel, current)
                if wallet is None or not wallet.parent_wallet_id:
                    return False
                if wallet.parent_wallet_id == ancestor_wallet_id:
                    return True
                current = wallet.parent_wallet_id
        return False

    async def get_swarm_budget(self, parent_wallet_id: str) -> dict:
        return await self._wallet_engine.get_swarm_budget(parent_wallet_id)

    async def _dry_run_charge(
        self,
        wallet_id: str,
        service_category: ServiceCategory,
        units: Decimal,
        charge_amount: Decimal,
        description: str,
        session_id: str | None,
    ) -> SimulatedChargeResult:
        return await self._billing_engine._dry_run_charge(
            wallet_id=wallet_id,
            service_category=service_category,
            units=units,
            charge_amount=charge_amount,
            description=description,
            session_id=session_id,
        )

    async def charge(
        self,
        wallet_id: str,
        service_category: ServiceCategory,
        units: Decimal = Decimal("1"),
        request_path: str | None = None,
        description: str = "",
        dry_run: bool = False,
        dry_run_session_id: str | None = None,
        operation_key: str | None = None,
    ) -> LedgerEntry | InsufficientFundsResponse | SimulatedChargeResult:
        return await self._billing_engine.charge(
            wallet_id=wallet_id,
            service_category=service_category,
            units=units,
            request_path=request_path,
            description=description,
            dry_run=dry_run,
            dry_run_session_id=dry_run_session_id,
            operation_key=operation_key,
        )

    async def refund_charge(
        self,
        *,
        wallet_id: str,
        charge_entry_id: str,
        description: str = "",
    ) -> LedgerEntry:
        return await self._billing_engine.refund_charge(
            wallet_id=wallet_id,
            charge_entry_id=charge_entry_id,
            description=description,
        )

    async def top_up(
        self,
        wallet_id: str,
        amount_fiat: Decimal,
        payment_method: str = "stripe",
        payment_token: str | None = None,
    ) -> TopUpResponse:
        return await self._billing_engine.top_up(
            wallet_id=wallet_id,
            amount_fiat=amount_fiat,
            payment_method=payment_method,
            payment_token=payment_token,
        )

    async def get_arbitrage_report(self) -> ArbitrageReport:
        return await self._billing_engine.get_arbitrage_report()

    def get_pricing_table(self) -> list[ServicePricing]:
        return self._billing_engine.get_pricing_table()

    async def get_alerts(self, wallet_id: str | None = None) -> list[BillingAlert]:
        return await self._billing_engine.get_alerts(wallet_id)

    async def get_wallet(self, wallet_id: str) -> WalletResponse | None:
        return await self._wallet_engine.get_wallet(wallet_id)

    async def wallet_is_expired(self, wallet_id: str) -> bool:
        return await self._wallet_engine.wallet_is_expired(wallet_id)

    async def get_daily_spend(self, wallet_id: str) -> Decimal:
        return await self._wallet_engine.get_daily_spend(wallet_id)

    async def list_wallets(
        self,
        wallet_type: str | None = None,
    ) -> list[WalletResponse]:
        return await self._wallet_engine.list_wallets(wallet_type=wallet_type)

    async def get_ledger(self, wallet_id: str, limit: int = 50) -> list[LedgerEntry]:
        return await self._billing_engine.get_ledger(wallet_id, limit)

    async def register_service(
        self,
        owner_wallet_id: str,
        name: str,
        description: str,
        category: ServiceCategory,
        credits_per_unit: Decimal,
        unit_name: str = "request",
        mcp_manifest: dict | None = None,
    ) -> ServiceRegistration:
        return await self._billing_engine.register_service(
            owner_wallet_id=owner_wallet_id,
            name=name,
            description=description,
            category=category,
            credits_per_unit=credits_per_unit,
            unit_name=unit_name,
            mcp_manifest=mcp_manifest,
        )

    async def list_services(
        self,
        category: ServiceCategory | None = None,
        active_only: bool = True,
    ) -> list[ServiceRegistration]:
        return await self._billing_engine.list_services(
            category=category,
            active_only=active_only,
        )


def _metadata_to_json(metadata: dict | None) -> str | None:
    """Convert metadata dict to JSON string."""
    if not metadata:
        return None
    return json.dumps(metadata, default=str)


_agent_money: AgentMoney | None = None


def get_agent_money() -> AgentMoney:
    """Get or create the AgentMoney singleton."""
    global _agent_money
    if _agent_money is None:
        _agent_money = AgentMoney()
    return _agent_money
