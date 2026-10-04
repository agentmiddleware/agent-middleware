from app.optimizer.planner import optimize_action_set
from app.schemas.optimizer import OptimizerRequest, OptimizerState


def _state(**kwargs):
    base = dict(
        wallet_id="w1",
        agent_id="a1",
        task_id="t1",
        request_id="r1",
        wallet_balance=100,
        daily_spend_used=0,
        daily_limit=100,
        rate_limit_headroom=1.0,
        service_health={"svc1": "healthy", "svc2": "healthy"},
        simulation_flags={"svc1": False, "svc2": True},
        auth_scope=["invoke"],
        task_context={"tier": "medium"},
        remaining_budget=20,
        slo_window_seconds=2,
    )
    base.update(kwargs)
    return OptimizerState(**base)


def test_budget_latency_and_scope_constraints_hold():
    req = OptimizerRequest(state=_state())
    candidates = [
        {
            "id": "a",
            "service": "svc1",
            "expected_value": 10,
            "reliability": 1.0,
            "credit_cost": 15,
            "latency_ms": 1500,
            "risk_score": 0.05,
            "scope_allowed": True,
        },
        {
            "id": "b",
            "service": "svc1",
            "expected_value": 5,
            "reliability": 1.0,
            "credit_cost": 10,
            "latency_ms": 1000,
            "risk_score": 0.05,
            "scope_allowed": False,
        },
        {
            "id": "c",
            "service": "svc1",
            "expected_value": 9,
            "reliability": 1.0,
            "credit_cost": 8,
            "latency_ms": 700,
            "risk_score": 0.04,
            "scope_allowed": True,
        },
    ]
    out = optimize_action_set(req.state, candidates, req)
    assert out["status"] in {"Optimal", "HeuristicFallback"}
    ids = {x["id"] for x in out["selected_actions"]}
    assert "b" not in ids
    assert out["totals"]["cost"] <= req.state.remaining_budget
    assert out["totals"]["latency_ms"] <= req.state.slo_window_seconds * 1000


def test_require_real_effects_filters_simulation_only_actions():
    req = OptimizerRequest(state=_state(), require_real_effects=True)
    candidates = [
        {
            "id": "sim",
            "service": "svc2",
            "expected_value": 10,
            "reliability": 1.0,
            "credit_cost": 1,
            "latency_ms": 100,
            "risk_score": 0.01,
            "scope_allowed": True,
        },
    ]
    out = optimize_action_set(req.state, candidates, req)
    assert out["status"] == "Infeasible"
    assert out["rejected_actions"][0]["reason"] == "simulation_only"
