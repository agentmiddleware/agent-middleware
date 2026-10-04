"""
Agent Financial Gateways Router
---------------------------------
Two-tier wallet system: human sponsors (liability sinks) fund agent wallets.
Per-action micro-metering charges fractions of a cent per API call.
Swarm arbitrage silently books margin on every transaction.

This is how the API generates revenue autonomously.
"""

from decimal import Decimal
from typing import ClassVar, cast
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..core.auth import AuthContext, get_auth_context, verify_api_key
from ..core.config import get_settings
from ..core.dependencies import get_agent_money
from .http_idempotency import (
    begin_http_idempotency as _begin_idempotency,
)
from ..services.agent_money import (
    AgentMoney,
    DEFAULT_PRICING,
    InsufficientFundsError,
    WalletNotFoundError,
)
from ..services.governance import record_governed_action
from ..services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    get_idempotency_service,
)
from ..services.policies import evaluate_wallet_policy
from ..services.velocity_monitor import WalletFrozenError
from ..services.wallet_engine import WalletExpiredError
from ..services.stripe_integration import get_stripe_integration
from ..services.shadow_ledger import SimulatedChargeResult, get_shadow_ledger
from ..services.acp_bridge import (
    ACPBridgeError,
    audit_request_id,
    get_acp_commerce_adapter,
)
from ..schemas.acp import ACPCheckoutRequest, ACPCheckoutResponse
from ..schemas.billing import (
    CreateSponsorWalletRequest,
    CreateAgentWalletRequest,
    CreateChildWalletRequest,
    ChildWalletResponse,
    SwarmBudgetSummary,
    ReclaimResponse,
    WalletResponse,
    WalletListResponse,
    LedgerResponse,
    TopUpRequest,
    InsufficientFundsResponse,
    ServiceCategory,
    PricingTableResponse,
    ArbitrageReport,
    AlertListResponse,
    RegisterServiceRequest,
    ServiceRegistration,
    ExactDecimalFieldsMixin,
    MAX_STORABLE_AMOUNT,
)


def _require_wallet_access(auth: AuthContext, wallet_id: str) -> None:
    auth.require_wallet_access(wallet_id)


def _session_not_found(session_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "session_not_found",
            "message": f"Session {session_id} not found",
        },
    )


async def _load_owned_dry_run_session(
    shadow_ledger, session_id: str, auth: AuthContext
):
    """Load a dry-run session, or 404 — whether it is missing or not the caller's.

    Every dry-run session endpoint used to look the session up, 404 if it was
    absent, and only then call ``_require_wallet_access``. That order makes the
    surface an oracle: a caller holding any valid wallet-scoped key gets 404 for
    an invented session id and 403 for a real one, so the two are
    distinguishable without any access at all.

    Worse, the 403 body carries ``session.wallet_id`` — the *owning* wallet —
    so probing another tenant's session id disclosed that tenant's wallet id
    outright. Session ids are UUID4 and cannot be enumerated, but an id learned
    from a log, a trace, or a shared URL was enough.

    A session the caller may not see is now indistinguishable from one that
    does not exist. Authorized callers are unaffected.
    """
    session = await shadow_ledger.get_session(session_id)
    if not session:
        raise _session_not_found(session_id)
    try:
        _require_wallet_access(auth, session.wallet_id)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise _session_not_found(session_id) from None
        raise
    return session


