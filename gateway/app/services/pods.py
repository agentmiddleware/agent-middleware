"""Pod orchestration: compose existing wallet + key primitives, atomically.

A pod is a sponsor wallet tagged as a pod (``metadata_json: {"kind": "pod",
"pod_name": ...}``) plus N agent wallets — one per member — each with its
own wallet-scoped API key. All of it is provisioned from one
``POST /v1/pods`` call.

Composition, not a new primitive
---------------------------------
Every money movement here is an existing, tested operation
(``AgentMoney.create_sponsor_wallet``, ``AgentMoney.create_agent_wallet``,
``APIKeyService.create_key``). This module adds no new ledger entries, no
new spend-authorization path, and no new key format. If those primitives are
correct, a pod is correct.

Atomicity: one real database transaction, not a saga
-------------------------------------------------------
Pod creation runs as a single database transaction: the sponsor wallet,
every member's agent wallet, and every member's API key are all written
through the same open ``AsyncSession``, which this module opens and
commits exactly once. ``WalletEngine.create_sponsor_wallet``,
``WalletEngine.create_agent_wallet``, and ``APIKeyService.create_key`` each
accept an optional ``session`` parameter for exactly this: when given, they
add/flush against it and never begin or commit it themselves, so the
caller's transaction is the only one that decides commit vs. rollback (see
each method's docstring in ``app/services/wallet_engine.py`` and
``app/services/api_key_service.py``). If any member fails partway — a
transient DB error, an unexpected insufficient-funds race, anything — the
``async with session.begin():`` block rolls back automatically and nothing
from this call persists: no sponsor wallet, no member wallets, no keys.
There is no partial pod to report or recover from. The pre-flight budget
check (over-allocation against the pod total) still runs first, purely so
the common mistake gets a clean 422 instead of a rollback.

This is not a new distributed-transaction primitive: it is one ordinary
transaction spanning calls that were already written to support being
composed this way. Every write inside it is still, individually, the exact
same tested code path standalone callers use.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from ..db.database import get_session_factory
from .agent_money import AgentMoney, WalletNotFoundError, get_agent_money
from .api_key_service import APIKeyService, get_api_key_service

POD_METADATA_KIND = "pod"


class PodError(Exception):
    """Base error for pod orchestration failures."""


class PodNotFoundError(PodError):
    def __init__(self, pod_id: str):
        self.pod_id = pod_id
        super().__init__(f"Pod not found: {pod_id}")


class PodBudgetError(PodError):
    """Requested member budgets cannot fit inside the pod's total budget."""


class PodProvisioningFailedError(PodError):
    """The pod transaction rolled back; nothing from this call was created.

    Because create_pod runs as one database transaction, there is no
    partial-pod case to describe: either every member was provisioned, or
    (on any error) the whole transaction rolled back and no sponsor wallet,
    member wallet, or key exists from this call. This wraps whatever the
    underlying failure was, for a clean, single error surface to the caller.
    """

    def __init__(self, failed_agent_id: str | None, original_error: Exception):
        self.failed_agent_id = failed_agent_id
        self.original_error = original_error
        where = f" while provisioning {failed_agent_id!r}" if failed_agent_id else ""
        super().__init__(
            f"Pod creation failed{where} and was rolled back in full: {original_error}"
        )


@dataclass
class PodMemberSpec:
    agent_id: str
    key_name: str
    budget_credits: Decimal | None


@dataclass
class ProvisionedMember:
    agent_id: str
    wallet_id: str
    budget_credits: Decimal
    key_id: str
    key_prefix: str
    api_key: str


@dataclass
class ProvisionedPod:
    pod_id: str
    pod_name: str
    budget_credits: Decimal
    members: list[ProvisionedMember]


def _split_budget_evenly(
    total: Decimal, unset_count: int, already_allocated: Decimal
) -> Decimal:
    remaining = total - already_allocated
    if unset_count == 0:
        return Decimal("0")
    # ROUND_DOWN (truncate), not the Decimal default ROUND_HALF_EVEN: for a
    # non-terminating division (e.g. 1 / 6 = 0.16666666...) the default
    # rounds the 8th decimal place UP whenever the discarded remainder is
    # more than half, so `share * unset_count` can exceed `remaining` by a
    # few units of the smallest wallet denomination. With unset_count
    # members each getting that rounded-up share, the last member(s) can
    # then fail create_agent_wallet with InsufficientFundsError, turning a
    # clean pod creation into a partial-provisioning 500. Truncating instead
    # guarantees share * unset_count <= remaining always; any leftover
    # (at most unset_count * 1e-8 credits) simply stays unallocated at the
    # pod level, visible via GET /v1/pods/{pod_id}'s remaining_at_pod.
    share = (remaining / unset_count).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    return share if share >= 0 else Decimal("0")


