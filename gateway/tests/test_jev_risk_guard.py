"""Advisory routing, evidence, privacy and failures; all Jev calls are mocked."""

import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import DuplicateGuardMode, Settings, get_settings
from app.core.time import utc_now
from app.main import app
from app.policy import jev_guard
from app.routers import mcp
from app.schemas.billing import ServiceCategory
from app.services.audit_log import list_audit_events
from app.services.human_approval import ApprovalCheck
from app.services.receipts import get_receipt_service
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "jev-echo"
REAL_POST = jev_guard._post


def _response(*, injected=0.0):
    answers = {}
    for name, question in jev_guard.QUESTIONS.items():
        kind = question["type"]
        if kind == "noul":
            answers[name] = {
                "type": kind,
                "noul": injected if name == "injected_instructions" else 0.0,
            }
        elif kind == "choice":
            answers[name] = {
                "type": kind,
                "choice": "no_purpose_given",
                "probabilities": {
                    "carries_out": 0.0,
                    "different_action": 0.0,
                    "no_purpose_given": 1.0,
                },
            }
        else:
            answers[name] = {"type": kind, "score": 0.0}
    return {
        "model": "jev-1.13.0-test",
        "answers": answers,
        "usage": {"input_tokens": 1000},
    }


@pytest.fixture(autouse=True)
def mock_jev(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "JEV_RISK_GUARD", DuplicateGuardMode.OFF)
    monkeypatch.setattr(settings, "JEV_RISK_GUARD_TIERS", "medium,high")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", SecretStr("jev-test-placeholder"))
    fake = SimpleNamespace(calls=[], body=_response(), failure=None)

    async def post(**kwargs):
        fake.calls.append(copy.deepcopy(kwargs["payload"]))
        if fake.failure == "timeout":
            await asyncio.sleep(0.1)
        if fake.failure == "exception":
            raise RuntimeError("secret exception text must never be logged")
        if fake.failure == "cancelled":
            raise asyncio.CancelledError
        if fake.failure == "http_500":
            return httpx.Response(500, text="private vendor error")
        if fake.failure == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json=fake.body)

    async def no_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in these tests")

    monkeypatch.setattr(jev_guard, "_post", post)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    return fake


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def registered_tool():
    executions = []

    def echo(message="hello"):
        executions.append(message)
        return {"message": message}

    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Jev Echo",
        description="A reversible local echo",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2.0,
    )
    yield executions
    registry.unregister_local(TOOL)


async def _evaluate(**kwargs):
    return await jev_guard.evaluate_jev_guard(
        **{
            "tool_name": TOOL,
            "tool_description": "Echo a message",
            "service_category": "agent_comms",
            "risk_tier": "medium",
            "permit_scope": "Echo one message",
            "requires_human_approval": False,
            "arguments": {"message": "hello"},
            **kwargs,
        }
    )


async def _setup(
    client,
    *,
    tier=None,
    policy_approval=False,
    allowed_tools=None,
    permit_approval=False,
):
    agent = await provision_agent_wallet(client)
    if tier is not None or policy_approval or allowed_tools is not None:
        response = await client.post(
            "/v1/policies",
            json={
                "wallet_id": agent["agent_wallet_id"],
                "name": "Guard policy",
                "risk_tier": tier or "medium",
                "human_approval_required": policy_approval,
                "allowed_tools": allowed_tools,
            },
            headers=BOOTSTRAP_HEADERS,
        )
        assert response.status_code == 201, response.text
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        requires_human_approval=permit_approval,
    )
    return agent, permit


async def _invoke(client, agent, permit, key="jev-invoke", *, headers=None):
    return await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={
            "name": TOOL,
            "arguments": {"message": "hello"},
            "mcp_context": {
                "wallet_id": agent["agent_wallet_id"],
                "permit_id": permit["permit_id"] if permit else None,
                "idempotency_key": key,
            },
        },
        headers=headers or agent["agent_headers"],
    )


async def _assert_metadata(receipt, wallet_id, *, verdict, status="ok"):
    guard = receipt["constraints_evaluated"]["jev_risk_guard"]
    assert guard["verdict"] == verdict
    assert guard["status"] == status
    assert guard["advisory"] is True
    if status == "ok":
        assert guard["model"] == "jev-1.13.0-test"
        assert guard["model_requested"] == "jev-1.13.0"
    events = await list_audit_events(wallet_id=wallet_id, limit=100)
    event = next(
        event for event in events if event.event_id == receipt["audit_event_id"]
    )
    assert event.metadata["jev_risk_guard"] == guard
    valid, reason, _ = await get_receipt_service().verify_receipt(receipt["receipt_id"])
    assert valid, reason
    return guard


