import json
import math

import app.optimizer.planner as planner
import app.routers.planner as planner_router
from app.schemas.optimizer import OptimizerRequest, OptimizerResponse, OptimizerState
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.services.audit_log import list_audit_events


def _state(tier="low", service_health=None):
    return OptimizerState(
        wallet_id="w1",
        agent_id="a1",
        task_id="t1",
        request_id="r1",
        wallet_balance=100,
        daily_spend_used=0,
        daily_limit=100,
        rate_limit_headroom=1.0,
        service_health=service_health or {"svc1": "healthy"},
        simulation_flags={"svc1": False},
        auth_scope=["invoke"],
        task_context={"tier": tier},
        remaining_budget=3,
        slo_window_seconds=1,
    )


def test_infeasible_when_service_unhealthy():
    state = _state(service_health={"svc1": "down"})
    req = OptimizerRequest(state=state)
    candidates = [
        {
            "id": "x",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 1,
            "reliability": 1.0,
        }
    ]
    out = planner.optimize_action_set(state, candidates, req)
    assert out["status"] == "Infeasible"
    assert out["rejected_actions"] == [{"id": "x", "reason": "service_unhealthy"}]
    assert out["policy_reasons"] == {"x": "service_unhealthy"}


def test_greedy_selection_respects_max_actions():
    state = _state(tier="high")
    req = OptimizerRequest(state=state, max_actions=2)
    candidates = [
        {
            "id": "x1",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 4,
            "reliability": 1.0,
        },
        {
            "id": "x2",
            "service": "svc1",
            "credit_cost": 2,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 3,
            "reliability": 1.0,
        },
    ]
    out = planner.optimize_action_set(state, candidates, req)
    assert out["status"] == "HeuristicFallback"
    assert len(out["selected_actions"]) <= 2


def test_risk_budget_enforced_by_tier():
    state = _state(tier="low")
    req = OptimizerRequest(state=state)
    candidates = [
        {
            "id": "safe",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 10,
            "risk_score": 0.02,
            "expected_value": 1,
            "reliability": 1.0,
        },
        {
            "id": "risky",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 10,
            "risk_score": 0.2,
            "expected_value": 10,
            "reliability": 1.0,
        },
    ]
    out = planner.optimize_action_set(state, candidates, req)
    ids = {x["id"] for x in out["selected_actions"]}
    assert "risky" not in ids
    rejected = {x["id"]: x["reason"] for x in out["rejected_actions"]}
    assert rejected["risky"] == "risk_budget_exceeded"
    assert out["policy_reasons"]["risky"] == "risk_budget_exceeded"


def test_feasible_unselected_candidate_is_not_policy_rejected():
    state = _state(tier="high")
    state.remaining_budget = 10
    req = OptimizerRequest(state=state, max_actions=1)
    # Net-positive utility: expected_value comfortably exceeds the cost penalty.
    # Kept net-positive because the scoring is shared with the ranking in
    # _greedy_heuristic, so an all-negative set would make which candidate wins
    # an artifact of sort order rather than of the constraint under test.
    candidates = [
        {
            "id": "winner",
            "service": "svc1",
            "credit_cost": 6,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 10,
            "reliability": 1.0,
        },
        {
            "id": "alternate",
            "service": "svc1",
            "credit_cost": 6,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 8,
            "reliability": 1.0,
        },
    ]

    out = planner.optimize_action_set(state, candidates, req)

    assert [action["id"] for action in out["selected_actions"]] == ["winner"]
    assert "alternate" not in {action["id"] for action in out["rejected_actions"]}
    assert "alternate" not in out["policy_reasons"]


def test_individual_budget_violation_maps_to_policy_reason():
    state = _state(tier="high")
    req = OptimizerRequest(state=state)
    candidates = [
        {
            "id": "too_expensive",
            "service": "svc1",
            "credit_cost": 4,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 10,
            "reliability": 1.0,
        },
    ]

    out = planner.optimize_action_set(state, candidates, req)

    assert out["status"] == "Infeasible"
    assert out["rejected_actions"] == [
        {"id": "too_expensive", "reason": "budget_exceeded"}
    ]
    assert out["policy_reasons"] == {"too_expensive": "budget_exceeded"}


def test_individual_latency_violation_maps_to_policy_reason():
    state = _state(tier="high")
    req = OptimizerRequest(state=state)
    candidates = [
        {
            "id": "too_slow",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 1001,
            "risk_score": 0.01,
            "expected_value": 10,
            "reliability": 1.0,
        },
    ]

    out = planner.optimize_action_set(state, candidates, req)

    assert out["status"] == "Infeasible"
    assert out["rejected_actions"] == [
        {"id": "too_slow", "reason": "latency_budget_exceeded"}
    ]
    assert out["policy_reasons"] == {"too_slow": "latency_budget_exceeded"}