async def _record_billing_governance(
    *,
    event: str,
    auth: AuthContext,
    wallet_id: str,
    service_category: str | None = None,
    endpoint: str,
    request_id: str | None = None,
    estimated_cost: float | None = None,
    committed_cost: float | None = None,
    ok: bool = True,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    await record_governed_action(
        event=event,
        auth=auth,
        wallet_id=wallet_id,
        target="billing",
        endpoint=endpoint,
        request_id=request_id,
        estimated_cost=estimated_cost,
        committed_cost=committed_cost,
        allowed=ok,
        reason="allowed" if ok else error,
        ok=ok,
        error=error,
        metadata={"service_category": service_category, **(metadata or {})},
    )


async def _enforce_billing_policy(
    *,
    auth: AuthContext,
    wallet_id: str,
    category: ServiceCategory,
    units: float,
    endpoint: str,
    request_id: str | None,
    money: AgentMoney,
) -> tuple[float, str | None, dict]:
    cost = Decimal(str(units)) * DEFAULT_PRICING[category][1]
    estimated_cost = float(cost)
    policy = await evaluate_wallet_policy(
        wallet_id=wallet_id,
        tool_name="billing",
        service_category=category.value,
        estimated_cost=cost,
        daily_spend_used=await money.get_daily_spend(wallet_id),
        simulation=False,
    )
    if not policy.allowed:
        await _record_billing_governance(
            event="billing.charge",
            auth=auth,
            wallet_id=wallet_id,
            service_category=category.value,
            endpoint=endpoint,
            request_id=request_id,
            estimated_cost=estimated_cost,
            ok=False,
            error=policy.reason,
            metadata={
                "policy_id": policy.policy_id,
                "evaluated_constraints": policy.evaluated_constraints,
                "units": units,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": policy.reason,
                "policy_id": policy.policy_id,
                "evaluated_constraints": policy.evaluated_constraints,
            },
        )
    return estimated_cost, policy.policy_id, policy.evaluated_constraints


router = APIRouter(
    prefix="/v1/billing",
    tags=["Agent Financial Gateways"],
    responses={
        401: {"description": "Missing API key"},
        402: {"description": "Insufficient funds", "model": InsufficientFundsResponse},
    },
)

# Expansion surfaces beyond the wedge's "one debit per dispatch" story:
# child/swarm wallets, wallet-to-wallet transfers, Stripe self-serve top-ups,
# the marketplace (service registration/listing, arbitrage), velocity status,
# and the dry-run sandbox. Real code, dormant demand — mounted only via
# app.main.mount_dormant_trust_surfaces (ENABLE_PROOF_SURFACES=true), so the
# production OpenAPI advertises the wedge: sponsor/agent wallet CRUD, ledger,
# charge, pricing. The service layer underneath (transfers, velocity
# enforcement, shadow ledger) is untouched — only the HTTP surface gates.
expansion_router = APIRouter(
    prefix="/v1/billing",
    tags=["Agent Financial Gateways"],
    responses={
        401: {"description": "Missing API key"},
        402: {"description": "Insufficient funds", "model": InsufficientFundsResponse},
    },
)


# --- Wallet Management ---


@router.post(
    "/wallets/sponsor",
    response_model=WalletResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a sponsor wallet (liability sink)",
    description=(
        "Bootstrap operator provisioning only. Create a human-owned root "
        "account that acts as the 'liability sink' for agent spending. "
        "Sponsors ingest fiat currency via payment rails and convert it to "
        "ecosystem credits. Agent wallets are provisioned from sponsor balances."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Bootstrap administrator access required"
        }
    },
)
async def create_sponsor_wallet(
    request: CreateSponsorWalletRequest,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    # Creating a sponsor mints ecosystem credits directly into its balance
    # (initial_credits), so this is a credit-issuance operation, not self-serve
    # onboarding. Without a gate, any valid wallet key could mint arbitrary
    # credits — the billing system would not be an economic control. Restrict to
    # bootstrap/admin credentials, which are the identities that operate the
    # fiat -> credit conversion this endpoint represents.
    auth.require_bootstrap_admin()
    initial = (
        Decimal(str(request.initial_credits))
        if request.initial_credits
        else Decimal("0")
    )
    return await money.create_sponsor_wallet(
        sponsor_name=request.sponsor_name,
        email=request.email,
        initial_credits=initial,
        currency=request.currency,
        metadata=request.metadata,
        require_kyc=request.require_kyc,
    )


@router.post(
    "/wallets/agent",
    response_model=WalletResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision an agent wallet",
    description=(
        "Create a pre-paid wallet for an autonomous agent, funded from a "
        "sponsor's balance. The agent can then transact autonomously up to "
        "its budget without human intervention. Supports daily spend limits "
        "and automatic refills."
    ),
)
async def create_agent_wallet(
    request: CreateAgentWalletRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, request.sponsor_wallet_id)
    # Resolve the sponsor before opening an idempotency record: the record's
    # wallet_id references wallets, so beginning one for an unknown sponsor
    # would fail the foreign key and answer 500 where the unkeyed path answers
    # 404. Same message as the engine's WalletNotFoundError so keyed and
    # unkeyed callers see one body.
    if await money.get_wallet(request.sponsor_wallet_id) is None:
        raise HTTPException(
            status_code=404,
            detail=str(WalletNotFoundError(request.sponsor_wallet_id)),
        )
    # Opt-in idempotency, keyed on the sponsor (the debited side): a client
    # whose provisioning call timed out after the sponsor was debited retries
    # with the same Idempotency-Key and gets the original wallet back instead
    # of funding a second one and paying twice.
    guard, replay = await _begin_idempotency(
        idempotency_key=idempotency_key,
        wallet_id=request.sponsor_wallet_id,
        endpoint="/v1/billing/wallets/agent",
        request_payload=request.model_dump(mode="json"),
    )
    if replay is not None:
        return replay
    try:
        wallet = await money.create_agent_wallet(
            sponsor_wallet_id=request.sponsor_wallet_id,
            agent_id=request.agent_id,
            budget_credits=Decimal(str(request.budget_credits)),
            daily_limit=(
                # `is not None`: the schema declares ge=0, so 0 is a valid cap
                # meaning "spend nothing", and a truthiness test turned that
                # into None -- which the engine reads as "no cap at all".
                Decimal(str(request.daily_limit))
                if request.daily_limit is not None
                else None
            ),
            auto_refill=request.auto_refill,
            auto_refill_threshold=Decimal(str(request.auto_refill_threshold)),
            auto_refill_amount=Decimal(str(request.auto_refill_amount)),
        )
    except WalletNotFoundError as e:
        # Every terminal branch must complete the record, or the key stays
        # in-progress and a retry answers 409 until the stale-record sweep.
        await guard.complete({"detail": str(e)}, 404)
        raise HTTPException(status_code=404, detail=str(e))
    except InsufficientFundsError as e:
        insufficient_detail = {
            "error": "insufficient_funds",
            "message": (
                f"Insufficient funds in sponsor wallet: "
                f"balance={e.current_balance}, required={e.required_amount}"
            ),
            "wallet_id": e.wallet_id,
            "current_balance": float(e.current_balance),
            "required_amount": float(e.required_amount),
            "shortfall": float(e.shortfall),
        }
        # Terminal for this key: nothing was created or debited, and a replay
        # should tell the caller the same thing rather than retry blindly.
        await guard.complete({"detail": insufficient_detail}, 400)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=insufficient_detail,
        )
    except ValueError as e:
        wallet_error = {"error": "wallet_error", "message": str(e)}
        await guard.complete({"detail": wallet_error}, 400)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=wallet_error,
        )
    body = wallet.model_dump(mode="json")
    await guard.complete(body, 201, response_reference=body.get("wallet_id"))
    return wallet


@expansion_router.post(
    "/wallets/child",
    response_model=ChildWalletResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Parent wallet expired or wallet access denied",
        },
    },
    summary="Spawn a child sub-agent wallet",
    description=(
        "Create a spend-capped child wallet from a parent agent's balance. "
        "Enables hierarchical swarm budgeting: a master agent building a complex tool "
        "can spin up specialized sub-agents (code-writer, tester, deployer), each with "
        "a micro-budget and hard lifetime cap. Supports TTL for auto-expiry."
    ),
)
async def create_child_wallet(
    request: CreateChildWalletRequest,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, request.parent_wallet_id)
    try:
        response = await money.create_child_wallet(
            parent_wallet_id=request.parent_wallet_id,
            child_agent_id=request.child_agent_id,
            budget_credits=Decimal(str(request.budget_credits)),
            max_spend=Decimal(str(request.max_spend)),
            task_description=request.task_description,
            ttl_seconds=request.ttl_seconds,
            auto_reclaim=request.auto_reclaim,
        )
        return ChildWalletResponse(
            wallet_id=response.wallet_id,
            wallet_type=response.wallet_type,
            parent_wallet_id=response.sponsor_wallet_id,
            child_agent_id=response.child_agent_id,
            balance=response.balance,
            max_spend=response.max_spend,
            spent=0.0,
            task_description=response.task_description or "",
            ttl_seconds=response.ttl_seconds,
            auto_reclaim=True,
            status=response.status,
            created_at=response.created_at,
        )
    except WalletNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "insufficient_funds",
                "wallet_id": e.wallet_id,
                "current_balance": str(e.current_balance),
                "required_amount": str(e.required_amount),
                "shortfall": str(e.shortfall),
            },
        )
    except WalletExpiredError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "wallet_expired",
                "wallet_id": e.wallet_id,
                "expires_at": e.expires_at.isoformat(),
                "message": str(e),
            },
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "child_wallet_error", "message": str(e)},
        )