def test_defaults_and_invalid_modes(monkeypatch):
    monkeypatch.delenv("JEV_RISK_GUARD", raising=False)
    settings = Settings(_env_file=None)
    assert settings.JEV_RISK_GUARD is DuplicateGuardMode.OFF
    assert settings.JEV_RISK_GUARD_MODEL == "jev-1.13.0"
    assert settings.JEV_RISK_GUARD_TIMEOUT_SECONDS == 1.5
    assert settings.JEV_RISK_GUARD_TIERS == "medium,high"
    for field in ("JEV_RISK_GUARD", "MCP_UPSTREAM_DUPLICATE_GUARD"):
        for value in ("enforced", "LOG", "", " enforce "):
            with pytest.raises(ValidationError):
                Settings(_env_file=None, **{field: value})


@pytest.mark.anyio
async def test_off_has_zero_calls_and_no_metadata(
    client, clean_database, registered_tool, mock_jev, monkeypatch
):
    from app.services import receipts

    lookup = AsyncMock(side_effect=AssertionError("Off mode must not load Jev metadata"))
    monkeypatch.setattr(receipts, "load_jev_guard_metadata", lookup)
    monkeypatch.setattr(mcp, "load_jev_guard_metadata", lookup, raising=False)
    assert (await _evaluate()).status == "off"
    agent, permit = await _setup(client)
    response = await _invoke(client, agent, permit)
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert "jev_risk_guard" not in receipt["constraints_evaluated"]
    events = await list_audit_events(wallet_id=agent["agent_wallet_id"], limit=100)
    assert all("jev_risk_guard" not in event.metadata for event in events)
    assert mock_jev.calls == []
    assert len(registered_tool) == 1
    lookup.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode,injected,verdict", [("log", 0.95, "escalate"), ("enforce", 0.0, "pass")]
)
async def test_success_metadata_and_replay(
    client,
    clean_database,
    registered_tool,
    mock_jev,
    monkeypatch,
    mode,
    injected,
    verdict,
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode(mode))
    mock_jev.body = _response(injected=injected)
    agent, permit = await _setup(client, tier="medium")
    first = await _invoke(client, agent, permit)
    assert first.status_code == 200, first.text
    guard = await _assert_metadata(
        first.json()["receipt"], agent["agent_wallet_id"], verdict=verdict
    )
    assert guard["mode"] == mode
    mock_jev.body = _response(injected=1 - injected)
    replay = await _invoke(client, agent, permit)
    assert replay.json() == first.json()
    assert len(mock_jev.calls) == len(registered_tool) == 1
    state = mock_jev.calls[0]["state"]
    assert state["tool"]["declared_risk_tier"] == "medium"
    assert state["permit"]["scope_description"]["scopes"] == permit["scopes"]


