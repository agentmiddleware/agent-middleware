"""Tests for the AMW V2 approval gate state machine.

Every value comes from the real G1 gate recording
(integrations/goose-agent-middleware/tests/fixtures/g1_gate_calls.jsonl) or is
made at runtime (secrets.token_hex / uuid.uuid4). Nothing here is hand-invented.
"""

import json
import secrets
import sys
import uuid
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
from sqlalchemy import delete  # noqa: E402

import app.services.approval_gate as gate_module  # noqa: E402
from app.db.approval_gate_models import ApprovalGateModel  # noqa: E402
from app.db.database import get_session_factory  # noqa: E402
from app.services.approval_gate import (  # noqa: E402
    ApprovalGate,
    ApprovalGateUnavailable,
    DbApprovalStore,
    InMemoryApprovalStore,
)

FIXTURE = (
    REPO_ROOT
    / "integrations"
    / "goose-agent-middleware"
    / "tests"
    / "fixtures"
    / "g1_gate_calls.jsonl"
)
RECORDS = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
WRITES = [record for record in RECORDS if record["tool"] == "write_file"]
W = WRITES[0]
TOOL = W["tool"]
SESSION = W["meta"]["agent-session-id"]
ARGS = {"path": W["path"]}  # the only argument field the recording holds
REQUEST_IDS = [w["meta"]["agent-tool-call-request-id"] for w in WRITES]

SECRET = secrets.token_hex(32)
CALLER = str(uuid.uuid4())


def _fresh_id() -> str:
    return str(uuid.uuid4())


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


@pytest.fixture
def db_gate(receipts: list[tuple[str, dict[str, Any]]], clock: _Clock) -> ApprovalGate:
    return ApprovalGate(
        secret=SECRET,
        store=DbApprovalStore(),
        clock=clock,
        receipt_sink=lambda event, record: receipts.append((event, record)),
    )


async def _purge_approval_gate_table() -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(delete(ApprovalGateModel))
        await session.commit()


@pytest.fixture
async def purge_approval_gate_table() -> Any:
    await _purge_approval_gate_table()
    yield
    await _purge_approval_gate_table()


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
        "input_responses": [{"requestId": _fresh_id(), "approved": True}],
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