@expansion_router.post(
    "/wallets/{wallet_id}/reclaim",
    response_model=ReclaimResponse,
    summary="Reclaim unspent credits from a child wallet",
    description=(
        "Close a child wallet and return unspent credits to the parent. "
        "Use this when a sub-agent completes its task or you want to reallocate budget."
    ),
)
async def reclaim_child_wallet(
    wallet_id: str,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, wallet_id)
    try:
        result = await money.reclaim_child_wallet(wallet_id)
        return ReclaimResponse(
            child_wallet_id=result["child_wallet_id"],
            parent_wallet_id=result["parent_wallet_id"],
            credits_reclaimed=result["credits_reclaimed"],
            parent_balance_after=result["parent_balance_after"],
            child_status=result["child_status"],
        )
    except WalletNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "reclaim_error", "message": str(e)},
        )


@expansion_router.get(
    "/wallets/{wallet_id}/swarm",
    response_model=SwarmBudgetSummary,
    summary="Get swarm budget summary",
    description="Hierarchical budget summary for an agent's child wallets.",
)
async def get_swarm_budget(
    wallet_id: str,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, wallet_id)
    try:
        result = await money.get_swarm_budget(wallet_id)
        return SwarmBudgetSummary(
            parent_wallet_id=result["parent_wallet_id"],
            parent_balance=result["parent_balance"],
            total_delegated=result["total_delegated"],
            total_reclaimed=result["total_reclaimed"],
            active_children=result["active_children"],
            completed_children=result["completed_children"],
            frozen_children=result["frozen_children"],
            children=result["children"],
        )
    except WalletNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "/wallets/{wallet_id}",
    response_model=WalletResponse,
    summary="Get wallet details",
)
async def get_wallet(
    wallet_id: str,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, wallet_id)
    wallet = await money.get_wallet(wallet_id)
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet


@router.get(
    "/wallets",
    response_model=WalletListResponse,
    summary="List wallets",
)
async def list_wallets(
    wallet_type: str | None = Query(None, description="Filter by wallet type"),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    if auth.is_bootstrap_admin:
        wallets = await money.list_wallets(wallet_type=wallet_type)
    else:
        wallet = await money.get_wallet(auth.wallet_id or "")
        wallets = [wallet] if wallet else []
        if wallet_type:
            wallets = [w for w in wallets if w.wallet_type.value == wallet_type]
    return WalletListResponse(wallets=wallets, total=len(wallets))


# --- Ledger ---


@router.get(
    "/ledger/{wallet_id}",
    response_model=LedgerResponse,
    summary="Get wallet ledger",
)
async def get_ledger(
    wallet_id: str,
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, wallet_id)
    entries = await money.get_ledger(wallet_id, limit)

    amounts = [Decimal(e.amount_exact or str(e.amount)) for e in entries]
    period_credits = sum((amount for amount in amounts if amount > 0), Decimal("0"))
    period_debits = sum((-amount for amount in amounts if amount < 0), Decimal("0"))

    return LedgerResponse(
        entries=entries,
        total=len(entries),
        wallet_id=wallet_id,
        period_credits=float(period_credits),
        period_credits_exact=str(period_credits),
        period_debits=float(period_debits),
        period_debits_exact=str(period_debits),
    )


# --- Charging ---


@router.post(
    "/charge",
    summary="Charge a wallet for API usage",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Wallet expired, frozen, or denied by policy",
        },
    },
)
async def charge_wallet(
    request: Request,
    wallet_id: str,
    service_category: ServiceCategory | None = None,
    service: ServiceCategory | None = Query(
        None,
        description="Service category (alias for service_category)",
    ),
    units: float = Query(
        1.0,
        gt=0,
        # ``gt=0`` alone lets an infinite value through -- it is a valid
        # float and it is greater than zero -- and metering then computes
        # ``Decimal("Infinity") - Decimal("Infinity")``, which raises
        # InvalidOperation and answers 500. The crash is the lucky outcome:
        # an infinite units count that reached a write would put a
        # non-finite amount in the ledger, which no reconciliation can undo.
        # (A not-a-number value is already refused: it fails ``gt=0``.)
        allow_inf_nan=False,
        description="Number of units consumed",
    ),
    request_path: str | None = None,
    description: str | None = None,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    _require_wallet_access(auth, wallet_id)
    category = service_category or service
    if not category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_service",
                "message": "service_category is required",
            },
        )
    request_id = request.headers.get("X-Request-ID")
    endpoint = "/v1/billing/charge"

    # Resolve after tenant authorization and before the idempotency row's FK.
    if not await money.get_wallet(wallet_id):
        await _record_billing_governance(
            event="billing.charge",
            auth=auth,
            wallet_id=wallet_id,
            service_category=category.value,
            endpoint=endpoint,
            request_id=request_id,
            ok=False,
            error="wallet_not_found",
            metadata={"units": units, "request_path": request_path},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "wallet_not_found",
                "wallet_id": wallet_id,
                "message": str(WalletNotFoundError(wallet_id)),
            },
        )

    # Idempotency is opt-in via the Idempotency-Key header: a client that
    # retries a charge (e.g. after a timeout) with the same key gets the
    # original outcome replayed instead of being billed twice.
    idem = None
    idem_key: str | None = None
    if idempotency_key:
        idem = get_idempotency_service()
        idem_key = idempotency_key
        try:
            replay = await idem.begin(
                wallet_id=wallet_id,
                endpoint=endpoint,
                idempotency_key=idem_key,
                request_payload={
                    "wallet_id": wallet_id,
                    "service_category": category.value,
                    "units": units,
                    "request_path": request_path,
                    "description": description,
                },
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "idempotency_key_reused", "message": str(exc)},
            ) from exc
        except IdempotencyInProgressError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "idempotency_in_progress", "message": str(exc)},
            ) from exc
        if replay is not None:
            return JSONResponse(
                status_code=replay.status_code, content=replay.response_json
            )

    async def _complete_idempotency(response_json: dict, status_code: int) -> None:
        if not idem or not idem_key:
            return
        await idem.complete(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idem_key,
            response_reference=response_json.get("entry_id"),
            response_json=response_json,
            status_code=status_code,
        )

    try:
        (
            policy_estimated_cost,
            policy_id,
            evaluated_constraints,
        ) = await _enforce_billing_policy(
            auth=auth,
            wallet_id=wallet_id,
            category=category,
            units=units,
            endpoint=endpoint,
            request_id=request_id,
            money=money,
        )
    except HTTPException as exc:
        await _complete_idempotency({"detail": exc.detail}, exc.status_code)
        raise
    try:
        result = await money.charge(
            wallet_id=wallet_id,
            service_category=category,
            units=Decimal(str(units)),
            request_path=request_path,
            description=description or "",
        )

        if isinstance(result, SimulatedChargeResult):
            # This endpoint never passes dry_run=True, so a SimulatedChargeResult
            # here would mean money.charge()'s behavior changed underneath us.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "unexpected_simulated_result",
                    "message": "Real charge unexpectedly returned a simulated result.",
                },
            )

        if isinstance(result, InsufficientFundsResponse):
            error_code = result.error
            denial_status = (
                status.HTTP_402_PAYMENT_REQUIRED
                if error_code == "insufficient_funds"
                else status.HTTP_403_FORBIDDEN
            )
            await _record_billing_governance(
                event="billing.charge",
                auth=auth,
                wallet_id=wallet_id,
                service_category=category.value,
                endpoint=endpoint,
                request_id=request_id,
                estimated_cost=float(result.required_amount),
                ok=False,
                error=error_code,
                metadata={
                    "units": units,
                    "request_path": request_path,
                    "shortfall": float(result.shortfall),
                },
            )
            denial_detail = result.model_dump(mode="json")
            await _complete_idempotency(
                {"detail": denial_detail},
                denial_status,
            )
            raise HTTPException(status_code=denial_status, detail=denial_detail)

        await _record_billing_governance(
            event="billing.charge",
            auth=auth,
            wallet_id=wallet_id,
            service_category=category.value,
            endpoint=endpoint,
            request_id=request_id,
            estimated_cost=policy_estimated_cost,
            committed_cost=abs(float(result.amount)),
            ok=True,
            metadata={
                "units": units,
                "request_path": request_path,
                "ledger_entry_id": result.entry_id,
                "amount_exact": result.amount_exact,
                "balance_after_exact": result.balance_after_exact,
                "policy_id": policy_id,
                "evaluated_constraints": evaluated_constraints,
            },
        )
        await _complete_idempotency(result.model_dump(mode="json"), 200)
        return result
    except WalletNotFoundError as e:
        # Every other endpoint in this router answers 404 for an unknown
        # wallet; this one let the exception escape as a 500, which reads as
        # "the server is broken" rather than "that wallet does not exist" and
        # sends a caller retrying a typo down the wrong path entirely.
        await _record_billing_governance(
            event="billing.charge",
            auth=auth,
            wallet_id=wallet_id,
            service_category=category.value,
            endpoint=endpoint,
            request_id=request_id,
            ok=False,
            error="wallet_not_found",
            metadata={"units": units, "request_path": request_path},
        )
        not_found_detail = {
            "error": "wallet_not_found",
            "wallet_id": wallet_id,
            "message": str(e),
        }
        await _complete_idempotency(
            {"detail": not_found_detail},
            status.HTTP_404_NOT_FOUND,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=not_found_detail,
        )
    except WalletFrozenError as e:
        if category:
            await _record_billing_governance(
                event="billing.charge",
                auth=auth,
                wallet_id=wallet_id,
                service_category=category.value,
                endpoint=endpoint,
                request_id=request_id,
                ok=False,
                error="wallet_frozen",
                metadata={"reason": e.reason, "units": units},
            )
        frozen_detail = {
            "error": "wallet_frozen",
            "wallet_id": e.wallet_id,
            "reason": e.reason,
            "message": (
                "Wallet has been frozen due to anomalous spend velocity. "
                "Contact sponsor."
            ),
        }
        await _complete_idempotency(
            {"detail": frozen_detail},
            status.HTTP_403_FORBIDDEN,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=frozen_detail,
        )