@pytest.mark.anyio
@pytest.mark.parametrize("approval_status", ["pending", "rejected", "approved"])
async def test_enforce_uses_existing_approval_gate(
    client, clean_database, registered_tool, mock_jev, monkeypatch, approval_status
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    mock_jev.body = _response(injected=0.9)
    calls = []

    async def ensure_approval(**kwargs):
        calls.append(kwargs)
        return ApprovalCheck(
            status=approval_status,
            approval_id="jev-test-approval",
            sentinel_action_id=None,
            simulated=True,
            decided_by=None,
            reason=None,
            expires_at=utc_now() + timedelta(minutes=5),
        )

    monkeypatch.setattr(
        mcp,
        "get_human_approval_service",
        lambda: SimpleNamespace(ensure_approval=ensure_approval),
    )
    agent, permit = await _setup(client)
    assert permit["requires_human_approval"] is False
    response = await _invoke(client, agent, permit)
    assert len(calls) == len(mock_jev.calls) == 1
    assert calls[0]["permit_id"] == permit["permit_id"]
    if approval_status == "pending":
        assert response.status_code == 202, response.text
        assert response.json()["detail"]["error"] == "human_approval_pending"
        assert registered_tool == []
        # Pending frees the record, but the verdict must survive that deletion.
        mock_jev.body = _response(injected=0.0)
        retry = await _invoke(client, agent, permit)
        assert retry.status_code == 202
        assert len(calls) == 2
        assert len(mock_jev.calls) == 1
        assert registered_tool == []
        events = await list_audit_events(wallet_id=agent["agent_wallet_id"], limit=100)
        assert len([event for event in events if event.event == "jev.risk_guard"]) == 1
        pending = [event for event in events if event.error == "human_approval_pending"]
        assert len(pending) == 2
        assert all(
            event.metadata["jev_risk_guard"]["verdict"] == "escalate"
            for event in pending
        )
    elif approval_status == "rejected":
        assert response.status_code == 403, response.text
        receipt = response.json()["detail"]["receipt"]
        assert receipt["reason_code"] == "human_approval_rejected"
        await _assert_metadata(receipt, agent["agent_wallet_id"], verdict="escalate")
        assert (await _invoke(client, agent, permit)).json() == response.json()
        assert len(calls) == len(mock_jev.calls) == 1
        assert registered_tool == []
    else:
        assert response.status_code == 200, response.text
        receipt = response.json()["receipt"]
        assert receipt["approval_id"] == "jev-test-approval"
        await _assert_metadata(receipt, agent["agent_wallet_id"], verdict="escalate")
        assert len(registered_tool) == 1


@pytest.mark.anyio
async def test_log_pending_retry_reuses_verdict(
    client, clean_database, registered_tool, mock_jev, monkeypatch
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    mock_jev.body = _response(injected=0.9)
    approval_calls = []

    async def ensure_approval(**kwargs):
        approval_calls.append(kwargs)
        return ApprovalCheck(
            status="pending" if len(approval_calls) == 1 else "approved",
            approval_id="jev-log-approval",
            sentinel_action_id=None,
            simulated=True,
            decided_by=None,
            reason=None,
            expires_at=utc_now() + timedelta(minutes=5),
        )

    monkeypatch.setattr(
        mcp,
        "get_human_approval_service",
        lambda: SimpleNamespace(ensure_approval=ensure_approval),
    )
    agent, permit = await _setup(client, permit_approval=True)
    first = await _invoke(client, agent, permit)
    assert first.status_code == 202, first.text
    mock_jev.body = _response(injected=0.0)
    retry = await _invoke(client, agent, permit)
    assert retry.status_code == 200, retry.text
    guard = await _assert_metadata(
        retry.json()["receipt"], agent["agent_wallet_id"], verdict="escalate"
    )
    events = await list_audit_events(wallet_id=agent["agent_wallet_id"], limit=100)
    checkpoints = [event for event in events if event.event == "jev.risk_guard"]
    assert len(checkpoints) == len(mock_jev.calls) == len(registered_tool) == 1
    assert checkpoints[0].metadata["jev_risk_guard"] == guard
    assert len(approval_calls) == 2


@pytest.mark.anyio
async def test_different_idempotency_key_gets_new_advice(
    client, clean_database, registered_tool, mock_jev, monkeypatch
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    agent, permit = await _setup(client)
    first = await _invoke(client, agent, permit, key="jev-first")
    assert first.status_code == 200, first.text
    await _assert_metadata(first.json()["receipt"], agent["agent_wallet_id"], verdict="pass")
    mock_jev.body = _response(injected=0.9)
    second = await _invoke(client, agent, permit, key="jev-second")
    assert second.status_code == 200, second.text
    await _assert_metadata(
        second.json()["receipt"], agent["agent_wallet_id"], verdict="escalate"
    )
    events = await list_audit_events(wallet_id=agent["agent_wallet_id"], limit=100)
    checkpoints = [event for event in events if event.event == "jev.risk_guard"]
    assert len(checkpoints) == len(mock_jev.calls) == len(registered_tool) == 2
    assert checkpoints[0].event_id != checkpoints[1].event_id


@pytest.mark.anyio
@pytest.mark.parametrize("conflict", ["integrity", "audit_intent"])
async def test_advisory_insert_race_uses_winning_escalation(
    client, clean_database, registered_tool, mock_jev, monkeypatch, conflict
):
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    original = mcp.record_audit_event

    async def racing_insert(**kwargs):
        if kwargs["event"] == "jev.risk_guard":
            winner = copy.deepcopy(kwargs)
            winner["metadata"]["jev_risk_guard"].update(
                verdict="escalate", reasons=["outside_scope"]
            )
            await original(**winner)
            if conflict == "integrity":
                raise IntegrityError("concurrent advisory insert", {}, Exception())
        return await original(**kwargs)

    approval = ApprovalCheck(
        status="pending",
        approval_id="jev-race-approval",
        sentinel_action_id=None,
        simulated=True,
        decided_by=None,
        reason=None,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    ensure_approval = AsyncMock(return_value=approval)
    monkeypatch.setattr(mcp, "record_audit_event", racing_insert)
    monkeypatch.setattr(
        mcp,
        "get_human_approval_service",
        lambda: SimpleNamespace(ensure_approval=ensure_approval),
    )
    agent, permit = await _setup(client)
    response = await _invoke(client, agent, permit)
    assert response.status_code == 202, response.text
    assert (await _invoke(client, agent, permit)).status_code == 202
    events = await list_audit_events(wallet_id=agent["agent_wallet_id"], limit=100)
    assert len([event for event in events if event.event == "jev.risk_guard"]) == 1
    pending = [event for event in events if event.error == "human_approval_pending"]
    assert len(pending) == ensure_approval.await_count == 2
    assert all(
        event.metadata["jev_risk_guard"]["verdict"] == "escalate"
        for event in pending
    )
    assert len(mock_jev.calls) == 1
    assert registered_tool == []


def test_advisory_identity_is_bounded_and_scoped():
    from app.services.jev_guard_metadata import jev_audit_id

    identity = jev_audit_id("wallet", "/mcp/invoke", "key")
    assert len(identity) <= 50
    assert identity == jev_audit_id("wallet", "/mcp/invoke", "key")
    assert len({
        identity,
        jev_audit_id("other-wallet", "/mcp/invoke", "key"),
        jev_audit_id("wallet", "/other-endpoint", "key"),
        jev_audit_id("wallet", "/mcp/invoke", "other-key"),
        jev_audit_id("wallet/mcp", "/invoke", "key"),
    }) == 5


@pytest.mark.anyio
async def test_enforce_ungoverned_denies(
    client, clean_database, registered_tool, mock_jev, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    monkeypatch.setattr(settings, "TRUST_MODE_ENABLED", False)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_UNPERMITTED_MCP", True)
    mock_jev.body = _response(injected=0.9)
    agent = await provision_agent_wallet(client)
    response = await _invoke(client, agent, None)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "jev_risk_review_required"
    assert registered_tool == []
    assert len(mock_jev.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("denial", ["tool_not_allowed", "human_approval_required"])
async def test_deterministic_policy_denial_never_calls_jev(
    client, clean_database, registered_tool, mock_jev, monkeypatch, denial
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    agent, permit = await _setup(
        client,
        policy_approval=denial == "human_approval_required",
        allowed_tools=["another-tool"] if denial == "tool_not_allowed" else None,
    )
    response = await _invoke(client, agent, permit)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == denial
    assert mock_jev.calls == registered_tool == []


@pytest.mark.anyio
async def test_cross_wallet_denial_never_calls_jev(
    client, clean_database, registered_tool, mock_jev, monkeypatch
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    agent, permit = await _setup(client)
    outsider = await provision_agent_wallet(client)
    response = await _invoke(client, agent, permit, headers=outsider["agent_headers"])
    assert response.status_code == 403
    assert mock_jev.calls == registered_tool == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,status",
    [
        ("timeout", "timeout"),
        ("http_500", "http_500"),
        ("json", "bad_response"),
        ("exception", "connection"),
        ("cancelled", "cancelled"),
        ("missing_key", "missing_key"),
    ],
)
async def test_failures_fail_open_with_metadata(
    client,
    clean_database,
    registered_tool,
    mock_jev,
    monkeypatch,
    caplog,
    failure,
    status,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    monkeypatch.setattr(
        settings,
        "JEV_RISK_GUARD_TIMEOUT_SECONDS",
        0.001 if failure == "timeout" else 1.5,
    )
    mock_jev.failure = failure
    if failure == "missing_key":
        monkeypatch.setattr(settings, "TYPESAFE_API_KEY", SecretStr(""))
    agent, permit = await _setup(client)
    response = await _invoke(client, agent, permit)
    assert response.status_code == 200, response.text
    await _assert_metadata(
        response.json()["receipt"],
        agent["agent_wallet_id"],
        verdict="skipped",
        status=f"unavailable:{status}",
    )
    assert len(registered_tool) == 1
    assert "secret exception text" not in caplog.text
    messages = [
        record.message for record in caplog.records if record.name == jev_guard.__name__
    ]
    assert messages == [f"unavailable:{status}"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tier,count,status",
    [
        ("low", 0, "skipped_tier"),
        ("medium", 1, "ok"),
        ("high", 1, "ok"),
        (None, 1, "ok"),
    ],
)
async def test_policy_tier_selection(
    client, clean_database, registered_tool, mock_jev, monkeypatch, tier, count, status
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    agent, permit = await _setup(client, tier=tier)
    response = await _invoke(client, agent, permit)
    assert response.status_code == 200, response.text
    assert (
        response.json()["receipt"]["constraints_evaluated"]["jev_risk_guard"]["status"]
        == status
    )
    assert len(mock_jev.calls) == count
    if count:
        assert mock_jev.calls[0]["state"]["tool"]["declared_risk_tier"] == tier


@pytest.mark.anyio
async def test_state_redaction_truncation_and_bound(mock_jev, monkeypatch):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    secret = "ghp_" + "Z" * 24
    arguments = {
        "message": "Readable words. " * 100,
        "nested": {
            "password": "short-private-value",
            "apiKey": "private",
            "note": f"token=abc {secret}",
        },
        "private_key": "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
        "api_key_copy": "jev-test-placeholder",
    }
    original = copy.deepcopy(arguments)
    verdict = await _evaluate(
        arguments=arguments,
        agent_purpose="Echo a message",
        tool_description=f"Echo {secret}",
    )
    assert verdict.status == "ok"
    state = mock_jev.calls[0]["state"]
    serialized = json.dumps(state)
    assert len(serialized) <= jev_guard.MAX_STATE_CHARS
    for value in (secret, "short-private-value", "jev-test-placeholder", "token=abc"):
        assert value not in serialized
    assert len(state["arguments"]["message"]) == 300
    assert state["arguments"]["nested"]["password"] == "[secret]"
    assert state["agent_purpose"] == "Echo a message"
    assert arguments == original
    await _evaluate(
        arguments={f"field-{i}": ["Unicode 😀 " * 100] * 100 for i in range(100)}
    )
    assert len(json.dumps(mock_jev.calls[-1]["state"])) <= jev_guard.MAX_STATE_CHARS
    await _evaluate(
        tool_name="😀" * 500,
        tool_description="😀" * 500,
        service_category="😀" * 500,
        agent_purpose="😀" * 500,
    )
    assert len(json.dumps(mock_jev.calls[-1]["state"])) <= jev_guard.MAX_STATE_CHARS


@pytest.mark.anyio
async def test_reversed_choice_averaging(mock_jev, monkeypatch):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    for name, mismatch in [("purpose_mismatch", 0.9), ("purpose_mismatch_rev", 0.3)]:
        mock_jev.body["answers"][name] = {
            "type": "choice",
            "choice": "different_action",
            "probabilities": {
                "carries_out": 1 - mismatch,
                "different_action": mismatch,
                "no_purpose_given": 0.0,
            },
        }
    result = await _evaluate()
    assert result.verdict == "pass"  # average exactly 0.60, threshold is strict >
    assert result.answers["purpose_mismatch_averaged"][
        "different_action"
    ] == pytest.approx(0.6)
    questions = mock_jev.calls[0]["questions"]
    assert list(questions["purpose_mismatch"]["criteria"]) == list(
        reversed(questions["purpose_mismatch_rev"]["criteria"])
    )
    mock_jev.body["answers"]["purpose_mismatch_rev"]["probabilities"].update(
        carries_out=0.69, different_action=0.31
    )
    assert (await _evaluate()).reasons == ["purpose_mismatch"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,value,tier,approval,reason",
    [
        ("injected_instructions", 0.699, "medium", False, None),
        ("injected_instructions", 0.70, "medium", False, "injected_instructions"),
        ("outside_scope", 0.699, "medium", False, None),
        ("outside_scope", 0.70, "medium", False, "outside_scope"),
        ("risk_understated", 0.799, "medium", False, None),
        ("risk_understated", 0.80, "medium", False, "risk_understated"),
        ("risk_understated", 0.80, "medium", True, None),
        ("blast_radius", 2.49, "low", False, None),
        ("blast_radius", 2.5, "low", False, "blast_radius_low_tier"),
        ("blast_radius", 3.0, "high", False, None),
        ("irreversible", 0.80, "low", False, None),
        ("moves_money", 0.80, "low", False, None),
        ("external_message", 0.5, "medium", False, None),
        ("bulk_or_destructive", 0.5, "medium", False, None),
    ],
)
async def test_thresholds(mock_jev, monkeypatch, field, value, tier, approval, reason):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD_TIERS", "low,medium,high")
    mock_jev.body["answers"][field]["score" if field == "blast_radius" else "noul"] = (
        value
    )
    result = await _evaluate(risk_tier=tier, requires_human_approval=approval)
    assert result.status == "ok"
    assert result.reasons == ([reason] if reason else [])
    assert result.verdict == ("escalate" if reason else "pass")
    if field != "blast_radius" and 0.3 <= value <= 0.7:
        assert field in result.answers["uncertain"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "irreversible,money,tier,escalated",
    [
        (0.8, 0.8, "low", True),
        (0.799, 0.8, "low", False),
        (0.8, 0.799, "low", False),
        (0.8, 0.8, "medium", False),
    ],
)
async def test_irreversible_money_low_tier(
    mock_jev, monkeypatch, irreversible, money, tier, escalated
):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD_TIERS", "low,medium,high")
    mock_jev.body["answers"]["irreversible"]["noul"] = irreversible
    mock_jev.body["answers"]["moves_money"]["noul"] = money
    result = await _evaluate(risk_tier=tier)
    assert result.reasons == (["irreversible_money_low_tier"] if escalated else [])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "malformation",
    [
        "missing_answer",
        "nan",
        "out_of_range",
        "wrong_type",
        "bad_choice",
        "missing_usage",
        "bad_model",
    ],
)
async def test_malformed_answers_are_unavailable(mock_jev, monkeypatch, malformation):
    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.ENFORCE)
    if malformation == "missing_answer":
        del mock_jev.body["answers"]["outside_scope"]
    elif malformation == "nan":
        # JSON cannot carry NaN through httpx's strict serializer; use infinity-free wrong shape below.
        mock_jev.body["answers"]["outside_scope"]["noul"] = "NaN"
    elif malformation == "out_of_range":
        mock_jev.body["answers"]["outside_scope"]["noul"] = 1.1
    elif malformation == "wrong_type":
        mock_jev.body["answers"]["outside_scope"]["noul"] = True
    elif malformation == "bad_choice":
        mock_jev.body["answers"]["purpose_mismatch"]["probabilities"] = {
            "invented": 1.0
        }
    elif malformation == "missing_usage":
        del mock_jev.body["usage"]
    else:
        mock_jev.body["model"] = None
    result = await _evaluate()
    assert result.verdict == "skipped"
    assert result.status == "unavailable:bad_response"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "upstream_mode",
    [
        "success",
        "pre_dispatch_failure",
        "returned_error",
        "delivery_uncertain",
        "response_rejected",
    ],
)
async def test_upstream_terminal_metadata(
    client, clean_database, mock_jev, monkeypatch, upstream_mode
):
    from tests.test_mcp_upstream_governed import (
        FakeUpstreamExecutor,
        _register_upstream,
    )

    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    mock_jev.body = _response(injected=0.9)
    executor = FakeUpstreamExecutor(upstream_mode)
    _register_upstream(TOOL, executor)
    try:
        agent, permit = await _setup(client)
        response = await _invoke(client, agent, permit)
        payload = response.json()
        receipt = (
            payload["receipt"]
            if upstream_mode == "success"
            else payload["detail"]["receipt"]
        )
        await _assert_metadata(receipt, agent["agent_wallet_id"], verdict="escalate")
        replay = await _invoke(client, agent, permit)
        assert replay.json() == payload
        assert len(mock_jev.calls) == 1
        assert executor.dispatch_count == (
            0 if upstream_mode == "pre_dispatch_failure" else 1
        )
    finally:
        get_service_registry().unregister_local(TOOL)


@pytest.mark.anyio
@pytest.mark.parametrize("reconcile_mode", [DuplicateGuardMode.LOG, DuplicateGuardMode.OFF])
async def test_upstream_crash_reconciliation_retains_advice(
    client, clean_database, mock_jev, monkeypatch, reconcile_mode
):
    from sqlalchemy import select
    from app.db.database import get_session_factory
    from app.db.models import McpDispatchAttemptModel
    from app.services.mcp_dispatch_reconciliation import (
        get_mcp_dispatch_reconciliation_service,
    )
    from tests.test_mcp_upstream_governed import (
        FakeUpstreamExecutor,
        _register_upstream,
    )

    monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    executor = FakeUpstreamExecutor("success")
    _register_upstream(TOOL, executor)
    original = mcp._audit_mcp_invocation

    async def crash_before_terminal_audit(**kwargs):
        if kwargs.get("dispatch_attempt") is not None and kwargs["ok"]:
            raise RuntimeError("synthetic finalization crash")
        return await original(**kwargs)

    try:
        agent, permit = await _setup(client)
        monkeypatch.setattr(mcp, "_audit_mcp_invocation", crash_before_terminal_audit)
        crashed = await _invoke(client, agent, permit)
        assert crashed.json()["isError"] is True
        assert crashed.json()["receipt"] is None
        async with get_session_factory()() as session:
            attempt = (
                await session.execute(
                    select(McpDispatchAttemptModel).where(
                        McpDispatchAttemptModel.wallet_id == agent["agent_wallet_id"],
                    )
                )
            ).scalar_one()
        monkeypatch.setattr(get_settings(), "JEV_RISK_GUARD", reconcile_mode)
        if reconcile_mode == DuplicateGuardMode.OFF:
            from app.services import mcp_dispatch_reconciliation, receipts

            lookup = AsyncMock(side_effect=AssertionError("Off mode must not load Jev metadata"))
            monkeypatch.setattr(mcp_dispatch_reconciliation, "load_jev_guard_metadata", lookup)
            monkeypatch.setattr(receipts, "load_jev_guard_metadata", lookup)
        await get_mcp_dispatch_reconciliation_service().reconcile_attempt(
            attempt.attempt_id
        )
        replay = await _invoke(client, agent, permit)
        assert replay.status_code == 200, replay.text
        if reconcile_mode == DuplicateGuardMode.OFF:
            assert "jev_risk_guard" not in replay.json()["receipt"]["constraints_evaluated"]
            lookup.assert_not_awaited()
        else:
            await _assert_metadata(
                replay.json()["receipt"], agent["agent_wallet_id"], verdict="pass"
            )
        assert len(mock_jev.calls) == executor.dispatch_count == 1
    finally:
        get_service_registry().unregister_local(TOOL)


@pytest.mark.anyio
async def test_http_contract_uses_bearer_auth(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=_response())

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    settings = get_settings()
    monkeypatch.setattr(settings, "JEV_RISK_GUARD", DuplicateGuardMode.LOG)
    monkeypatch.setattr(settings, "TYPESAFE_BASE_URL", "https://vendor.test/")
    monkeypatch.setattr(jev_guard, "_post", REAL_POST)
    monkeypatch.setattr(jev_guard.httpx, "AsyncClient", client)
    result = await _evaluate()
    assert result.status == "ok"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://vendor.test/v1/systemone"
    assert request.headers["authorization"] == "Bearer jev-test-placeholder"
    body = json.loads(request.content)
    assert set(body) == {"state", "model", "questions"}
    assert body["model"] == "jev-1.13.0"


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_metadata", ["not json", "[]"])
async def test_receipt_metadata_loader_is_wallet_scoped_and_tolerates_legacy_data(
    clean_database,
    mock_jev,
    invalid_metadata,
):
    from app.db.database import get_session_factory
    from app.db.models import ControlPlaneAuditEventModel
    from app.services.audit_log import record_audit_event
    from app.services.jev_guard_metadata import load_jev_guard_metadata

    guard = {"verdict": "pass", "advisory": True}
    event = await record_audit_event(
        event="mcp.invoke",
        wallet_id="jev-wallet",
        tool=TOOL,
        metadata={"jev_risk_guard": guard},
    )
    assert await load_jev_guard_metadata(event.event_id, "jev-wallet") == guard
    assert await load_jev_guard_metadata(event.event_id, "other-wallet") is None
    async with get_session_factory()() as session:
        row = await session.get(ControlPlaneAuditEventModel, event.event_id)
        row.metadata_json = invalid_metadata
        await session.commit()
    assert await load_jev_guard_metadata(event.event_id, "jev-wallet") is None
    assert mock_jev.calls == []
