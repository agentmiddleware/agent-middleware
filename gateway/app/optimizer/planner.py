from __future__ import annotations

from typing import Any

from app.optimizer.policy import get_risk_budget, is_admissible
from app.schemas.optimizer import OptimizerRequest, OptimizerState

# Selection is a deterministic greedy heuristic. An optional PuLP/CBC MILP
# branch used to sit in front of it, but `pulp` was never a declared
# dependency, so the import always failed and the solver never ran. Declaring
# it would add a solver capability the wedge has no customer evidence for
# (WEDGE.md freezes the planner; docs/PROOF_SURFACES.md prefers deleting
# unused scaffolding), so the dead branch is gone and this module now does
# exactly what it has always done at runtime.


def _score(action: dict[str, Any], lambdas: dict[str, float]) -> float:
    return (
        action.get("expected_value", 0.0) * action.get("reliability", 1.0)
        - lambdas["cost"] * action.get("credit_cost", 0.0)
        - lambdas["latency"] * action.get("latency_ms", 0.0) / 1000.0
        - lambdas["risk"] * action.get("risk_score", 0.0)
    )


def _pack_response(
    status: str,
    selected: list[dict],
    rejected: list[dict],
    state: OptimizerState,
    risk_budget: float,
    lambdas: dict[str, float],
    policy_reasons: dict[str, str],
) -> dict:
    total_cost = sum(a.get("credit_cost", 0.0) for a in selected)
    total_latency = sum(a.get("latency_ms", 0.0) for a in selected)
    total_risk = sum(a.get("risk_score", 0.0) for a in selected)
    expected_utility = sum(_score(a, lambdas) for a in selected)
    return {
        "status": status,
        "selected_actions": selected,
        "rejected_actions": rejected,
        "policy_reasons": policy_reasons,
        "expected_utility": expected_utility,
        "totals": {
            "cost": total_cost,
            "latency_ms": total_latency,
            "risk": total_risk,
        },
        "constraint_margins": {
            "budget_left": state.remaining_budget - total_cost,
            "latency_left": state.slo_window_seconds * 1000 - total_latency,
            "risk_left": risk_budget - total_risk,
        },
    }


def _policy_reasons(rejected: list[dict]) -> dict[str, str]:
    return {item["id"]: item["reason"] for item in rejected if item.get("id")}


def _individual_constraint_rejections(
    candidates: list[dict],
    selected: list[dict],
    state: OptimizerState,
    risk_budget: float,
) -> list[dict]:
    selected_ids = {action.get("id") for action in selected}
    latency_budget = state.slo_window_seconds * 1000

    rejected: list[dict] = []
    for action in candidates:
        if action.get("id") in selected_ids:
            continue
        if action.get("credit_cost", 0.0) > state.remaining_budget:
            rejected.append({"id": action.get("id"), "reason": "budget_exceeded"})
        elif action.get("latency_ms", 0.0) > latency_budget:
            rejected.append(
                {"id": action.get("id"), "reason": "latency_budget_exceeded"}
            )
        elif action.get("risk_score", 0.0) > risk_budget:
            rejected.append({"id": action.get("id"), "reason": "risk_budget_exceeded"})
    return rejected


def _greedy_heuristic(
    candidates: list[dict],
    state: OptimizerState,
    risk_budget: float,
    max_actions: int,
    lambdas: dict[str, float],
) -> list[dict]:
    scored = sorted(candidates, key=lambda a: _score(a, lambdas), reverse=True)
    selected: list[dict] = []
    total_cost = 0.0
    total_latency = 0.0
    total_risk = 0.0
    for action in scored:
        c = action.get("credit_cost", 0.0)
        l = action.get("latency_ms", 0.0)
        r = action.get("risk_score", 0.0)
        if total_cost + c > state.remaining_budget:
            continue
        if total_latency + l > state.slo_window_seconds * 1000:
            continue
        if total_risk + r > risk_budget:
            continue
        selected.append(action)
        total_cost += c
        total_latency += l
        total_risk += r
        if len(selected) >= max_actions:
            break
    return selected


def optimize_action_set(
    state: OptimizerState, candidates: list[dict], req: OptimizerRequest
) -> dict:
    lambdas = {"cost": 0.8, "latency": 0.3, "risk": 2.0}
    if req.objective_overrides:
        for k in lambdas:
            if k in req.objective_overrides:
                lambdas[k] = float(req.objective_overrides[k])

    risk_budget = get_risk_budget(state.task_context.get("tier", "medium"))
    admissible: list[dict] = []
    rejected: list[dict] = []

    for action in candidates:
        ok, reason = is_admissible(action, state, req.require_real_effects)
        if ok:
            admissible.append(action)
        else:
            rejected.append({"id": action.get("id"), "reason": reason})

    if not admissible:
        return _pack_response(
            "Infeasible",
            [],
            rejected,
            state,
            risk_budget,
            lambdas,
            _policy_reasons(rejected),
        )

    max_actions = 5 if req.max_actions is None else req.max_actions

    selected = _greedy_heuristic(admissible, state, risk_budget, max_actions, lambdas)
    constraint_rejected = _individual_constraint_rejections(
        admissible, selected, state, risk_budget
    )
    all_rejected = rejected + constraint_rejected
    status = "HeuristicFallback" if selected else "Infeasible"
    return _pack_response(
        status,
        selected,
        all_rejected,
        state,
        risk_budget,
        lambdas,
        _policy_reasons(all_rejected),
    )