# --- Top-Up ---


@expansion_router.post(
    "/top-up",
    response_model=None,
    status_code=status.HTTP_410_GONE,
    summary="Direct top-ups are disabled",
    description=(
        "Deprecated. Direct requests cannot prove payment settlement. Use "
        "`POST /v1/billing/top-up/prepare`; credits are minted only after the "
        "payment provider's verified webhook."
    ),
    deprecated=True,
    responses={
        status.HTTP_410_GONE: {
            "description": (
                "Direct top-ups are disabled; use `/v1/billing/top-up/prepare`."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "error": "direct_top_up_disabled",
                            "message": (
                                "Direct top-ups cannot verify payment settlement. "
                                "Create a Stripe PaymentIntent; credits are minted "
                                "only after its verified webhook."
                            ),
                            "prepare_url": "/v1/billing/top-up/prepare",
                        }
                    }
                }
            },
        }
    },
)
async def top_up_wallet(
    request: TopUpRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    _require_wallet_access(auth, request.wallet_id)
    endpoint = "/v1/billing/top-up"
    # Client-supplied payment tokens are not proof of a settled payment. The
    # only supported fiat credit path is the Stripe PaymentIntent flow below,
    # whose signed webhook performs the idempotent mint after settlement.
    await _record_billing_governance(
        event="billing.top_up",
        auth=auth,
        wallet_id=request.wallet_id,
        service_category="top_up",
        endpoint=endpoint,
        ok=False,
        error="direct_top_up_disabled",
        metadata={
            "amount_fiat": request.amount_fiat,
            "payment_method": request.payment_method,
        },
    )
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "error": "direct_top_up_disabled",
            "message": (
                "Direct top-ups cannot verify payment settlement. Create a Stripe "
                "PaymentIntent; credits are minted only after its verified webhook."
            ),
            "prepare_url": "/v1/billing/top-up/prepare",
        },
    )


@expansion_router.post(
    "/top-up/prepare",
    summary="Prepare a fiat top-up via Stripe",
    description=(
        "Create a Stripe PaymentIntent for fiat payment. "
        "After payment succeeds, credits are minted automatically via webhook. "
        "Requires KYC verification if enabled for the wallet."
    ),
)
async def prepare_top_up(
    wallet_id: str,
    amount_fiat: float = Query(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Amount in fiat currency (USD)",
    ),
    currency: str = Query("USD", description="Fiat currency code"),
    auth: AuthContext = Depends(get_auth_context),
):
    _require_wallet_access(auth, wallet_id)
    """
    Prepare a fiat top-up by creating a Stripe PaymentIntent.

    Flow:
    1. Client calls this endpoint to get a client_secret
    2. Client uses Stripe.js to complete payment in browser
    3. Stripe sends webhook to /v1/webhooks/stripe
    4. Webhook handler mints credits to the wallet

    Returns:
        {
            "client_secret": str,
            "payment_intent_id": str,
            "amount_credits": int,
            "amount_fiat": float,
            "currency": str,
        }
    """
    from ..core.config import get_settings

    settings = get_settings()

    if settings.KYC_REQUIRED_FOR_TOPUP:
        from ..services.kyc_service import get_kyc_service

        kyc_service = get_kyc_service()
        kyc_status = await kyc_service.get_verification_status(wallet_id)
        if kyc_status["kyc_status"] != "verified":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "kyc_required",
                    "wallet_id": wallet_id,
                    "kyc_status": kyc_status["kyc_status"],
                    "message": (
                        f"KYC verification required. "
                        f"Current status: {kyc_status['kyc_status']}"
                    ),
                    "verification_url": "/v1/kyc/sessions",
                },
            )

    stripe_integration = get_stripe_integration()

    try:
        result = await stripe_integration.create_top_up_intent(
            wallet_id=wallet_id,
            amount_fiat=Decimal(str(amount_fiat)),
            currency=currency,
        )
        return result
    except WalletNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "topup_prepare_error", "message": str(e)},
        )


