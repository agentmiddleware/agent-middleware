from __future__ import annotations

from app.schemas.optimizer import OptimizerState


def get_risk_budget(task_tier: str) -> float:
    return {"low": 0.05, "medium": 0.15, "high": 0.25}.get(task_tier, 0.10)


def is_admissible(
    action: dict,
    state: OptimizerState,
    require_real: bool,
) -> tuple[bool, str]:
    if not action.get("scope_allowed", True):
        return False, "scope_violation"
    service = action.get("service", "")
    if state.service_health.get(service, "healthy") != "healthy":
        return False, "service_unhealthy"
    if require_real and state.simulation_flags.get(service, False):
        return False, "simulation_only"
    return True, ""
