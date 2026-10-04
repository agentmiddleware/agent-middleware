"""
Shadow Ledger — Dry Run Sandbox
================================

Redis-backed shadow ledger for simulating billing operations without
affecting real wallet balances or triggering velocity monitoring.

Architecture:
- Each dry-run session gets a unique session_id
- Simulated charges are tracked in Redis with 15-minute TTL
- Virtual balance is computed from real balance minus simulated charges
- Sessions auto-expire via Redis EXPIRE

Security:
- Dry-run charges are INVISIBLE to VelocityMonitor
- No real ledger entries are created
- No side effects on real wallet state
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import redis.asyncio as redis

from ..core.config import get_settings
from ..schemas.billing import InsufficientFundsResponse, ServiceCategory

# One pricing table, not a second copy. The dry run must quote exactly what
# the real charge path would take, so both read .pricing.
from .pricing import DEFAULT_PRICING

logger = logging.getLogger(__name__)


SESSION_TTL_SECONDS = 900  # 15 minutes
KEY_PREFIX = "dryrun"


@dataclass
class SimulatedCharge:
    """A simulated charge in the shadow ledger."""

    charge_id: str
    service_category: str
    units: float
    credits: Decimal
    description: str
    timestamp: datetime


@dataclass
class DryRunSession:
    """A dry-run session for simulating billing operations."""

    session_id: str
    wallet_id: str
    real_balance: Decimal
    simulated_charges: list[SimulatedCharge] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_simulated(self) -> Decimal:
        """Total credits consumed by simulated charges."""
        return sum((c.credits for c in self.simulated_charges), Decimal("0"))

    @property
    def virtual_balance(self) -> Decimal:
        """Simulated balance after all simulated charges."""
        return self.real_balance - self.total_simulated

    def add_charge(self, charge: SimulatedCharge) -> None:
        """Add a simulated charge to this session."""
        self.simulated_charges.append(charge)


@dataclass
class SimulatedChargeResult:
    """Result of a simulated charge operation."""

    session_id: str
    wallet_id: str
    service_category: str
    units: float
    credits_would_charge: Decimal
    simulated_balance_before: Decimal
    simulated_balance_after: Decimal
    would_succeed: bool
    dry_run: bool = True
    reason: str | None = None
    charge_id: str | None = None


@dataclass
class SessionSummary:
    """Summary of a completed dry-run session."""

    session_id: str
    wallet_id: str
    created_at: datetime
    ended_at: datetime
    total_simulated_credits: Decimal
    simulated_charges: list[dict]
    real_balance: Decimal
    virtual_balance_after: Decimal
    charge_count: int


@dataclass
class CommitResult:
    """Result of committing a sandbox session to real billing."""

    session_id: str
    wallet_id: str
    committed_charges: int
    total_credits_deducted: Decimal
    real_balance_before: Decimal
    real_balance_after: Decimal
    ledger_entries: list[dict]
    success: bool
    message: str


@dataclass
class RevertResult:
    """Result of reverting a sandbox session."""

    session_id: str
    wallet_id: str
    reverted: bool
    message: str


class ShadowLedger:
    """
    Redis-backed shadow ledger for dry-run billing simulations.

    Operations:
    - create_session: Start a new dry-run session
    - simulate_charge: Simulate a charge without affecting real balance
    - get_session: Retrieve current session state
    - end_session: End session and return summary
    """

    def __init__(self, redis_url: str | None = None):
        settings = get_settings()
        self._redis_url = redis_url or settings.REDIS_URL.strip()
        self._redis: redis.Redis | None = None
        self._lock = asyncio.Lock()
        self._warned = False

        self._memory_store: dict[str, dict] = {}

    async def _get_redis(self) -> redis.Redis | None:
        """Get or create Redis connection."""
        if not self._redis_url:
            return None

        if self._redis is not None:
            return self._redis

        async with self._lock:
            if self._redis is not None:
                return self._redis

            try:
                client = redis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
                await client.ping()
                self._redis = client
                logger.info("Shadow ledger connected to Redis")
                return self._redis
            except Exception as e:
                if not self._warned:
                    logger.warning(
                        f"Redis unavailable for shadow ledger: {e}. "
                        "Using in-memory store."
                    )
                    self._warned = True
                return None

    async def _session_key(self, session_id: str) -> str:
        return f"{KEY_PREFIX}:session:{session_id}"

    async def _balance_key(self, session_id: str) -> str:
        return f"{KEY_PREFIX}:balance:{session_id}"

    async def _wallet_sessions_key(self, wallet_id: str) -> str:
        return f"{KEY_PREFIX}:wallet:{wallet_id}:sessions"

    async def create_session(
        self,
        wallet_id: str,
        real_balance: Decimal,
    ) -> DryRunSession:
        """
        Create a new dry-run session.

        Args:
            wallet_id: Wallet being simulated
            real_balance: Current real balance (fetched from AgentMoney)

        Returns:
            DryRunSession with unique session_id
        """
        session_id = str(uuid.uuid4())
        session = DryRunSession(
            session_id=session_id,
            wallet_id=wallet_id,
            real_balance=real_balance,
        )

        redis_client = await self._get_redis()

        if redis_client:
            session_data = {
                "session_id": session_id,
                "wallet_id": wallet_id,
                "real_balance": float(real_balance),
                "simulated_charges": [],
                "created_at": session.created_at.isoformat(),
            }
            await redis_client.setex(
                await self._session_key(session_id),
                SESSION_TTL_SECONDS,
                json.dumps(session_data),
            )
            await redis_client.setex(
                await self._balance_key(session_id),
                SESSION_TTL_SECONDS,
                str(real_balance),
            )
            await redis_client.sadd(
                await self._wallet_sessions_key(wallet_id),
                session_id,
            )
            logger.info(f"Created dry-run session {session_id} for wallet {wallet_id}")
        else:
            self._memory_store[session_id] = {
                "session": session,
                "balance": real_balance,
            }
            logger.info(
                f"Created dry-run session {session_id} "
                f"(in-memory) for wallet {wallet_id}"
            )

        return session

    async def get_session(self, session_id: str) -> DryRunSession | None:
        """Retrieve a dry-run session by ID."""
        redis_client = await self._get_redis()

        if redis_client:
            data = await redis_client.get(await self._session_key(session_id))
            if not data:
                return None

            session_data = json.loads(data)
            return self._deserialize_session(session_data)

        return self._memory_store.get(session_id, {}).get("session")

    async def _claim_session(self, session_id: str) -> DryRunSession | None:
        """Atomically fetch-and-remove a session so exactly one caller can
        commit it.

        Without this, two concurrent commits of the same session both read it
        and both apply every simulated charge to the real wallet -- a
        double-charge. Claiming (GETDEL on Redis, dict.pop in memory) makes the
        commit exclusive: the loser gets ``None`` and does nothing.
        """
        redis_client = await self._get_redis()
        if redis_client:
            raw = await redis_client.getdel(await self._session_key(session_id))
            if not raw:
                return None
            await redis_client.delete(await self._balance_key(session_id))
            session = self._deserialize_session(json.loads(raw))
            await redis_client.srem(
                await self._wallet_sessions_key(session.wallet_id),
                session_id,
            )
            return session
        entry = self._memory_store.pop(session_id, None)
        if not entry:
            return None
        return entry.get("session")

    async def simulate_charge(
        self,
        session_id: str,
        service_category: ServiceCategory,
        units: float = 1.0,
        description: str = "",
        blocked_reason: str | None = None,
    ) -> SimulatedChargeResult:
        """
        Simulate a charge without affecting real balance or velocity.

        Args:
            session_id: The dry-run session
            service_category: Service being simulated
            units: Number of units
            description: Optional description

        Returns:
            SimulatedChargeResult with virtual balance impact
        """
        session = await self.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        pricing = DEFAULT_PRICING.get(service_category)
        if not pricing:
            raise ValueError(f"No pricing for {service_category.value}")

        unit_name, credits_per_unit, _ = pricing
        charge_amount = Decimal(str(units)) * credits_per_unit
        charge_id = str(uuid.uuid4())

        simulated_before = session.virtual_balance
        simulated_after = simulated_before - charge_amount
        would_succeed = blocked_reason is None and simulated_after >= Decimal("0")

        reason = blocked_reason
        if reason is None and not would_succeed:
            reason = "insufficient_simulated_funds"

        result = SimulatedChargeResult(
            dry_run=True,
            session_id=session_id,
            wallet_id=session.wallet_id,
            service_category=service_category.value,
            units=units,
            credits_would_charge=charge_amount,
            # Bug fix: this field is declared Decimal for monetary
            # precision; converting to float here silently discarded
            # precision on every dry-run charge.
            simulated_balance_before=simulated_before,
            simulated_balance_after=simulated_after,
            would_succeed=would_succeed,
            reason=reason,
            charge_id=charge_id,
        )

        if would_succeed:
            charge = SimulatedCharge(
                charge_id=charge_id,
                service_category=service_category.value,
                units=units,
                credits=charge_amount,
                description=description or f"{units} × {unit_name}",
                timestamp=datetime.now(timezone.utc),
            )
            session.add_charge(charge)

            redis_client = await self._get_redis()
            if redis_client:
                session_data = {
                    "session_id": session.session_id,
                    "wallet_id": session.wallet_id,
                    "real_balance": float(session.real_balance),
                    "simulated_charges": [
                        {
                            "charge_id": c.charge_id,
                            "service_category": c.service_category,
                            "units": c.units,
                            "credits": float(c.credits),
                            "description": c.description,
                            "timestamp": c.timestamp.isoformat(),
                        }
                        for c in session.simulated_charges
                    ],
                    "created_at": session.created_at.isoformat(),
                }
                # ``xx=True``: write only if the key still exists. A terminal
                # operation (commit/end/revert) can claim the session between
                # the read above and this write, and an unconditional ``setex``
                # would *recreate* it -- with every simulated charge intact,
                # so it could be committed a second time and the same charges
                # applied to the real wallet twice.
                wrote = await redis_client.set(
                    await self._session_key(session_id),
                    json.dumps(session_data),
                    ex=SESSION_TTL_SECONDS,
                    xx=True,
                )
                if not wrote:
                    raise ValueError(f"Session not found: {session_id}")
            else:
                # Same window, different symptom: the claim popped the entry,
                # so ``self._memory_store[session_id]`` raised ``KeyError`` and
                # escaped the router as a 500. Refuse the same way the Redis
                # path does rather than resurrecting or crashing.
                entry = self._memory_store.get(session_id)
                if entry is None:
                    raise ValueError(f"Session not found: {session_id}")
                entry["session"] = session

            logger.debug(
                f"Simulated charge {charge_id}: {charge_amount} credits "
                f"(balance: {simulated_before} → {simulated_after})"
            )

        return result

    async def end_session(self, session_id: str) -> SessionSummary | None:
        """
        End a dry-run session and return summary.

        Does NOT affect real wallet state - just provides a summary
        of what would have been charged.

        Claims the session rather than reading it and deleting it separately.
        Read-then-delete leaves a window between the two: a concurrent
        ``commit_session`` claims the session and charges the real wallet, this
        method's delete becomes a no-op, and it still returns a summary built
        from the stale read -- so the caller is told the session was ended
        when a commit ended it, and ``revert_session`` reports a revert that
        did not happen. ``_claim_session`` is the same atomic primitive commit
        uses (GETDEL on Redis, ``dict.pop`` in memory), so exactly one of
        end/commit/revert can win.
        """
        session = await self._claim_session(session_id)
        if not session:
            return None

        summary = SessionSummary(
            session_id=session_id,
            wallet_id=session.wallet_id,
            created_at=session.created_at,
            ended_at=datetime.now(timezone.utc),
            total_simulated_credits=session.total_simulated,
            simulated_charges=[
                {
                    "charge_id": c.charge_id,
                    "service_category": c.service_category,
                    "units": c.units,
                    "credits": float(c.credits),
                    "description": c.description,
                }
                for c in session.simulated_charges
            ],
            real_balance=session.real_balance,
            virtual_balance_after=session.virtual_balance,
            charge_count=len(session.simulated_charges),
        )

        logger.info(
            f"Ended dry-run session {session_id}: "
            f"{summary.charge_count} charges, "
            f"{summary.total_simulated_credits} credits simulated"
        )

        return summary

    async def commit_session(
        self,
        session_id: str,
        agent_money,
    ) -> CommitResult:
        """
        Commit a sandbox session to real billing.

        Applies all simulated charges to the real wallet via AgentMoney.
        Uses the charge service for each simulated operation.

        Args:
            session_id: The dry-run session to commit
            agent_money: AgentMoney service instance for billing

        Returns:
            CommitResult with applied charges and new balance
        """
        # Claim the session atomically so a concurrent commit of the same
        # session cannot also charge the wallet. The loser of the race sees no
        # session and returns "Session not found" (the winner already applied
        # the charges).
        session = await self._claim_session(session_id)
        if not session:
            return CommitResult(
                session_id=session_id,
                wallet_id="",
                committed_charges=0,
                total_credits_deducted=Decimal("0"),
                real_balance_before=Decimal("0"),
                real_balance_after=Decimal("0"),
                ledger_entries=[],
                success=False,
                message="Session not found",
            )

        if not session.simulated_charges:
            return CommitResult(
                session_id=session_id,
                wallet_id=session.wallet_id,
                committed_charges=0,
                total_credits_deducted=Decimal("0"),
                real_balance_before=session.real_balance,
                real_balance_after=session.real_balance,
                ledger_entries=[],
                success=True,
                message="No charges to commit",
            )

        real_balance_before = session.real_balance
        total_deducted = Decimal("0")
        ledger_entries = []
        committed_count = 0
        errors = []

        for charge in session.simulated_charges:
            try:
                from ..schemas.billing import ServiceCategory

                category = ServiceCategory(charge.service_category)

                result = await agent_money.charge(
                    wallet_id=session.wallet_id,
                    service_category=category,
                    units=Decimal(str(charge.units)),
                    description=f"[COMMITTED] {charge.description}",
                    dry_run=False,
                )

                if isinstance(result, InsufficientFundsResponse):
                    errors.append(f"{charge.service_category}: {result.error}")
                    ledger_entries.append(
                        {
                            "charge_id": charge.charge_id,
                            "service_category": charge.service_category,
                            "units": charge.units,
                            "credits": float(charge.credits),
                            "description": charge.description,
                            "committed": False,
                            "error": result.error,
                        }
                    )
                    continue

                total_deducted += charge.credits
                committed_count += 1

                ledger_entries.append(
                    {
                        "charge_id": charge.charge_id,
                        "service_category": charge.service_category,
                        "units": charge.units,
                        "credits": float(charge.credits),
                        "description": charge.description,
                        "committed": True,
                    }
                )

            except Exception as e:
                logger.error(f"Failed to commit charge {charge.charge_id}: {e}")
                errors.append(f"{charge.service_category}: {str(e)}")

                ledger_entries.append(
                    {
                        "charge_id": charge.charge_id,
                        "service_category": charge.service_category,
                        "units": charge.units,
                        "credits": float(charge.credits),
                        "description": charge.description,
                        "committed": False,
                        "error": str(e),
                    }
                )

        wallet = await agent_money.get_wallet(session.wallet_id)
        real_balance_after = wallet.balance if wallet else real_balance_before

        # Session was already removed by _claim_session above; nothing to end.

        success = committed_count == len(session.simulated_charges)
        message = (
            f"Committed {committed_count}/{len(session.simulated_charges)} charges. "
            f"Deducted {total_deducted} credits."
        )
        if errors:
            message += f" Errors: {'; '.join(errors[:3])}"

        logger.info(f"Committed sandbox session {session_id}: {message}")

        return CommitResult(
            session_id=session_id,
            wallet_id=session.wallet_id,
            committed_charges=committed_count,
            total_credits_deducted=total_deducted,
            real_balance_before=real_balance_before,
            real_balance_after=real_balance_after,
            ledger_entries=ledger_entries,
            success=success,
            message=message,
        )

    async def revert_session(self, session_id: str) -> RevertResult:
        """
        Revert a sandbox session.

        Simply discards the simulated charges without affecting real billing.
        The session is cleaned up.

        Args:
            session_id: The dry-run session to revert

        Returns:
            RevertResult confirming the revert
        """
        session = await self.get_session(session_id)
        if not session:
            return RevertResult(
                session_id=session_id,
                wallet_id="",
                reverted=False,
                message="Session not found",
            )

        charge_count = len(session.simulated_charges)
        total = session.total_simulated

        if await self.end_session(session_id) is None:
            # The session was claimed between the read above and this call --
            # by ``commit_session``, which claims atomically. Discarding this
            # return value meant reporting ``reverted=True`` with the wallet id
            # from the stale read, and a message reading "No changes made to
            # real wallet" when the concurrent commit had already applied the
            # charges to it. Reproduced: a wallet at 100 ended at 96 while the
            # revert claimed nothing had moved.
            #
            # Reported as a missing session, matching the branch above, so the
            # router's empty-``wallet_id`` check answers 404 rather than a 200
            # asserting a revert that did not happen.
            return RevertResult(
                session_id=session_id,
                wallet_id="",
                reverted=False,
                message="Session not found",
            )

        logger.info(
            f"Reverted sandbox session {session_id}: "
            f"discarded {charge_count} charges ({total} credits)"
        )

        return RevertResult(
            session_id=session_id,
            wallet_id=session.wallet_id,
            reverted=True,
            message=(
                f"Reverted {charge_count} simulated charges "
                f"({total} credits). No changes made to real wallet."
            ),
        )

    async def list_sessions(self, wallet_id: str) -> list[DryRunSession]:
        """List all active sessions for a wallet."""
        redis_client = await self._get_redis()

        if redis_client:
            session_ids = await redis_client.smembers(
                await self._wallet_sessions_key(wallet_id)
            )
            sessions = []
            for sid in session_ids:
                # redis-py's smembers is typed as returning bytes|str even
                # though decode_responses=True guarantees str at runtime;
                # normalize explicitly rather than relying on that.
                session = await self.get_session(
                    sid.decode() if isinstance(sid, bytes) else sid
                )
                if session:
                    sessions.append(session)
            return sessions

        return [
            data["session"]
            for data in self._memory_store.values()
            if data["session"].wallet_id == wallet_id
        ]

    def _deserialize_session(self, data: dict) -> DryRunSession:
        """Deserialize session from JSON."""
        charges = []
        for c in data.get("simulated_charges", []):
            charges.append(
                SimulatedCharge(
                    charge_id=c["charge_id"],
                    service_category=c["service_category"],
                    units=c["units"],
                    credits=Decimal(str(c["credits"])),
                    description=c["description"],
                    timestamp=datetime.fromisoformat(c["timestamp"]),
                )
            )

        return DryRunSession(
            session_id=data["session_id"],
            wallet_id=data["wallet_id"],
            real_balance=Decimal(str(data["real_balance"])),
            simulated_charges=charges,
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None


_shadow_ledger: ShadowLedger | None = None


def get_shadow_ledger() -> ShadowLedger:
    """Get or create the global ShadowLedger singleton."""
    global _shadow_ledger
    if _shadow_ledger is None:
        _shadow_ledger = ShadowLedger()
    return _shadow_ledger