@expansion_router.post(
    "/transfer",
    summary="Transfer credits between wallets",
    description="Transfer credits from one wallet to another (agent-to-agent handoff).",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Source wallet expired or wallet access denied",
        },
    },
)
async def transfer_wallets(
    from_wallet_id: str = Query(..., description="Source wallet ID"),
    to_wallet_id: str = Query(..., description="Destination wallet ID"),
    # ``gt=0`` alone admits Infinity, which reached the engine and then broke
    # rendering its own 402 (an infinite shortfall is not JSON): a 500.
    amount: float = Query(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Amount of credits to transfer",
    ),
    description: str | None = Query(None, description="Optional transfer description"),
    correlation_id: str | None = Query(
        None,
        description="Optional ID to link related transfers",
    ),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    """
    Transfer credits between two wallets.

    This enables agent-to-agent payments for completed tasks.
    Both wallets are locked during the transaction for ACID compliance.
    """
    _require_wallet_access(auth, from_wallet_id)
    endpoint = "/v1/billing/transfer"
    # Opt-in idempotency: a retried transfer with the same Idempotency-Key
    # replays the original result instead of moving credits twice. Keyed on
    # the source wallet (the debited side).
    guard, replay = await _begin_idempotency(
        idempotency_key=idempotency_key,
        wallet_id=from_wallet_id,
        endpoint=endpoint,
        request_payload={
            "from_wallet_id": from_wallet_id,
            "to_wallet_id": to_wallet_id,
            "amount": str(amount),
            "description": description,
        },
    )
    if replay is not None:
        return replay
    try:
        result = await money.transfer(
            from_wallet_id=from_wallet_id,
            to_wallet_id=to_wallet_id,
            amount=Decimal(str(amount)),
            description=description or "",
            correlation_id=correlation_id,
        )
        await _record_billing_governance(
            event="billing.transfer",
            auth=auth,
            wallet_id=from_wallet_id,
            service_category="transfer",
            endpoint="/v1/billing/transfer",
            request_id=correlation_id,
            estimated_cost=amount,
            committed_cost=amount,
            ok=True,
            metadata={
                "to_wallet_id": to_wallet_id,
                "transfer_id": result.get("transfer_id"),
                "description": description,
            },
        )
        await guard.complete(result, 200, response_reference=result.get("transfer_id"))
        return result
    except WalletNotFoundError as e:
        await _record_billing_governance(
            event="billing.transfer",
            auth=auth,
            wallet_id=from_wallet_id,
            service_category="transfer",
            endpoint="/v1/billing/transfer",
            request_id=correlation_id,
            estimated_cost=amount,
            ok=False,
            error="wallet_not_found",
            metadata={"to_wallet_id": to_wallet_id},
        )
        await guard.complete({"error": "wallet_not_found", "message": str(e)}, 404)
        raise HTTPException(status_code=404, detail=str(e))
    except InsufficientFundsError as e:
        await _record_billing_governance(
            event="billing.transfer",
            auth=auth,
            wallet_id=from_wallet_id,
            service_category="transfer",
            endpoint="/v1/billing/transfer",
            request_id=correlation_id,
            estimated_cost=amount,
            ok=False,
            error="insufficient_funds",
            metadata={"to_wallet_id": to_wallet_id, "shortfall": float(e.shortfall)},
        )
        insufficient_detail = {
            "error": "insufficient_funds",
            "wallet_id": e.wallet_id,
            "shortfall": float(e.shortfall),
        }
        await guard.complete(insufficient_detail, 402)
        raise HTTPException(
            status_code=402,
            detail=insufficient_detail,
        )
    except WalletExpiredError as e:
        expired_detail = {
            "error": "wallet_expired",
            "wallet_id": e.wallet_id,
            "expires_at": e.expires_at.isoformat(),
            "message": str(e),
        }
        await _record_billing_governance(
            event="billing.transfer",
            auth=auth,
            wallet_id=from_wallet_id,
            service_category="transfer",
            endpoint="/v1/billing/transfer",
            request_id=correlation_id,
            estimated_cost=amount,
            ok=False,
            error="wallet_expired",
            metadata={"to_wallet_id": to_wallet_id},
        )
        await guard.complete(
            {"detail": expired_detail},
            status.HTTP_403_FORBIDDEN,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=expired_detail,
        )
    except ValueError as e:
        await _record_billing_governance(
            event="billing.transfer",
            auth=auth,
            wallet_id=from_wallet_id,
            service_category="transfer",
            endpoint="/v1/billing/transfer",
            request_id=correlation_id,
            estimated_cost=amount,
            ok=False,
            error="transfer_error",
            metadata={"to_wallet_id": to_wallet_id, "message": str(e)},
        )
        await guard.complete(
            {"error": "transfer_error", "message": str(e)},
            status.HTTP_400_BAD_REQUEST,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "transfer_error", "message": str(e)},
        )


# --- ACP Checkout (Agentic Commerce Protocol) ---


