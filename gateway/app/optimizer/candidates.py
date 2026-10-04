from __future__ import annotations

from app.schemas.optimizer import OptimizerState


def get_candidate_actions(state: OptimizerState) -> list[dict]:
    # Placeholder adapter: in production this should merge MCP manifest + dry-run pricing.
    return state.task_context.get("candidate_actions", [])
