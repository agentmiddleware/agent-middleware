"""Data-free tests for the AMW V2 approval gate state machine."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (
    REPO_ROOT / "integrations" / "goose-agent-middleware" / "src",
    REPO_ROOT / "sdk" / "python" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from typing import Any  # noqa: E402

import pytest  # noqa: E402
from goose_amw import canonical as goose_canonical  # noqa: E402

from app.services.approval_gate import (  # noqa: E402
    ApprovalGate,
    ApprovalGateUnavailable,
    InMemoryApprovalStore,
)

SECRET = "test-state-secret"
CALLER = "caller-a"
TOOL = "tool-x"
SESSION = "session-1"
ARGS: dict[str, Any] = {}


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def receipts() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def gate(receipts: list[tuple[str, dict[str, Any]]], clock: _Clock) -> ApprovalGate:
    return ApprovalGate(
        secret=SECRET,
        store=InMemoryApprovalStore(),
        clock=clock,
        receipt_sink=lambda event, record: receipts.append((event, record)),
    )


def _pause(gate: ApprovalGate) -> Any:
    decision = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert decision.action == "PAUSE"
    assert decision.payload is not None
    return decision


def _retry(gate: ApprovalGate, state: str, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "caller": CALLER,
        "tool": TOOL,
        "arguments": ARGS,
        "session_id": SESSION,
        "client_supports_input_required": True,
        "request_state": state,
        "input_responses": [{"requestId": "r1", "approved": True}],
    }
    params.update(kwargs)
    return gate.evaluate(
        params.pop("caller"),
        params.pop("tool"),
        params.pop("arguments"),
        params.pop("session_id"),
        params.pop("client_supports_input_required"),
        **params,
    )


def test_first_call_pauses_with_input_required_payload(gate: ApprovalGate) -> None:
    decision = _pause(gate)
    assert decision.payload is not None
    assert decision.payload["resultType"] == "input_required"
    assert isinstance(decision.payload["inputRequests"], list)
    assert decision.payload["inputRequests"]
    assert isinstance(decision.payload["requestState"], str)
    assert decision.payload["requestState"]


def test_key_equals_goose_idempotency_key(gate: ApprovalGate) -> None:
    expected = goose_canonical.idempotency_key(
        caller=CALLER, tool=TOOL, arguments=ARGS, session_id=SESSION
    )
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, True, now=gate.clock())
    executed = _retry(gate, decision.payload["requestState"])
    assert executed.action == "EXECUTE"
    assert executed.idempotency_key == expected


def test_approved_retry_executes_exactly_once(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, True, now=gate.clock())
    first = _retry(gate, decision.payload["requestState"])
    assert first.action == "EXECUTE"
    second = _retry(gate, decision.payload["requestState"])
    assert second.action == "REJECT"
    assert second.reason == "replayed"
    assert [event for event, _ in receipts] == ["replayed"]


def test_pending_retry_repauses(gate: ApprovalGate) -> None:
    decision = _pause(gate)
    again = _retry(gate, decision.payload["requestState"])
    assert again.action == "PAUSE"
    assert again.payload is not None
    assert again.payload["resultType"] == "input_required"


def test_deny_rejects_and_receipts(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, False, now=gate.clock())
    rejected = _retry(gate, decision.payload["requestState"])
    assert rejected.action == "REJECT"
    assert rejected.reason == "denied"
    assert [event for event, _ in receipts] == ["denied"]


def test_expired_rejects_timeout_and_marks_expired(
    gate: ApprovalGate, clock: _Clock, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    clock.now += 3600 + 1
    rejected = _retry(gate, decision.payload["requestState"])
    assert rejected.action == "REJECT"
    assert rejected.reason == "timeout"
    assert decision.nonce is not None
    assert gate.store.get(decision.nonce) is not None
    assert gate.store.get(decision.nonce).status == "expired"  # type: ignore[union-attr]
    assert [event for event, _ in receipts] == ["timeout"]


def test_tampered_state_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    state: str = decision.payload["requestState"]
    tampered = ("A" if state[0] != "A" else "B") + state[1:]
    rejected = _retry(gate, tampered)
    assert rejected.action == "REJECT"
    assert rejected.reason == "tampered"
    assert [event for event, _ in receipts] == ["tampered"]


def test_unknown_nonce_rejects_tampered(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]], clock: _Clock
) -> None:
    decision = _pause(gate)
    fresh = ApprovalGate(
        secret=SECRET,
        store=InMemoryApprovalStore(),
        clock=clock,
        receipt_sink=lambda event, record: receipts.append((event, record)),
    )
    rejected = _retry(fresh, decision.payload["requestState"])
    assert rejected.action == "REJECT"
    assert rejected.reason == "tampered"


def test_unsupported_client_rejects_without_record(gate: ApprovalGate) -> None:
    decision = gate.evaluate(CALLER, TOOL, ARGS, SESSION, False)
    assert decision.action == "REJECT"
    assert decision.reason == "unsupported_client"
    assert gate.store.list_pending() == []


def test_args_mismatch_rejects(gate: ApprovalGate) -> None:
    decision = _pause(gate)
    rejected = _retry(gate, decision.payload["requestState"], arguments={"k": "v"})
    assert rejected.action == "REJECT"
    assert rejected.reason == "args_mismatch"


def test_principal_mismatch_rejects(gate: ApprovalGate) -> None:
    decision = _pause(gate)
    rejected = _retry(gate, decision.payload["requestState"], caller="caller-b")
    assert rejected.action == "REJECT"
    assert rejected.reason == "principal_mismatch"


def test_missing_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMW_APPROVAL_STATE_SECRET", raising=False)
    gate = ApprovalGate(store=InMemoryApprovalStore())
    with pytest.raises(ApprovalGateUnavailable):
        gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)


def test_request_id_is_logged_not_keyed(gate: ApprovalGate) -> None:
    first = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True, request_id="req-1")
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True, request_id="req-2")
    assert first.action == "PAUSE"
    assert second.action == "PAUSE"
    assert gate.store.list_pending()
    keys = {record.idempotency_key for record in gate.store.list_pending()}
    assert len(keys) == 1