@expansion_router.post(
    "/acp/checkout",
    response_model=ACPCheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Settle an ACP checkout under PermitV2 bounds",
    description=(
        "Translate an OpenAI/Stripe Agentic Commerce Protocol checkout into "
        "PermitV2 tool-execution bounds, settle it via a Stripe Shared "
        "Payment Token, and bind the order id into the signed receipt and "
        "tamper-evident audit chain. No credits are minted and no real "
        "ledger entries are written. Idempotent on `intent_id`: a repeated "
        "intent replays the original order and never charges twice."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Checkout refused (total mismatch, budget, charge failure)",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Wallet access denied",
        },
        status.HTTP_409_CONFLICT: {
            "description": (
                "Intent id already used with a different request, or the "
                "same intent is still being processed"
            ),
        },
    },
)
async def acp_checkout(
    request: ACPCheckoutRequest,
    sponsor_wallet_id: str = Query(
        ..., description="Sponsor wallet that issues the checkout permit"
    ),
    agent_wallet_id: str = Query(
        ..., description="Agent wallet the checkout is bound to (permit subject)"
    ),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    # The agent wallet is the permit subject — the wallet whose budget the
    # checkout encumbers — so it is the tenant boundary for this endpoint.
    _require_wallet_access(auth, agent_wallet_id)
    # The named sponsor becomes the permit's issuer. A wallet key must not be
    # able to attribute issuance to an arbitrary sponsor: the agent wallet has
    # to actually sit under it in the funding hierarchy. Mirrors POST
    # /v1/permits' subject/issuer hierarchy check, inverted for the
    # subject-side caller. Bootstrap admins are unrestricted.
    if not auth.is_bootstrap_admin and not await money.is_wallet_or_descendant(
        agent_wallet_id, sponsor_wallet_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "sponsor_wallet_access_denied",
                "message": (
                    "The agent wallet must be funded by the named sponsor wallet."
                ),
            },
        )
    endpoint = "/v1/billing/acp/checkout"
    # Governance metadata below must never include the spt_token: it is
    # persisted into the signed audit trail. The request_id is derived from
    # the SAME acp-{intent_id} order key the bridge indexes its settlement
    # events under (collision-safe digest past the audit column's 100-char
    # cap — never a plain truncation), so one checkout's governance and
    # settlement events share one request_id.
    governance_request_id = audit_request_id(f"acp-{request.intent_id}")
    try:
        result = await get_acp_commerce_adapter().execute_checkout(
            request,
            sponsor_wallet_id=sponsor_wallet_id,
            agent_wallet_id=agent_wallet_id,
            key_id=auth.key_id,
        )
    except ACPBridgeError as exc:
        await _record_billing_governance(
            event="billing.acp_checkout",
            auth=auth,
            wallet_id=agent_wallet_id,
            service_category="acp_checkout",
            endpoint=endpoint,
            request_id=governance_request_id,
            ok=False,
            error=exc.reason,
            metadata={
                "intent_id": request.intent_id,
                "merchant_domain": request.merchant_domain,
                "client_total": request.client_total,
            },
        )
        # Conflict/retry conditions are 409, not 400: the request itself is
        # well-formed, the intent id is just contended or already bound to a
        # different body.
        status_code = (
            status.HTTP_409_CONFLICT
            if exc.reason in {"acp_intent_conflict", "acp_intent_in_progress"}
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"error": exc.reason, "message": str(exc)},
        )
    await _record_billing_governance(
        event="billing.acp_checkout",
        auth=auth,
        wallet_id=agent_wallet_id,
        service_category="acp_checkout",
        endpoint=endpoint,
        request_id=governance_request_id,
        ok=True,
        metadata={
            "intent_id": request.intent_id,
            "order_id": result.order_id,
            "merchant_domain": request.merchant_domain,
            "permit_id": result.permit_id,
            "receipt_id": result.receipt_id,
            "derived_total": result.derived_total,
        },
    )
    return result


# --- Pricing ---


@router.get(
    "/pricing",
    response_model=PricingTableResponse,
    summary="Get pricing table",
)
async def get_pricing(
    api_key: str = Depends(verify_api_key),
    money: AgentMoney = Depends(get_agent_money),
):
    from datetime import datetime, timezone

    # Read the configured rate per request so the advertised conversion always
    # matches the one Stripe settlement mints credits at. The exact field is
    # derived from the Decimal rather than from the float, so a non-round rate
    # is advertised without binary-float noise.
    exchange_rate = get_settings().EXCHANGE_RATE
    return PricingTableResponse(
        pricing=money.get_pricing_table(),
        exchange_rate=float(exchange_rate),
        exchange_rate_exact=str(exchange_rate),
        last_updated=datetime.now(timezone.utc),
    )


# --- Arbitrage ---


@expansion_router.get(
    "/arbitrage",
    response_model=ArbitrageReport,
    summary="Get arbitrage report",
)
async def get_arbitrage_report(
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    """Operator profitability across wallets for the preceding 24 hours."""
    auth.require_bootstrap_admin()
    return await money.get_arbitrage_report()


# --- Alerts ---


@expansion_router.get(
    "/alerts",
    response_model=AlertListResponse,
    summary="Get billing alerts",
)
async def get_alerts(
    wallet_id: str | None = Query(None),
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    wallet_filter: str | None
    if wallet_id:
        _require_wallet_access(auth, wallet_id)
        wallet_filter = wallet_id
    else:
        wallet_filter = None if auth.is_bootstrap_admin else auth.wallet_id
    alerts = await money.get_alerts(wallet_filter)
    unacknowledged = sum(1 for a in alerts if not a.acknowledged)
    return AlertListResponse(
        alerts=alerts,
        total=len(alerts),
        unacknowledged=unacknowledged,
    )


@expansion_router.post(
    "/services",
    response_model=ServiceRegistration,
    status_code=status.HTTP_201_CREATED,
    summary="Register a billable service",
    description=(
        "Register a new service in the marketplace that agents can discover "
        "and pay for."
    ),
)
async def register_service(
    request: RegisterServiceRequest,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    """
    Register a billable service in the agent marketplace.

    The service will be discoverable by other agents via the services endpoint.
    Charges for this service will be credited to the owner's wallet.
    """
    if auth.wallet_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "wallet_identity_required",
                "message": ("Service registration requires a wallet-scoped API key."),
            },
        )
    try:
        registration = await money.register_service(
            owner_wallet_id=auth.wallet_id,
            name=request.name,
            description=request.description,
            category=request.category,
            credits_per_unit=Decimal(str(request.credits_per_unit)),
            unit_name=request.unit_name,
            mcp_manifest=request.mcp_manifest,
        )
        return registration
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "service_registration_error", "message": str(e)},
        )


@expansion_router.get(
    "/services",
    summary="List available services",
    description="List all registered billable services in the marketplace.",
)
async def list_services(
    category: ServiceCategory | None = Query(None),
    active_only: bool = Query(True, description="Only return active services"),
    api_key: str = Depends(verify_api_key),
    money: AgentMoney = Depends(get_agent_money),
):
    """List all services, optionally filtered by category."""
    services = await money.list_services(category=category, active_only=active_only)
    return {"services": services, "total": len(services)}


