from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.optimizer.candidates import get_candidate_actions
from app.optimizer.planner import optimize_action_set
from app.core.auth import AuthContext, get_auth_context
from app.policy.decisions import evaluate_governed_action
from app.schemas.optimizer import OptimizerRequest, OptimizerResponse
from app.services.audit_log import record_audit_event
from app.services.policies import evaluate_wallet_policy

router = APIRouter(prefix="/v1/planner", tags=["optimizer"])


@router.post("/optimize", response_model=OptimizerResponse)
async def optimize_endpoint(
    req: OptimizerRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> OptimizerResponse:
    # Authenticate and enforce that the caller owns the wallet whose policy
    # bundles are read and whose audit chain is written below. Previously this
    # endpoint had no auth and trusted the client-supplied wallet_id, letting
    # an unauthenticated caller read another wallet's policy config and write
    # signed audit events attributed to that wallet.
    auth.require_wallet_access(req.state.wallet_id)
    candidates = get_candidate_actions(req.state)
    policy_rejected: list[dict] = []
    policy_ids: set[str] = set()
    allowed_candidates: list[dict] = []
    for action in candidates:
        service = action.get("service")
        service_key = service if isinstance(service, str) else ""
        evaluation = await evaluate_wallet_policy(
            wallet_id=req.state.wallet_id,
            tool_name=action.get("id"),
            service_category=action.get("category") or service,
            estimated_cost=action.get("credit_cost"),
            daily_spend_used=req.state.daily_spend_used,
            simulation=req.state.simulation_flags.get(service_key, False),
            risk_tier=req.state.task_context.get("tier"),
        )
        if evaluation.policy_id:
            policy_ids.add(evaluation.policy_id)
        if evaluation.allowed:
            allowed_candidates.append(action)
        else:
            policy_rejected.append(
                {
                    "id": action.get("id"),
                    "reason": evaluation.reason,
                    "policy_id": evaluation.policy_id,
                }
            )

    plan = optimize_action_set(req.state, allowed_candidates, req)
    if policy_rejected:
        plan["rejected_actions"] = policy_rejected + plan["rejected_actions"]
        plan["policy_reasons"].update(
            {item["id"]: item["reason"] for item in policy_rejected if item.get("id")}
        )
    request_id = request.headers.get("X-Request-ID") or req.state.request_id
    decision = evaluate_governed_action(
        auth=auth,
        wallet_id=req.state.wallet_id,
        action_type="planner.optimize",
        target="planner",
        estimated_cost=plan["totals"].get("cost"),
        request_id=request_id,
        allowed=plan["status"] != "Infeasible",
        reason="allowed" if plan["status"] != "Infeasible" else "infeasible",
    )
    audit_event = await record_audit_event(
        event="planner.optimize",
        wallet_id=req.state.wallet_id,
        tool="planner",
        endpoint="/v1/planner/optimize",
        auth_source=decision.auth_source,
        key_id=decision.key_id,
        policy_decision_id=decision.decision_id,
        request_id=request_id,
        ok=True,
        metadata={
            "status": plan["status"],
            "selected_count": len(plan["selected_actions"]),
            "rejected_count": len(plan["rejected_actions"]),
            "policy_reason": decision.reason,
            "policy_ids": sorted(policy_ids),
            "simulation_flags": req.state.simulation_flags,
            "constraint_margins": plan["constraint_margins"],
        },
    )
    plan["governance"] = {
        "request_id": request_id,
        "wallet_id": req.state.wallet_id,
        "policy_decision_id": decision.decision_id,
        "policy_ids": sorted(policy_ids),
        "audit_event_id": audit_event.event_id,
    }
    return OptimizerResponse(**plan)
