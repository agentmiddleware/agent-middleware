from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
import uuid

from app.core.auth import AuthContext


@dataclass(frozen=True)
class PolicyDecision:
    decision_id: str
    allowed: bool
    reason: str
    wallet_id: str
    tool_name: str
    auth_source: str
    key_id: str | None
    estimated_cost: float | None
    request_id: str | None

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return asdict(self)


def evaluate_tool_invocation(
    *,
    auth: AuthContext,
    wallet_id: str,
    tool_name: str,
    estimated_cost: float | None,
    request_id: str | None,
) -> PolicyDecision:
    if auth.is_bootstrap_admin or auth.wallet_id == wallet_id:
        return PolicyDecision(
            decision_id=f"pol-{uuid.uuid4().hex[:16]}",
            allowed=True,
            reason="allowed",
            wallet_id=wallet_id,
            tool_name=tool_name,
            auth_source=auth.source,
            key_id=auth.key_id,
            estimated_cost=estimated_cost,
            request_id=request_id,
        )

    return PolicyDecision(
        decision_id=f"pol-{uuid.uuid4().hex[:16]}",
        allowed=False,
        reason="wallet_access_denied",
        wallet_id=wallet_id,
        tool_name=tool_name,
        auth_source=auth.source,
        key_id=auth.key_id,
        estimated_cost=estimated_cost,
        request_id=request_id,
    )


def evaluate_governed_action(
    *,
    auth: AuthContext | None,
    wallet_id: str | None,
    action_type: str,
    target: str,
    estimated_cost: float | None = None,
    request_id: str | None = None,
    allowed: bool | None = None,
    reason: str | None = None,
) -> PolicyDecision:
    """Create the shared policy-shaped decision used by governed actions."""
    auth_source = (
        "bootstrap"
        if auth and auth.is_bootstrap_admin
        else (auth.source if auth else "anonymous")
    )
    key_id = auth.key_id if auth else None

    if allowed is None:
        allowed = bool(
            auth
            and wallet_id
            and (auth.is_bootstrap_admin or auth.wallet_id == wallet_id)
        )
    if reason is None:
        reason = "allowed" if allowed else "wallet_access_denied"

    return PolicyDecision(
        decision_id=f"pol-{uuid.uuid4().hex[:16]}",
        allowed=allowed,
        reason=reason,
        wallet_id=wallet_id or "",
        tool_name=target or action_type,
        auth_source=auth_source,
        key_id=key_id,
        estimated_cost=estimated_cost,
        request_id=request_id,
    )
