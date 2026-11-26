from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Python's JSON parser accepts NaN/Infinity tokens and ``ge`` alone lets
# Infinity through; either would poison expected_utility and the margins.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _first_non_finite_path(value: Any, root: str) -> str | None:
    """Path of the first NaN/Infinity float nested in ``value``, else None.

    Iterative so a deeply nested payload cannot exhaust the stack.
    """
    stack: list[tuple[Any, str]] = [(value, root)]
    while stack:
        item, path = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return path
        elif isinstance(item, dict):
            stack.extend((v, f"{path}.{k}") for k, v in item.items())
        elif isinstance(item, (list, tuple)):
            stack.extend((v, f"{path}[{i}]") for i, v in enumerate(item))
    return None


class OptimizerState(BaseModel):
    wallet_id: str
    agent_id: str
    task_id: str
    request_id: str
    wallet_balance: float = Field(..., ge=0, allow_inf_nan=False)
    daily_spend_used: float = Field(..., ge=0, allow_inf_nan=False)
    daily_limit: float = Field(..., ge=0, allow_inf_nan=False)
    rate_limit_headroom: float = Field(..., ge=0, le=1, allow_inf_nan=False)
    service_health: dict[str, Literal["healthy", "degraded", "down"]]
    simulation_flags: dict[str, bool]
    auth_scope: list[str]
    task_context: dict
    remaining_budget: float = Field(..., ge=0, allow_inf_nan=False)
    slo_window_seconds: int = Field(30, ge=1)

    @field_validator("task_context")
    @classmethod
    def _task_context_is_finite(cls, task_context: dict) -> dict:
        # task_context is untyped, but its candidate_actions carry the
        # credit_cost / latency_ms / risk_score / expected_value / reliability
        # the planner scores and budgets with. A NaN cost passes every budget
        # comparison and a -Infinity risk or latency cancels its budget, so
        # refuse non-finite numbers here like the typed fields above.
        path = _first_non_finite_path(task_context, "task_context")
        if path is not None:
            raise ValueError(f"{path} must be a finite number")
        return task_context


class OptimizerRequest(BaseModel):
    state: OptimizerState
    objective_overrides: Optional[dict[str, FiniteFloat]] = None
    max_actions: Optional[int] = Field(default=5, ge=1)
    require_real_effects: bool = False


class OptimizerResponse(BaseModel):
    # "Optimal" was only reachable through a MILP branch that never ran (its
    # solver was never a declared dependency). Selection is the deterministic
    # greedy heuristic, so these are the two statuses the planner can emit.
    status: Literal["HeuristicFallback", "Infeasible"]
    selected_actions: list[dict]
    rejected_actions: list[dict]
    policy_reasons: dict[str, str]
    expected_utility: float
    totals: dict[str, float]
    constraint_margins: dict[str, float]
    governance: Optional[dict] = None
