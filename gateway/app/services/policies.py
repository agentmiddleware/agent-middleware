from __future__ import annotations

from dataclasses import dataclass
from app.core.time import utc_now
from decimal import Decimal
import json
import logging
from typing import Any
import uuid

from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import PolicyBundleModel
from app.schemas.policies import (
    PolicyBundleCreate,
    PolicyBundlePatch,
    PolicyBundleResponse,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyEvaluation:
    allowed: bool
    reason: str
    policy_id: str | None
    evaluated_constraints: dict[str, Any]


class _CorruptPolicyListError(ValueError):
    """A list column that is present but is not a JSON array of strings."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"policy_constraint_corrupt:{field}")


def _decode_list_strict(value: str | None, *, field: str) -> list[str] | None:
    """Decode a stored allowlist column for enforcement.

    SQL NULL is the only "no restriction on this dimension". Anything else
    must be a JSON array of strings; a value that is present but undecodable,
    or the wrong shape, raises instead of reading as NULL, so a corrupted
    allowlist can never widen into "allow everything".
    """
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        raise _CorruptPolicyListError(field) from None
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise _CorruptPolicyListError(field)
    return decoded


def _decode_list(value: str | None, *, field: str, policy_id: str) -> list[str] | None:
    """Tolerant decode for reads: never raises, never reports corrupt as unset.

    A corrupt column reads back as ``[]`` (nothing allowed), not ``None``
    (unrestricted): consumers of the response model, such as the enterprise
    IGA bridge, enforce exactly what it says.
    """
    try:
        return _decode_list_strict(value, field=field)
    except _CorruptPolicyListError:
        logger.warning(
            "policy_constraint_corrupt policy_id=%s field=%s", policy_id, field
        )
        return []


def _encode_list(value: list[str] | None) -> str | None:
    return json.dumps(value) if value is not None else None


def _to_response(model: PolicyBundleModel) -> PolicyBundleResponse:
    return PolicyBundleResponse(
        policy_id=model.policy_id,
        wallet_id=model.wallet_id,
        name=model.name,
        allowed_tools=_decode_list(
            model.allowed_tools_json,
            field="allowed_tools",
            policy_id=model.policy_id,
        ),
        allowed_service_categories=_decode_list(
            model.allowed_service_categories_json,
            field="allowed_service_categories",
            policy_id=model.policy_id,
        ),
        max_cost_per_action=(
            float(model.max_cost_per_action)
            if model.max_cost_per_action is not None
            else None
        ),
        daily_spend_limit=(
            float(model.daily_spend_limit)
            if model.daily_spend_limit is not None
            else None
        ),
        require_real_effects=model.require_real_effects,
        risk_tier=model.risk_tier,
        human_approval_required=model.human_approval_required,
        is_active=model.is_active,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


async def create_policy_bundle(request: PolicyBundleCreate) -> PolicyBundleResponse:
    model = PolicyBundleModel(
        policy_id=f"polb-{uuid.uuid4().hex[:16]}",
        wallet_id=request.wallet_id,
        name=request.name,
        allowed_tools_json=_encode_list(request.allowed_tools),
        allowed_service_categories_json=_encode_list(
            request.allowed_service_categories
        ),
        max_cost_per_action=(
            Decimal(str(request.max_cost_per_action))
            if request.max_cost_per_action is not None
            else None
        ),
        daily_spend_limit=(
            Decimal(str(request.daily_spend_limit))
            if request.daily_spend_limit is not None
            else None
        ),
        require_real_effects=request.require_real_effects,
        risk_tier=request.risk_tier,
        human_approval_required=request.human_approval_required,
        is_active=request.is_active,
    )
    factory = get_session_factory()
    async with factory() as session:
        session.add(model)
        await session.commit()
        await session.refresh(model)
    return _to_response(model)


async def list_policy_bundles(
    wallet_id: str | None = None,
) -> list[PolicyBundleResponse]:
    # SQLModel fields are typed as plain Python types (not Mapped[...]), so mypy
    # sees `.created_at`/`==` comparisons as datetime/bool rather than SQLAlchemy
    # ColumnElement expressions. Root cause lives in app/db/models.py (out of scope here).
    stmt = select(PolicyBundleModel).order_by(PolicyBundleModel.created_at)  # type: ignore[arg-type]
    if wallet_id:
        stmt = stmt.where(PolicyBundleModel.wallet_id == wallet_id)  # type: ignore[arg-type]
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(stmt)
        return [_to_response(row) for row in result.scalars().all()]


async def get_policy_bundle(policy_id: str) -> PolicyBundleResponse | None:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(PolicyBundleModel, policy_id)
        return _to_response(row) if row else None


async def patch_policy_bundle(
    policy_id: str,
    patch: PolicyBundlePatch,
) -> PolicyBundleResponse | None:
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(PolicyBundleModel, policy_id)
        if not row:
            return None
        update = patch.model_dump(exclude_unset=True)
        for field, value in update.items():
            if field == "allowed_tools":
                row.allowed_tools_json = _encode_list(value)
            elif field == "allowed_service_categories":
                row.allowed_service_categories_json = _encode_list(value)
            elif field in {"max_cost_per_action", "daily_spend_limit"}:
                setattr(row, field, Decimal(str(value)) if value is not None else None)
            else:
                setattr(row, field, value)
        row.updated_at = utc_now()
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return _to_response(row)


async def wallet_human_approval_required(wallet_id: str) -> bool:
    """True when any active policy bundle for this wallet demands a human decision.

    Used by surfaces that mint permits on the caller's behalf (the standard
    MCP endpoint) to materialize the policy as a ``requires_human_approval``
    permit, so the demand is satisfied by the invoke-time approval gate
    instead of surfacing as an unsatisfiable denial.
    """
    stmt = (
        select(PolicyBundleModel.policy_id)  # type: ignore[call-overload]
        .where(
            PolicyBundleModel.wallet_id == wallet_id,  # type: ignore[arg-type]
            PolicyBundleModel.is_active == True,  # type: ignore[arg-type]  # noqa: E712
            PolicyBundleModel.human_approval_required == True,  # type: ignore[arg-type]  # noqa: E712
        )
        .limit(1)
    )
    factory = get_session_factory()
    async with factory() as session:
        return (await session.execute(stmt)).first() is not None


def _as_decimal(value: float | int | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


async def evaluate_wallet_policy(
    *,
    wallet_id: str,
    tool_name: str | None = None,
    service_category: str | None = None,
    estimated_cost: float | Decimal | None = None,
    daily_spend_used: float | Decimal | None = None,
    simulation: bool | None = None,
    risk_tier: str | None = None,
    approval_gate_active: bool = False,
) -> PolicyEvaluation:
    """Evaluate a wallet's active policy bundles against one intended action.

    ``approval_gate_active`` declares that the invoke being evaluated already
    runs through the permit-level human-approval gate. A policy's
    ``human_approval_required`` constraint is then satisfied by that gate
    (recorded in ``evaluated_constraints``) instead of denying — every OTHER
    constraint in the bundle is still enforced. Callers with no approval gate
    keep the fail-closed denial.
    """
    stmt = (
        select(PolicyBundleModel)
        .where(
            PolicyBundleModel.wallet_id == wallet_id,  # type: ignore[arg-type]
            PolicyBundleModel.is_active == True,  # type: ignore[arg-type]  # noqa: E712
        )
        .order_by(PolicyBundleModel.created_at)  # type: ignore[arg-type]
    )
    factory = get_session_factory()
    async with factory() as session:
        models = (await session.execute(stmt)).scalars().all()
    if not models:
        return PolicyEvaluation(True, "allowed", None, {"policy_count": 0})

    # Money comparisons are done in Decimal end-to-end; thresholds are stored as
    # Decimal and the incoming cost is normalized rather than compared as float.
    est = _as_decimal(estimated_cost)
    daily = _as_decimal(daily_spend_used)

    evaluated: list[dict[str, Any]] = []
    for policy in models:
        try:
            allowed_tools = _decode_list_strict(
                policy.allowed_tools_json, field="allowed_tools"
            )
            allowed_categories = _decode_list_strict(
                policy.allowed_service_categories_json,
                field="allowed_service_categories",
            )
        except _CorruptPolicyListError as exc:
            # Fail closed: an allowlist that cannot be read cannot be shown to
            # permit this action. The stored value is not echoed into the
            # evaluation, which lands in audit metadata.
            logger.warning(
                "policy_constraint_corrupt policy_id=%s field=%s",
                policy.policy_id,
                exc.field,
            )
            evaluated.append(
                {"policy_id": policy.policy_id, "corrupt_constraint": exc.field}
            )
            return PolicyEvaluation(
                False,
                "policy_constraint_corrupt",
                policy.policy_id,
                {"evaluated": evaluated},
            )
        constraints = {
            "policy_id": policy.policy_id,
            "allowed_tools": allowed_tools,
            "allowed_service_categories": allowed_categories,
            "max_cost_per_action": (
                float(policy.max_cost_per_action)
                if policy.max_cost_per_action is not None
                else None
            ),
            "daily_spend_limit": (
                float(policy.daily_spend_limit)
                if policy.daily_spend_limit is not None
                else None
            ),
            "require_real_effects": policy.require_real_effects,
            "risk_tier": policy.risk_tier,
            "human_approval_required": policy.human_approval_required,
        }
        evaluated.append(constraints)
        if policy.human_approval_required:
            if not approval_gate_active:
                return PolicyEvaluation(
                    False,
                    "human_approval_required",
                    policy.policy_id,
                    {"evaluated": evaluated},
                )
            constraints["human_approval_satisfied_by_gate"] = True
        if allowed_tools is not None and tool_name not in allowed_tools:
            return PolicyEvaluation(
                False, "tool_not_allowed", policy.policy_id, {"evaluated": evaluated}
            )
        if (
            allowed_categories is not None
            and service_category not in allowed_categories
        ):
            return PolicyEvaluation(
                False,
                "service_category_not_allowed",
                policy.policy_id,
                {"evaluated": evaluated},
            )
        if (
            policy.max_cost_per_action is not None
            and est is not None
            and est > policy.max_cost_per_action
        ):
            return PolicyEvaluation(
                False,
                "max_cost_per_action_exceeded",
                policy.policy_id,
                {"evaluated": evaluated},
            )
        if (
            policy.daily_spend_limit is not None
            and daily is not None
            and est is not None
            and daily + est > policy.daily_spend_limit
        ):
            return PolicyEvaluation(
                False,
                "daily_spend_limit_exceeded",
                policy.policy_id,
                {"evaluated": evaluated},
            )
        if policy.require_real_effects and simulation:
            return PolicyEvaluation(
                False,
                "real_effects_required",
                policy.policy_id,
                {"evaluated": evaluated},
            )
        if risk_tier is not None and policy.risk_tier != risk_tier:
            constraints["requested_risk_tier"] = risk_tier

    return PolicyEvaluation(
        True, "allowed", models[0].policy_id, {"evaluated": evaluated}
    )