def test_empty_selection_returns_infeasible_regression():
    state = _state(tier="high")
    req = OptimizerRequest(state=state)
    candidates = [
        {
            "id": "x1",
            "service": "svc1",
            "credit_cost": 10,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 4,
            "reliability": 1.0,
        },
    ]
    out = planner.optimize_action_set(state, candidates, req)
    assert out["selected_actions"] == []
    assert out["status"] == "Infeasible"


def test_emitted_statuses_match_declared_response_schema():
    """Selection is deterministically greedy, and the schema says only that.

    A PuLP/CBC MILP branch used to sit in front of the heuristic and was the
    only producer of the "Optimal" status, but `pulp` was never a declared
    dependency — the import always failed, so the branch never ran and
    "Optimal" was unreachable in every environment. Guard both halves: no
    solver hook reappears without its dependency, and every status the planner
    can emit validates against the wire schema.
    """
    assert not hasattr(planner, "pulp")

    state = _state(tier="high")
    req = OptimizerRequest(state=state, max_actions=2)
    affordable = planner.optimize_action_set(
        state,
        [
            {
                "id": "cheap",
                "service": "svc1",
                "credit_cost": 1,
                "latency_ms": 10,
                "risk_score": 0.01,
                "expected_value": 4,
                "reliability": 1.0,
            }
        ],
        req,
    )
    nothing_admissible = planner.optimize_action_set(state, [], req)

    assert affordable["status"] == "HeuristicFallback"
    assert nothing_admissible["status"] == "Infeasible"
    for payload in (affordable, nothing_admissible):
        OptimizerResponse.model_validate(payload)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_planner_endpoint_returns_governance_context_and_records_audit(
    client,
    clean_database,
):
    request_id = "req-planner-governance"
    payload = {
        "state": _state(tier="high").model_dump(),
        "max_actions": 1,
    }
    payload["state"]["request_id"] = request_id
    payload["state"]["wallet_id"] = "wallet-planner-governance"
    payload["state"]["task_context"]["candidate_actions"] = [
        {
            "id": "winner",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 4,
            "reliability": 1.0,
        },
        {
            "id": "too_slow",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 2000,
            "risk_score": 0.01,
            "expected_value": 10,
            "reliability": 1.0,
        },
    ]

    response = await client.post(
        "/v1/planner/optimize",
        json=payload,
        headers={"X-API-Key": "test-key", "X-Request-ID": request_id},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["governance"]["request_id"] == request_id
    assert data["governance"]["wallet_id"] == "wallet-planner-governance"
    assert data["governance"]["audit_event_id"].startswith("audit-")
    assert data["governance"]["policy_decision_id"].startswith("pol-")
    assert data["policy_reasons"] == {"too_slow": "latency_budget_exceeded"}

    events = await list_audit_events(request_id=request_id)
    assert len(events) == 1
    event = events[0]
    assert event.event == "planner.optimize"
    assert event.wallet_id == "wallet-planner-governance"
    assert event.tool == "planner"
    assert event.endpoint == "/v1/planner/optimize"
    assert event.ok is True
    assert event.policy_decision_id == data["governance"]["policy_decision_id"]
    assert event.metadata["status"] == data["status"]
    assert event.metadata["selected_count"] == 1
    assert event.metadata["rejected_count"] == 1


# --- Non-finite inputs -------------------------------------------------------
#
# Python's JSON parser accepts the NaN / Infinity / -Infinity tokens, and a
# bare ``float`` field (even with ``ge``) lets Infinity through. A NaN or
# infinite objective weight or budget then poisons expected_utility and the
# constraint margins, so these must be refused at the boundary with a 422.

_NON_FINITE = [float("inf"), float("-inf"), float("nan")]


def _endpoint_payload(**request_fields):
    payload = {"state": _state(tier="high").model_dump(), "max_actions": 1}
    payload["state"]["task_context"]["candidate_actions"] = [
        {
            "id": "winner",
            "service": "svc1",
            "credit_cost": 1,
            "latency_ms": 10,
            "risk_score": 0.01,
            "expected_value": 4,
            "reliability": 1.0,
        }
    ]
    payload.update(request_fields)
    return payload


@pytest.mark.proof
@pytest.mark.parametrize(
    "field",
    [
        "wallet_balance",
        "daily_spend_used",
        "daily_limit",
        "rate_limit_headroom",
        "remaining_budget",
    ],
)
@pytest.mark.parametrize("value", _NON_FINITE)
def test_optimizer_state_rejects_non_finite_floats(field, value):
    payload = _state().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        OptimizerState(**payload)


@pytest.mark.proof
@pytest.mark.parametrize("value", _NON_FINITE)
def test_objective_overrides_reject_non_finite_weights(value):
    with pytest.raises(ValidationError):
        OptimizerRequest(state=_state(), objective_overrides={"cost": value})


_NON_FINITE_BODIES = [
    ({"cost": "NaN"}, {}),
    ({"risk": "-Infinity"}, {}),
    (None, {"wallet_balance": "Infinity"}),
    (None, {"remaining_budget": "Infinity"}),
]
_NON_FINITE_BODY_IDS = ["nan-cost", "neg-inf-risk", "inf-balance", "inf-budget"]


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize(
    "objective_overrides, state_fields", _NON_FINITE_BODIES, ids=_NON_FINITE_BODY_IDS
)
async def test_planner_endpoint_rejects_non_finite_inputs(
    client, clean_database, objective_overrides, state_fields
):
    # Lax float parsing turns the strings "NaN" / "Infinity" into non-finite
    # floats, so these reached the planner (200, poisoned utility) before.
    request_id = "req-planner-non-finite-string"
    payload = _endpoint_payload(objective_overrides=objective_overrides)
    payload["state"].update(state_fields, request_id=request_id)

    response = await client.post(
        "/v1/planner/optimize",
        json=payload,
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "finite_number"
    assert await list_audit_events(request_id=request_id) == []


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize(
    "objective_overrides, state_fields", _NON_FINITE_BODIES, ids=_NON_FINITE_BODY_IDS
)
async def test_planner_endpoint_never_plans_with_bare_non_finite_tokens(
    clean_database, monkeypatch, objective_overrides, state_fields
):
    """Bare NaN / Infinity JSON tokens never reach the planner or audit chain.

    httpx's json= refuses them, so the raw tokens a client could put on the
    wire are sent as content. FastAPI's default 422 handler echoes the input
    and cannot serialize a non-finite float, so that refusal currently
    surfaces as a 500 instead of a 422 (an app-wide handler issue, not the
    planner's); what this pins is that no plan is computed or audited.
    """
    calls: list = []
    original = planner_router.optimize_action_set

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(planner_router, "optimize_action_set", spy)
    request_id = "req-planner-non-finite-token"
    payload = _endpoint_payload(
        objective_overrides=objective_overrides
        and {k: float(v) for k, v in objective_overrides.items()}
    )
    payload["state"].update(
        {k: float(v) for k, v in state_fields.items()}, request_id=request_id
    )

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/v1/planner/optimize",
            content=json.dumps(payload),
            headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
        )

    assert response.status_code in (422, 500)
    assert calls == []
    assert await list_audit_events(request_id=request_id) == []


@pytest.mark.proof
@pytest.mark.anyio
async def test_planner_endpoint_accepts_finite_overrides(client, clean_database):
    payload = _endpoint_payload(
        objective_overrides={"cost": 0.5, "latency": 0.0, "risk": 1.5}
    )

    response = await client.post(
        "/v1/planner/optimize",
        json=payload,
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert [a["id"] for a in data["selected_actions"]] == ["winner"]
    assert math.isfinite(data["expected_utility"])
    assert data["expected_utility"] == pytest.approx(4 - 0.5 * 1 - 1.5 * 0.01)


# task_context.candidate_actions carries the per-action numbers the planner
# scores and budgets with. A NaN credit_cost passes every budget comparison,
# a -Infinity risk_score or latency_ms cancels that budget, and either one
# poisons expected_utility and the audited constraint margins.
_CANDIDATE_NUMBER_FIELDS = [
    "credit_cost",
    "latency_ms",
    "risk_score",
    "expected_value",
    "reliability",
]


@pytest.mark.proof
@pytest.mark.parametrize("field", _CANDIDATE_NUMBER_FIELDS)
@pytest.mark.parametrize("value", _NON_FINITE)
def test_task_context_rejects_non_finite_candidate_numbers(field, value):
    payload = _endpoint_payload()["state"]
    payload["task_context"]["candidate_actions"][0][field] = value
    with pytest.raises(ValidationError) as exc:
        OptimizerState(**payload)
    assert f"candidate_actions[0].{field}" in str(exc.value)


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize(
    "field, token",
    [
        ("credit_cost", "NaN"),
        ("risk_score", "-Infinity"),
        ("latency_ms", "-Infinity"),
        ("expected_value", "Infinity"),
        ("reliability", "NaN"),
    ],
)
async def test_planner_endpoint_never_plans_with_non_finite_candidates(
    clean_database, monkeypatch, field, token
):
    calls: list = []
    original = planner_router.optimize_action_set

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(planner_router, "optimize_action_set", spy)
    request_id = f"req-planner-non-finite-candidate-{field}"
    payload = _endpoint_payload()
    payload["state"]["request_id"] = request_id
    payload["state"]["task_context"]["candidate_actions"][0][field] = float(token)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/v1/planner/optimize",
            content=json.dumps(payload),
            headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
        )

    # Refused by the schema; as with the bare-token test above, FastAPI's
    # default 422 handler may surface that as a 500 because it echoes the
    # non-finite input. Either way nothing is planned or audited.
    assert response.status_code in (422, 500)
    assert calls == []
    assert await list_audit_events(request_id=request_id) == []