@expansion_router.get(
    "/wallets/{wallet_id}/velocity",
    summary="Get spend velocity status",
    description="Get current spend velocity metrics for a wallet.",
)
async def get_velocity_status(
    wallet_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get current velocity status including hourly/daily spend vs limits."""
    from ..services.velocity_monitor import get_velocity_monitor

    _require_wallet_access(auth, wallet_id)
    monitor = get_velocity_monitor()
    return await monitor.get_velocity_status(wallet_id)


# --- Dry-Run Sandbox Endpoints ---


class CreateDryRunSessionRequest(BaseModel):
    """Start a dry-run session for simulating billing operations."""

    wallet_id: str = Field(..., description="Wallet to simulate charges against")


class DryRunSessionResponse(ExactDecimalFieldsMixin):
    """Response when creating a dry-run session."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "real_balance": "real_balance_exact",
        "virtual_balance": "virtual_balance_exact",
    }

    session_id: str
    wallet_id: str
    real_balance: float
    real_balance_exact: str | None = None
    virtual_balance: float
    virtual_balance_exact: str | None = None
    created_at: str
    expires_in_seconds: int = 900


class SimulatedChargeRequest(BaseModel):
    """Simulate a charge without affecting real balance."""

    wallet_id: str = Field(..., description="Wallet being simulated")
    service: ServiceCategory = Field(..., description="Service category to simulate")
    units: float = Field(
        default=1.0,
        # Same constraints as the real charge query parameter, for the same
        # reasons and one extra. The session-based branch of simulate_charge
        # calls ShadowLedger.simulate_charge directly and never reaches
        # BillingEngine.charge, so the engine's guard does not cover it at
        # all; the session-less branch does reach the engine, where the guard
        # raises ValueError and surfaces as a 500 rather than a 422. Refusing
        # here fixes both: one clean rejection at the boundary, before either
        # branch is chosen.
        gt=0,
        allow_inf_nan=False,
        description="Number of units",
    )
    description: str | None = Field(None, description="Optional description")
    dry_run_session_id: str | None = Field(
        None,
        description="Session ID for session-based simulation",
    )


class SimulatedChargeResponse(ExactDecimalFieldsMixin):
    """Result of a simulated charge."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "credits_would_charge": "credits_would_charge_exact",
        "simulated_balance_before": "simulated_balance_before_exact",
        "simulated_balance_after": "simulated_balance_after_exact",
    }

    dry_run: bool = True
    session_id: str
    wallet_id: str
    service_category: str
    units: float
    credits_would_charge: float
    credits_would_charge_exact: str | None = None
    simulated_balance_before: float
    simulated_balance_before_exact: str | None = None
    simulated_balance_after: float
    simulated_balance_after_exact: str | None = None
    would_succeed: bool
    reason: str | None = None


@expansion_router.post(
    "/dry-run/session",
    response_model=DryRunSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a dry-run session",
    description=(
        "Create a new dry-run session for simulating billing operations. "
        "All charges simulated within this session use a virtual balance "
        "derived from the wallet's real balance minus simulated charges. "
        "Sessions expire after 15 minutes.\n\n"
        "Use this to:\n"
        "- Test if a multi-step workflow fits within your budget\n"
        "- Estimate total cost before committing to execution\n"
        "- Plan complex agent workflows safely"
    ),
)
async def create_dry_run_session(
    request: CreateDryRunSessionRequest,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    """Start a new dry-run session for the specified wallet."""
    _require_wallet_access(auth, request.wallet_id)
    wallet = await money.get_wallet(request.wallet_id)
    if not wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "wallet_not_found",
                "message": f"Wallet {request.wallet_id} not found",
            },
        )

    shadow_ledger = get_shadow_ledger()
    session = await shadow_ledger.create_session(
        wallet_id=request.wallet_id,
        real_balance=Decimal(str(wallet.balance)),
    )

    return DryRunSessionResponse(
        session_id=session.session_id,
        wallet_id=session.wallet_id,
        real_balance=float(session.real_balance),
        virtual_balance=float(session.virtual_balance),
        created_at=session.created_at.isoformat(),
        expires_in_seconds=900,
    )


@expansion_router.get(
    "/dry-run/session/{session_id}",
    summary="Get dry-run session status",
    description="Retrieve current state of a dry-run session.",
)
async def get_dry_run_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get the current state of a dry-run session."""
    shadow_ledger = get_shadow_ledger()
    session = await _load_owned_dry_run_session(shadow_ledger, session_id, auth)
    return {
        "session_id": session.session_id,
        "wallet_id": session.wallet_id,
        "real_balance": float(session.real_balance),
        "virtual_balance": float(session.virtual_balance),
        "total_simulated": float(session.total_simulated),
        "charge_count": len(session.simulated_charges),
        "charges": [
            {
                "charge_id": c.charge_id,
                "service_category": c.service_category,
                "units": c.units,
                "credits": float(c.credits),
                "description": c.description,
            }
            for c in session.simulated_charges
        ],
        "created_at": session.created_at.isoformat(),
    }


@expansion_router.delete(
    "/dry-run/session/{session_id}",
    summary="End a dry-run session",
    description=(
        "End a dry-run session and get a summary of all simulated charges. "
        "This does NOT affect the real wallet - it's purely informational."
    ),
)
async def end_dry_run_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """End a dry-run session and return summary."""
    shadow_ledger = get_shadow_ledger()
    await _load_owned_dry_run_session(shadow_ledger, session_id, auth)
    summary = await shadow_ledger.end_session(session_id)
    if summary is None:
        # Session existed above but is gone by the time we tried to end it
        # (e.g. concurrent request already ended/expired it).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "session_not_found",
                "message": f"Session {session_id} not found",
            },
        )

    return {
        "session_id": summary.session_id,
        "wallet_id": summary.wallet_id,
        "created_at": summary.created_at.isoformat(),
        "ended_at": summary.ended_at.isoformat(),
        "total_simulated_credits": float(summary.total_simulated_credits),
        "charge_count": summary.charge_count,
        "real_balance": float(summary.real_balance),
        "virtual_balance_after": float(summary.virtual_balance_after),
        "charges": summary.simulated_charges,
    }


@expansion_router.post(
    "/dry-run/session/{session_id}/commit",
    summary="Commit sandbox session to billing",
    description=(
        "Commit all simulated charges to real billing. "
        "This applies all sandbox charges to the wallet's real balance. "
        "Use this after reviewing the simulation results and deciding to proceed."
    ),
)
async def commit_dry_run_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    """
    Commit a sandbox session to real billing.

    Applies all simulated charges to the real wallet.
    The session is ended after committing.
    """
    shadow_ledger = get_shadow_ledger()
    await _load_owned_dry_run_session(shadow_ledger, session_id, auth)
    result = await shadow_ledger.commit_session(session_id, money)
    if not result.wallet_id:
        # The session was there for the ownership check above and gone by the
        # time we claimed it. ``end_dry_run_session`` already 404s on the same
        # window; without this, commit answers 200 with ``success: false`` and
        # a caller reading only the status code records a commit that never
        # happened.
        #
        # Keyed on the empty ``wallet_id`` rather than on ``success``, because
        # ``commit_session`` also reports ``success: false`` when the charges
        # themselves fail (insufficient funds, say). That is a real answer
        # about a real session and must stay a 200 — 404-ing it would claim
        # the session never existed.
        raise _session_not_found(session_id)

    return {
        "session_id": result.session_id,
        "wallet_id": result.wallet_id,
        "committed_charges": result.committed_charges,
        "total_credits_deducted": float(result.total_credits_deducted),
        "real_balance_before": float(result.real_balance_before),
        "real_balance_after": float(result.real_balance_after),
        "ledger_entries": result.ledger_entries,
        "success": result.success,
        "message": result.message,
    }


