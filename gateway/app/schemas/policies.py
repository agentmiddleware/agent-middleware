from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# policy_bundles stores both limits as Numeric(18, 8): ten integer digits, so
# a limit must stay below 1e10. allow_inf_nan=False refuses the Infinity/NaN
# literals Starlette's JSON parser accepts (ge=0 alone let +Infinity through).
_MAX_POLICY_LIMIT = 1e10
# The planner's task tiers (app/optimizer/policy.py). A policy's tier is a
# ceiling over the requested one (low < medium < high); any other spelling
# fails closed at evaluation time. The column is String(20).
RiskTier = Literal["low", "medium", "high"]

# Columns that are NOT NULL in policy_bundles. A patch may leave them out
# but may not set them to null; the list and limit fields keep null as
# "no restriction on this dimension".
_NOT_NULL_PATCH_FIELDS = (
    "name",
    "require_real_effects",
    "risk_tier",
    "human_approval_required",
    "is_active",
)


class PolicyBundleCreate(BaseModel):
    wallet_id: str
    name: str = Field(..., min_length=1, max_length=255)
    allowed_tools: list[str] | None = None
    allowed_service_categories: list[str] | None = None
    max_cost_per_action: float | None = Field(
        default=None, ge=0, lt=_MAX_POLICY_LIMIT, allow_inf_nan=False
    )
    daily_spend_limit: float | None = Field(
        default=None, ge=0, lt=_MAX_POLICY_LIMIT, allow_inf_nan=False
    )
    require_real_effects: bool = False
    risk_tier: RiskTier = "medium"
    human_approval_required: bool = False
    is_active: bool = True


class PolicyBundlePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_tools: list[str] | None = None
    allowed_service_categories: list[str] | None = None
    max_cost_per_action: float | None = Field(
        default=None, ge=0, lt=_MAX_POLICY_LIMIT, allow_inf_nan=False
    )
    daily_spend_limit: float | None = Field(
        default=None, ge=0, lt=_MAX_POLICY_LIMIT, allow_inf_nan=False
    )
    require_real_effects: bool | None = None
    risk_tier: RiskTier | None = None
    human_approval_required: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _refuse_null_for_required_fields(self) -> PolicyBundlePatch:
        nulled = [
            field
            for field in _NOT_NULL_PATCH_FIELDS
            if field in self.model_fields_set and getattr(self, field) is None
        ]
        if nulled:
            raise ValueError(f"fields cannot be null: {', '.join(nulled)}")
        return self


class PolicyBundleResponse(BaseModel):
    policy_id: str
    wallet_id: str
    name: str
    allowed_tools: list[str] | None
    allowed_service_categories: list[str] | None
    max_cost_per_action: float | None
    daily_spend_limit: float | None
    require_real_effects: bool
    risk_tier: str
    human_approval_required: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PolicyBundleListResponse(BaseModel):
    policies: list[PolicyBundleResponse]
    total: int
