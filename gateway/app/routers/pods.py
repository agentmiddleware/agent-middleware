"""Pods: a named group of agent API keys under one shared budget.

Dormant trust surface (see app/main.py DORMANT_TRUST_ROUTERS and AGENTS.md's
customer-validation invariant): the mechanism is real and tested, but it is
a new core capability with no documented named-customer evidence yet, so it
is not mounted or advertised in production until ENABLE_PROOF_SURFACES is
turned on for a specific pilot. See docs/pods.md.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from ..core.auth import AuthContext, get_auth_context
from ..schemas.pods import (
    CreatePodRequest,
    CreatePodResponse,
    PodBudgetResponse,
    PodMemberResponse,
    PodMemberSummary,
)
from ..services.agent_money import InsufficientFundsError
from ..services.pods import (
    PodBudgetError,
    PodError,
    PodMemberSpec,
    PodNotFoundError,
    PodProvisioningFailedError,
    PodService,
    get_pod_service,
)

router = APIRouter(
    prefix="/v1/pods",
    tags=["Pods"],
    responses={
        401: {"description": "Missing API key"},
        403: {"description": "Bootstrap administrator access required"},
    },
)


@router.post(
    "",
    response_model=CreatePodResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pod: a shared-budget wallet plus N member keys, in one call",
    description=(
        "Bootstrap operator provisioning only. Creates one sponsor wallet "
        "holding the pod's shared budget, then provisions one agent wallet "
        "and one wallet-scoped API key per member from that budget, all in "
        "one database transaction: either every member is created, or (on "
        "any failure) the whole pod is rolled back and nothing persists. "
        "Also rejects up front, before opening that transaction, member "
        "budgets that exceed the pod total."
    ),
)
async def create_pod(
    request: CreatePodRequest,
    auth: AuthContext = Depends(get_auth_context),
    pods: PodService = Depends(get_pod_service),
) -> CreatePodResponse:
    # Provisioning agent keys from a shared budget is credit-allocation
    # authority, the same reason app.routers.billing.create_sponsor_wallet
    # is bootstrap-only: without this gate any valid wallet key could mint
    # itself an arbitrarily-budgeted pod of keys.
    auth.require_bootstrap_admin()

    member_specs = [
        PodMemberSpec(
            agent_id=member.agent_id,
            key_name=member.key_name,
            budget_credits=(
                Decimal(str(member.budget_credits))
                if member.budget_credits is not None
                else None
            ),
        )
        for member in request.members
    ]

    try:
        pod = await pods.create_pod(
            pod_name=request.pod_name,
            budget_credits=Decimal(str(request.budget_credits)),
            members=member_specs,
        )
    except PodBudgetError as exc:
        # Raised before create_pod opens its transaction, so nothing was
        # ever created — never wrapped in PodProvisioningFailedError.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "pod_budget_exceeded", "message": str(exc)},
        ) from exc
    except PodProvisioningFailedError as exc:
        # Everything past the budget pre-check runs inside create_pod's one
        # transaction, so any failure here is wrapped in this type. It
        # always means nothing from the call persisted (no sponsor wallet,
        # no member, no key) — never a partial pod. Insufficient funds mid-
        # provisioning (which pre-validated budgets should make unreachable
        # in practice, but a defense-in-depth guard inside the transaction
        # can still raise it) is reported as 402; anything else as 500.
        if isinstance(exc.original_error, InsufficientFundsError):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "error": "insufficient_funds",
                    "message": str(exc.original_error),
                    "failed_agent_id": exc.failed_agent_id,
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "pod_provisioning_failed",
                "message": str(exc),
                "failed_agent_id": exc.failed_agent_id,
            },
        ) from exc

    return CreatePodResponse(
        pod_id=pod.pod_id,
        pod_name=pod.pod_name,
        budget_credits=float(pod.budget_credits),
        budget_credits_exact=str(pod.budget_credits),
        members=[
            PodMemberResponse(
                agent_id=m.agent_id,
                wallet_id=m.wallet_id,
                budget_credits=float(m.budget_credits),
                key_id=m.key_id,
                key_prefix=m.key_prefix,
                api_key=m.api_key,
            )
            for m in pod.members
        ],
    )


@router.get(
    "/{pod_id}",
    response_model=PodBudgetResponse,
    summary="Get a pod's aggregate budget status",
    description=(
        "Bootstrap operator only. Returns the pod's total budget, what's "
        "still unallocated at the pod level, and each member's current "
        "balance. No key material is ever returned here."
    ),
)
async def get_pod(
    pod_id: str,
    auth: AuthContext = Depends(get_auth_context),
    pods: PodService = Depends(get_pod_service),
) -> PodBudgetResponse:
    auth.require_bootstrap_admin()

    try:
        summary = await pods.get_pod_budget(pod_id)
    except PodNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "pod_not_found", "message": str(exc)},
        ) from exc
    except PodError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_a_pod", "message": str(exc)},
        ) from exc

    return PodBudgetResponse(
        pod_id=summary["pod_id"],
        pod_name=summary["pod_name"],
        total_budget=float(summary["total_budget"]),
        remaining_at_pod=float(summary["remaining_at_pod"]),
        allocated_to_members=float(summary["allocated_to_members"]),
        members=[
            PodMemberSummary(
                agent_id=m["agent_id"],
                wallet_id=m["wallet_id"],
                status=m["status"],
                balance=float(m["balance"]),
                lifetime_debits=float(m["lifetime_debits"]),
            )
            for m in summary["members"]
        ],
    )