def _resolve_member_budgets(
    total_budget: Decimal, members: list[PodMemberSpec]
) -> list[Decimal]:
    """Resolve each member's budget, splitting the remainder evenly where unset.

    Raises PodBudgetError before anything is created if explicit member
    budgets alone already exceed the pod total.
    """
    explicit_sum = sum(
        (m.budget_credits for m in members if m.budget_credits is not None),
        Decimal("0"),
    )
    if explicit_sum > total_budget:
        raise PodBudgetError(
            f"Member budgets sum to {explicit_sum} which exceeds the pod's "
            f"total budget of {total_budget}."
        )

    unset_count = sum(1 for m in members if m.budget_credits is None)
    even_share = _split_budget_evenly(total_budget, unset_count, explicit_sum)

    return [
        m.budget_credits if m.budget_credits is not None else even_share
        for m in members
    ]


class PodService:
    """Orchestrates pod creation and budget reporting on top of AgentMoney."""

    def __init__(
        self,
        money: AgentMoney | None = None,
        keys: APIKeyService | None = None,
    ):
        self._money = money or get_agent_money()
        self._keys = keys or get_api_key_service()

    async def create_pod(
        self,
        pod_name: str,
        budget_credits: Decimal,
        members: list[PodMemberSpec],
    ) -> ProvisionedPod:
        """Provision a pod as one atomic transaction.

        Either every member's wallet and key gets created, or (on any
        failure) the whole transaction rolls back and nothing — not the
        sponsor wallet, not any member — persists. See the module docstring
        for how the session is shared across the underlying calls.
        """
        member_budgets = _resolve_member_budgets(budget_credits, members)

        agent_id_in_flight: str | None = None
        session_factory = get_session_factory()
        async with session_factory() as session:
            try:
                async with session.begin():
                    sponsor = await self._money.create_sponsor_wallet(
                        sponsor_name=f"pod:{pod_name}",
                        email=f"pod+{pod_name}@pods.local",
                        initial_credits=budget_credits,
                        require_kyc=False,
                        metadata={"kind": POD_METADATA_KIND, "pod_name": pod_name},
                        session=session,
                    )

                    provisioned: list[ProvisionedMember] = []
                    for member, member_budget in zip(members, member_budgets):
                        agent_id_in_flight = (
                            member.agent_id or f"pod-member-{len(provisioned)}"
                        )
                        agent_wallet = await self._money.create_agent_wallet(
                            sponsor_wallet_id=sponsor.wallet_id,
                            agent_id=agent_id_in_flight,
                            budget_credits=member_budget,
                            session=session,
                        )
                        key = await self._keys.create_key(
                            wallet_id=agent_wallet.wallet_id,
                            key_name=member.key_name,
                            session=session,
                        )
                        provisioned.append(
                            ProvisionedMember(
                                agent_id=agent_id_in_flight,
                                wallet_id=agent_wallet.wallet_id,
                                budget_credits=member_budget,
                                key_id=key["key_id"],
                                key_prefix=key["key_prefix"],
                                api_key=key["api_key"],
                            )
                        )
            except Exception as exc:  # noqa: BLE001 - re-raised typed, with context
                # `async with session.begin():` already rolled back on this
                # exception: no sponsor wallet, member wallet, or key from
                # this call persisted anywhere.
                raise PodProvisioningFailedError(agent_id_in_flight, exc) from exc

        return ProvisionedPod(
            pod_id=sponsor.wallet_id,
            pod_name=pod_name,
            budget_credits=budget_credits,
            members=provisioned,
        )

    async def get_pod_budget(self, pod_id: str) -> dict:
        """Aggregate budget status for a pod. Raises PodNotFoundError if the
        wallet doesn't exist, or PodError if it exists but isn't a pod."""
        wallet = await self._money.get_wallet(pod_id)
        if wallet is None:
            raise PodNotFoundError(pod_id)

        metadata = wallet.metadata or {}
        if metadata.get("kind") != POD_METADATA_KIND:
            raise PodError(f"Wallet {pod_id} exists but is not a pod.")

        try:
            swarm = await self._money.get_swarm_budget(pod_id)
        except WalletNotFoundError as exc:
            raise PodNotFoundError(pod_id) from exc

        return {
            "pod_id": pod_id,
            "pod_name": metadata.get("pod_name", ""),
            "total_budget": wallet.lifetime_credits,
            "remaining_at_pod": swarm["parent_balance"],
            "allocated_to_members": swarm["total_delegated"],
            "members": [
                {
                    "agent_id": child.agent_id,
                    "wallet_id": child.wallet_id,
                    "status": child.status,
                    "balance": child.balance,
                    "lifetime_debits": child.lifetime_debits,
                }
                for child in swarm["children"]
            ],
        }


_pod_service: PodService | None = None


def get_pod_service() -> PodService:
    global _pod_service
    if _pod_service is None:
        _pod_service = PodService()
    return _pod_service