def test_unsupported_client_rejects_without_record(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = gate.evaluate(CALLER, TOOL, ARGS, SESSION, False)
    assert decision.action == "REJECT"
    assert decision.reason == "unsupported_client"
    assert gate.store.list_pending() == []
    assert [event for event, _ in receipts] == ["unsupported_client"]
    assert receipts[0][1]["tool"] == TOOL
    assert receipts[0][1]["principal"] == CALLER


def test_unsupported_client_retry_rejects_with_receipt(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    rejected = _retry(
        gate, decision.payload["requestState"], client_supports_input_required=False
    )
    assert rejected.action == "REJECT"
    assert rejected.reason == "unsupported_client"
    assert [event for event, _ in receipts] == ["unsupported_client"]


def test_args_mismatch_rejects(
    gate: ApprovalGate,
    receipts: list[tuple[str, dict[str, Any]]],
    tmp_path: Path,
) -> None:
    decision = _pause(gate)
    rejected = _retry(
        gate, decision.payload["requestState"], arguments={"path": str(tmp_path)}
    )
    assert rejected.action == "REJECT"
    assert rejected.reason == "args_mismatch"
    assert [event for event, _ in receipts] == ["args_mismatch"]


def test_principal_mismatch_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    rejected = _retry(gate, decision.payload["requestState"], caller=_fresh_id())
    assert rejected.action == "REJECT"
    assert rejected.reason == "principal_mismatch"
    assert [event for event, _ in receipts] == ["principal_mismatch"]


def test_tool_mismatch_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    rejected = _retry(gate, decision.payload["requestState"], tool=_fresh_id())
    assert rejected.action == "REJECT"
    assert rejected.reason == "tool_mismatch"
    assert [event for event, _ in receipts] == ["tool_mismatch"]


def test_session_mismatch_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    rejected = _retry(gate, decision.payload["requestState"], session_id=_fresh_id())
    assert rejected.action == "REJECT"
    assert rejected.reason == "session_mismatch"
    assert [event for event, _ in receipts] == ["session_mismatch"]


def test_key_mismatch_rejects(
    gate: ApprovalGate,
    receipts: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drifted key derivation cannot ride the sealed approval."""
    decision = _pause(gate)
    real = goose_canonical

    class _DriftedCanonical:
        args_hash = staticmethod(real.args_hash)

        @staticmethod
        def idempotency_key(**kwargs: Any) -> str:
            return secrets.token_hex(32)

    monkeypatch.setattr(gate_module, "_load_canonical", lambda: _DriftedCanonical)
    rejected = _retry(gate, decision.payload["requestState"])
    assert rejected.action == "REJECT"
    assert rejected.reason == "key_mismatch"
    assert [event for event, _ in receipts] == ["key_mismatch"]


def test_missing_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMW_APPROVAL_STATE_SECRET", raising=False)
    gate = ApprovalGate(store=InMemoryApprovalStore())
    with pytest.raises(ApprovalGateUnavailable):
        gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)


def test_request_id_is_logged_not_keyed(gate: ApprovalGate) -> None:
    assert len(REQUEST_IDS) >= 2
    first = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True, request_id=REQUEST_IDS[0])
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True, request_id=REQUEST_IDS[1])
    assert first.action == "PAUSE"
    assert second.action == "PAUSE"
    assert gate.store.list_pending()
    keys = {record.idempotency_key for record in gate.store.list_pending()}
    assert len(keys) == 1


def test_default_store_is_db_backed() -> None:
    assert isinstance(ApprovalGate(secret=SECRET).store, DbApprovalStore)


async def test_db_consumed_nonce_stays_consumed_across_restart(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    decision = _pause(db_gate)
    assert decision.nonce is not None
    state: str = decision.payload["requestState"]

    # Simulated restart: a new store instance sees the same pending row.
    restarted = DbApprovalStore()
    record = restarted.get(decision.nonce)
    assert record is not None
    assert record.status == "pending"
    assert record.tool == TOOL
    assert record.session_id == SESSION
    assert record.principal == CALLER
    assert record.args_hash == goose_canonical.args_hash(ARGS)
    assert record.idempotency_key == goose_canonical.idempotency_key(
        caller=CALLER, tool=TOOL, arguments=ARGS, session_id=SESSION
    )
    assert record.deadline == clock.now + 300
    assert [pending.nonce for pending in restarted.list_pending()] == [decision.nonce]

    restarted.decide(decision.nonce, True, now=clock())
    executed = _retry(db_gate, state)
    assert executed.action == "EXECUTE"

    # Second restart: the nonce stays consumed; the retry cannot replay.
    restarted_again = DbApprovalStore()
    consumed = restarted_again.get(decision.nonce)
    assert consumed is not None
    assert consumed.consumed is True
    assert restarted_again.consume(decision.nonce) is None
    replayed = _retry(db_gate, state)
    assert replayed.action == "REJECT"
    assert replayed.reason == "replayed"
    assert [event for event, _ in receipts] == ["replayed"]


async def test_db_denied_decision_visible_across_instances(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    decision = _pause(db_gate)
    assert decision.nonce is not None
    decider = DbApprovalStore()
    denied = decider.decide(decision.nonce, False, now=clock())
    assert denied is not None
    assert denied.status == "denied"

    observer = DbApprovalStore()
    assert observer.get(decision.nonce) is not None
    assert observer.get(decision.nonce).status == "denied"  # type: ignore[union-attr]
    assert observer.list_pending() == []
    assert observer.decide(decision.nonce, True, now=clock()) is None

    rejected = _retry(db_gate, decision.payload["requestState"])
    assert rejected.action == "REJECT"
    assert rejected.reason == "denied"
    assert [event for event, _ in receipts] == ["denied"]
