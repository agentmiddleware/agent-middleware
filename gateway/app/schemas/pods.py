"""Pods: a named group of agent API keys under one shared budget.

A pod is not a new wallet primitive. It is a thin, labeled composition of
the existing sponsor/agent wallet hierarchy (``app/services/agent_money.py``,
``app/services/wallet_engine.py``) plus wallet-scoped key issuance
(``app/services/api_key_service.py``): one sponsor wallet holds the pod's
shared budget, and each named member is an ordinary agent wallet with its
own key, provisioned from that sponsor in a single call.

This is the smallest real version of "an agent organization — pods of
specialized agents under one budget": no new ledger, no new auth model, no
new money movement semantics. See ``app/services/pods.py`` for the
orchestration and ``docs/pods.md`` for scope notes and what this
deliberately does not do yet (no department templates, no on-demand
provisioning trigger, no cross-pod budget sharing).
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from app.schemas.billing import MAX_STORABLE_AMOUNT
from pydantic import BaseModel, Field, field_validator

from .billing import ExactDecimalFieldsMixin

MAX_POD_MEMBERS = 50


class PodMemberRequest(BaseModel):
    """One member to provision inside a new pod."""

    agent_id: str = Field(
        default="",
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]*$",
        description="Identifier for this member's agent wallet. Generated when omitted.",
    )
    key_name: str = Field(default="pod-member", max_length=64)
    budget_credits: float | None = Field(
        default=None,
        ge=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description=(
            "This member's share of the pod budget. Omit to split the pod's "
            "total budget evenly across all members with no explicit share."
        ),
    )


class CreatePodRequest(BaseModel):
    """Provision a pod: one shared-budget sponsor wallet plus N member keys."""

    pod_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable pod name (e.g. 'research', 'outreach').",
        examples=["research"],
    )
    budget_credits: float = Field(
        ...,
        gt=0,
        lt=MAX_STORABLE_AMOUNT,
        allow_inf_nan=False,
        description="Total shared budget for the pod, in ecosystem credits.",
    )
    members: list[PodMemberRequest] = Field(
        ...,
        min_length=1,
        max_length=MAX_POD_MEMBERS,
        description="Members to provision under this pod's shared budget.",
    )

    @field_validator("members")
    @classmethod
    def _unique_nonempty_agent_ids(
        cls, members: list[PodMemberRequest]
    ) -> list[PodMemberRequest]:
        named = [m.agent_id for m in members if m.agent_id]
        if len(named) != len(set(named)):
            raise ValueError("member agent_id values must be unique when provided")
        return members


class PodMemberResponse(BaseModel):
    """One provisioned pod member. The API key is shown once, here."""

    agent_id: str
    wallet_id: str
    budget_credits: float
    key_id: str
    key_prefix: str
    api_key: str


class CreatePodResponse(ExactDecimalFieldsMixin):
    """A newly provisioned pod: its shared-budget wallet and its members."""

    _decimal_exact_fields: ClassVar[dict[str, str]] = {
        "budget_credits": "budget_credits_exact",
    }

    pod_id: str = Field(..., description="The pod's shared-budget sponsor wallet_id.")
    pod_name: str
    budget_credits: float
    budget_credits_exact: str | None = None
    members: list[PodMemberResponse]
    note: str = (
        "Each member's api_key is returned once, here, and is not "
        "recoverable afterward. Store it now."
    )


class PodMemberSummary(BaseModel):
    """A pod member as seen from the pod's budget summary (no secret material)."""

    agent_id: str
    wallet_id: str
    status: str
    balance: float
    lifetime_debits: float


class PodBudgetResponse(BaseModel):
    """Aggregate budget status for a pod, built on the swarm-budget query."""

    pod_id: str
    pod_name: str
    total_budget: float
    remaining_at_pod: float = Field(
        ..., description="Unallocated balance still sitting in the pod's own wallet."
    )
    allocated_to_members: float = Field(
        ..., description="Sum of budget_credits handed out to members so far."
    )
    members: list[PodMemberSummary]


def _round2(value: Decimal) -> float:
    return float(value)