@expansion_router.post(
    "/dry-run/session/{session_id}/revert",
    summary="Revert sandbox session",
    description=(
        "Revert a sandbox session and discard all simulated charges. "
        "No changes are made to the real wallet. "
        "This is useful when the simulation shows the operation would fail "
        "or you're not ready to proceed."
    ),
)
async def revert_dry_run_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Revert a sandbox session.

    Discards all simulated charges without affecting the real wallet.
    The session is ended after reverting.
    """
    shadow_ledger = get_shadow_ledger()
    await _load_owned_dry_run_session(shadow_ledger, session_id, auth)
    result = await shadow_ledger.revert_session(session_id)
    if not result.wallet_id:
        # Same window as commit: present for the ownership check, gone before
        # the revert. A missing session is ``revert_session``'s only failure
        # mode, so an unset wallet id means exactly that.
        raise _session_not_found(session_id)

    return {
        "session_id": result.session_id,
        "wallet_id": result.wallet_id,
        "reverted": result.reverted,
        "message": result.message,
    }


@expansion_router.post(
    "/dry-run/charge",
    response_model=SimulatedChargeResponse,
    summary="Simulate a charge",
    description=(
        "Simulate a charge without affecting real balance or triggering "
        "velocity monitoring. "
        "Returns cost estimate and virtual balance impact.\n\n"
        "Options:\n"
        "- Use session_id for multi-step simulation (tracks cumulative cost)\n"
        "- Omit session_id for single-shot estimation"
    ),
)
async def simulate_charge(
    request: SimulatedChargeRequest,
    auth: AuthContext = Depends(get_auth_context),
    money: AgentMoney = Depends(get_agent_money),
):
    """Simulate a charge operation."""
    session_id = request.dry_run_session_id

    if session_id:
        shadow_ledger = get_shadow_ledger()
        session = await _load_owned_dry_run_session(shadow_ledger, session_id, auth)
        try:
            result = await shadow_ledger.simulate_charge(
                session_id=session_id,
                service_category=request.service,
                units=request.units,
                description=request.description or "",
                blocked_reason=(
                    "wallet_expired"
                    if await money.wallet_is_expired(session.wallet_id)
                    else None
                ),
            )
        except ValueError as exc:
            # The session was claimed by a terminal operation between the
            # ownership check and the simulation's write-back. Answered like
            # every other session-gone path here rather than escaping as a
            # 500 -- which is what the ``KeyError`` from the memory store used
            # to do.
            if "Session not found" not in str(exc):
                raise
            raise _session_not_found(session_id) from None
        await _record_billing_governance(
            event="billing.dry_run",
            auth=auth,
            wallet_id=result.wallet_id,
            service_category=result.service_category,
            endpoint="/v1/billing/dry-run/charge",
            request_id=session_id,
            estimated_cost=float(result.credits_would_charge),
            ok=result.would_succeed,
            error=None if result.would_succeed else result.reason,
            metadata={
                "session_id": session_id,
                "units": result.units,
                "simulated_balance_before": result.simulated_balance_before,
                "simulated_balance_after": result.simulated_balance_after,
            },
        )
        return SimulatedChargeResponse(
            dry_run=True,
            session_id=result.session_id,
            wallet_id=result.wallet_id,
            service_category=result.service_category,
            units=result.units,
            # Pass the Decimals through as-is (cast(float, ...) is a type-hint
            # only, zero runtime effect): ExactDecimalFieldsMixin derives the
            # *_exact string fields from these raw values before pydantic
            # coerces them to float for display. Pre-converting to float here
            # would make the "exact" fields derive from an already-rounded
            # float instead of the real Decimal.
            credits_would_charge=cast(float, result.credits_would_charge),
            simulated_balance_before=cast(float, result.simulated_balance_before),
            simulated_balance_after=cast(float, result.simulated_balance_after),
            would_succeed=result.would_succeed,
            reason=result.reason,
        )

    _require_wallet_access(auth, request.wallet_id)
    wallet = await money.get_wallet(request.wallet_id)
    if not wallet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "wallet_not_found",
                "message": f"Wallet {request.wallet_id} not found",
            },
        )

    charge_result = await money.charge(
        wallet_id=request.wallet_id,
        service_category=request.service,
        units=Decimal(str(request.units)),
        description=request.description or "",
        dry_run=True,
        dry_run_session_id=None,
    )

    # AgentMoney._dry_run_charge (which charge(dry_run=True) always delegates
    # to) is typed to return SimulatedChargeResult unconditionally -- there is
    # no code path here that returns anything else, so narrow to that type
    # directly instead of hasattr/getattr-with-defaults that can never fire.
    assert isinstance(charge_result, SimulatedChargeResult)

    await _record_billing_governance(
        event="billing.dry_run",
        auth=auth,
        wallet_id=charge_result.wallet_id,
        service_category=charge_result.service_category,
        endpoint="/v1/billing/dry-run/charge",
        estimated_cost=float(charge_result.credits_would_charge),
        ok=charge_result.would_succeed,
        error=None if charge_result.would_succeed else charge_result.reason,
        metadata={"units": charge_result.units},
    )
    return SimulatedChargeResponse(
        dry_run=True,
        session_id=charge_result.session_id,
        wallet_id=charge_result.wallet_id,
        service_category=charge_result.service_category,
        units=charge_result.units,
        # Pass Decimals through as-is (see the session-based branch above
        # for why): ExactDecimalFieldsMixin needs the raw Decimal, not an
        # already-rounded float, to populate the *_exact fields.
        credits_would_charge=cast(float, charge_result.credits_would_charge),
        simulated_balance_before=cast(float, charge_result.simulated_balance_before),
        simulated_balance_after=cast(float, charge_result.simulated_balance_after),
        would_succeed=charge_result.would_succeed,
        reason=charge_result.reason,
    )
