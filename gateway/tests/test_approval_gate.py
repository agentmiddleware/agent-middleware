"""Tests for the AMW V2 approval gate state machine.

Every value comes from the real G1 gate recording
(integrations/goose-agent-middleware/tests/fixtures/g1_gate_calls.jsonl) or is
made at runtime (secrets.token_hex / uuid.uuid4). Nothing here is hand-invented.
"""

import asyncio
import json
import logging
import secrets
import sys
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (
    REPO_ROOT / "integrations" / "goose-agent-middleware" / "src",
    REPO_ROOT / "sdk" / "python" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from typing import Any, cast  # noqa: E402

import pytest  # noqa: E402
from goose_amw import canonical as goose_canonical  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

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


def _fixture_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


FIXTURE_EPOCH = _fixture_epoch(W["ts"])


class _Clock:
    def __init__(self, now: float = FIXTURE_EPOCH) -> None:
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


async def _apause(gate: ApprovalGate) -> Any:
    decision = await gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert decision.action == "PAUSE"
    assert decision.payload is not None
    return decision


def _responses(nonce: str) -> dict[str, Any]:
    """SEP-2322 retry answers, keyed by the pause's request id."""
    return {nonce: {"approved": True}}


def _retry(gate: ApprovalGate, state: str, nonce: str, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "caller": CALLER,
        "tool": TOOL,
        "arguments": ARGS,
        "session_id": SESSION,
        "client_supports_input_required": True,
        "request_state": state,
        "input_responses": _responses(nonce),
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


async def _aretry(gate: ApprovalGate, state: str, nonce: str, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "caller": CALLER,
        "tool": TOOL,
        "arguments": ARGS,
        "session_id": SESSION,
        "client_supports_input_required": True,
        "request_state": state,
        "input_responses": _responses(nonce),
    }
    params.update(kwargs)
    return await gate.aevaluate(
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
    requests = decision.payload["inputRequests"]
    assert isinstance(requests, dict)
    assert decision.nonce is not None
    assert set(requests) == {decision.nonce}
    entry = requests[decision.nonce]
    assert entry["method"] == "elicitation/create"
    assert entry["params"]["message"] == f"Approve tool call: {TOOL}"
    assert entry["params"]["requestedSchema"] == {
        "type": "object",
        "properties": {"approved": {"type": "boolean"}},
        "required": ["approved"],
    }
    assert isinstance(decision.payload["requestState"], str)
    assert decision.payload["requestState"]


def test_key_equals_goose_idempotency_key(gate: ApprovalGate) -> None:
    expected = goose_canonical.idempotency_key(
        caller=CALLER, tool=TOOL, arguments=ARGS, session_id=SESSION
    )
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, True, now=gate.clock())
    executed = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert executed.action == "EXECUTE"
    assert executed.idempotency_key == expected


def test_approved_retry_executes_exactly_once(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, True, now=gate.clock())
    first = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert first.action == "EXECUTE"
    second = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert second.action == "REJECT"
    assert second.reason == "replayed"
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


def test_execute_emits_consumed_receipt(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, True, now=gate.clock())
    executed = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert executed.action == "EXECUTE"
    assert [event for event, _ in receipts] == ["consumed"]
    assert receipts[0][1]["nonce"] == decision.nonce
    assert receipts[0][1]["tool"] == TOOL
    assert receipts[0][1]["principal"] == CALLER


def test_pending_retry_repauses(gate: ApprovalGate) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    again = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert again.action == "PAUSE"
    assert again.payload is not None
    assert again.payload["resultType"] == "input_required"
    assert set(again.payload["inputRequests"]) == {decision.nonce}
    assert again.payload["requestState"] == decision.payload["requestState"]


def test_deny_rejects_and_receipts(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, False, now=gate.clock())
    rejected = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == "denied"
    assert [event for event, _ in receipts] == ["denied"]


def test_expired_rejects_timeout_and_marks_expired(
    gate: ApprovalGate, clock: _Clock, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    clock.now += 3600 + 1
    rejected = _retry(gate, decision.payload["requestState"], decision.nonce)
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
    # The seal fails before the responses are read, so any key shape works.
    rejected = _retry(gate, tampered, _fresh_id())
    assert rejected.action == "REJECT"
    assert rejected.reason == "tampered"
    assert [event for event, _ in receipts] == ["tampered"]


def test_unknown_nonce_rejects_tampered(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]], clock: _Clock
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    fresh = ApprovalGate(
        secret=SECRET,
        store=InMemoryApprovalStore(),
        clock=clock,
        receipt_sink=lambda event, record: receipts.append((event, record)),
    )
    rejected = _retry(fresh, decision.payload["requestState"], decision.nonce)
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
    assert decision.nonce is not None
    rejected = _retry(
        gate,
        decision.payload["requestState"],
        decision.nonce,
        client_supports_input_required=False,
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
    assert decision.nonce is not None
    rejected = _retry(
        gate,
        decision.payload["requestState"],
        decision.nonce,
        arguments={"path": str(tmp_path)},
    )
    assert rejected.action == "REJECT"
    assert rejected.reason == "args_mismatch"
    assert [event for event, _ in receipts] == ["args_mismatch"]


def test_principal_mismatch_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    rejected = _retry(
        gate, decision.payload["requestState"], decision.nonce, caller=_fresh_id()
    )
    assert rejected.action == "REJECT"
    assert rejected.reason == "principal_mismatch"
    assert [event for event, _ in receipts] == ["principal_mismatch"]


def test_tool_mismatch_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    rejected = _retry(
        gate, decision.payload["requestState"], decision.nonce, tool=_fresh_id()
    )
    assert rejected.action == "REJECT"
    assert rejected.reason == "tool_mismatch"
    assert [event for event, _ in receipts] == ["tool_mismatch"]


def test_session_mismatch_rejects(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    rejected = _retry(
        gate, decision.payload["requestState"], decision.nonce, session_id=_fresh_id()
    )
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
    assert decision.nonce is not None
    rejected = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == "key_mismatch"
    assert [event for event, _ in receipts] == ["key_mismatch"]


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("tool", "tool_mismatch"),
        ("session_id", "session_mismatch"),
        ("idempotency_key", "key_mismatch"),
    ],
)
def test_stored_record_mismatch_rejects(
    gate: ApprovalGate,
    receipts: list[tuple[str, dict[str, Any]]],
    field: str,
    reason: str,
) -> None:
    """A stored record that drifts from the sealed state cannot execute."""
    decision = _pause(gate)
    assert decision.nonce is not None
    assert decision.payload is not None
    record = gate.store.get(decision.nonce)
    assert record is not None
    if field == "idempotency_key":
        tampered = f"amw-fb-{secrets.token_hex(32)}"
        while tampered == record.idempotency_key:
            tampered = f"amw-fb-{secrets.token_hex(32)}"
    else:
        tampered = _fresh_id()
        while tampered == getattr(record, field):
            tampered = _fresh_id()
    gate.store.save(replace(record, **{field: tampered}))
    rejected = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == reason
    assert [event for event, _ in receipts] == [reason]


@pytest.mark.parametrize("case", ["missing", "empty", "wrong_id", "legacy_list"])
def test_retry_without_matching_input_response_rejects(
    gate: ApprovalGate,
    receipts: list[tuple[str, dict[str, Any]]],
    case: str,
) -> None:
    """The retry must answer the pause's request id; anything else rejects."""
    decision = _pause(gate)
    assert decision.nonce is not None
    if case == "missing":
        responses = None
    elif case == "empty":
        responses = {}
    elif case == "wrong_id":
        responses = _responses(_fresh_id())
    else:
        responses = cast(Any, [{"requestId": decision.nonce, "approved": True}])
    rejected = _retry(
        gate,
        decision.payload["requestState"],
        decision.nonce,
        input_responses=responses,
    )
    assert rejected.action == "REJECT"
    assert rejected.reason == "response_mismatch"
    assert [event for event, _ in receipts] == ["response_mismatch"]


def test_consume_rejects_expired_approval(gate: ApprovalGate, clock: _Clock) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    gate.store.decide(decision.nonce, True, now=gate.clock())
    clock.now += 3600 + 1
    assert gate.store.consume(decision.nonce, now=clock()) is None
    record = gate.store.get(decision.nonce)
    assert record is not None
    assert record.consumed is False


def test_decide_refuses_expired_approval(gate: ApprovalGate, clock: _Clock) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    clock.now += 3600 + 1
    assert gate.store.decide(decision.nonce, True, now=clock()) is None
    assert gate.store.decide(decision.nonce, False, now=clock()) is None
    record = gate.store.get(decision.nonce)
    assert record is not None
    assert record.status == "pending"


async def test_adecide_refuses_expired_approval(
    gate: ApprovalGate, clock: _Clock
) -> None:
    decision = _pause(gate)
    assert decision.nonce is not None
    clock.now += 3600 + 1
    assert await gate.store.adecide(decision.nonce, True, now=clock()) is None
    record = await gate.store.aget(decision.nonce)
    assert record is not None
    assert record.status == "pending"


def test_list_pending_excludes_expired(gate: ApprovalGate, clock: _Clock) -> None:
    first = _pause(gate)
    assert first.nonce is not None
    clock.now += 3600 + 1
    second = _pause(gate)
    assert second.nonce is not None
    pending = gate.store.list_pending(now=clock())
    assert [record.nonce for record in pending] == [second.nonce]


async def test_alist_pending_excludes_expired(
    gate: ApprovalGate, clock: _Clock
) -> None:
    first = _pause(gate)
    assert first.nonce is not None
    clock.now += 3600 + 1
    second = _pause(gate)
    assert second.nonce is not None
    pending = await gate.store.alist_pending(now=clock())
    assert [record.nonce for record in pending] == [second.nonce]


async def test_async_evaluate_executes_after_approval(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    decision = await gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert decision.action == "PAUSE"
    assert decision.payload is not None
    assert decision.nonce is not None
    assert set(decision.payload["inputRequests"]) == {decision.nonce}
    await gate.store.adecide(decision.nonce, True, now=gate.clock())
    executed = await _aretry(gate, decision.payload["requestState"], decision.nonce)
    assert executed.action == "EXECUTE"
    replayed = await _aretry(gate, decision.payload["requestState"], decision.nonce)
    assert replayed.action == "REJECT"
    assert replayed.reason == "replayed"
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


async def test_db_concurrent_retries_execute_exactly_once(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """Two retries racing on one approval spend it exactly once."""
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    state: str = decision.payload["requestState"]
    await db_gate.store.adecide(decision.nonce, True, now=clock())
    first, second = await asyncio.gather(
        _aretry(db_gate, state, decision.nonce),
        _aretry(db_gate, state, decision.nonce),
    )
    assert sorted([first.action, second.action]) == ["EXECUTE", "REJECT"]
    loser = first if first.action == "REJECT" else second
    assert loser.reason == "replayed"
    assert sorted(event for event, _ in receipts) == ["consumed", "replayed"]


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
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    state: str = decision.payload["requestState"]

    # Simulated restart: a new store instance sees the same pending row.
    restarted = DbApprovalStore()
    record = await restarted.aget(decision.nonce)
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
    pending = await restarted.alist_pending()
    assert [item.nonce for item in pending] == [decision.nonce]

    await restarted.adecide(decision.nonce, True, now=clock())
    executed = await _aretry(db_gate, state, decision.nonce)
    assert executed.action == "EXECUTE"

    # Second restart: the nonce stays consumed; the retry cannot replay.
    restarted_again = DbApprovalStore()
    consumed = await restarted_again.aget(decision.nonce)
    assert consumed is not None
    assert consumed.consumed is True
    assert await restarted_again.aconsume(decision.nonce, now=clock()) is None
    replayed = await _aretry(db_gate, state, decision.nonce)
    assert replayed.action == "REJECT"
    assert replayed.reason == "replayed"
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


async def test_db_denied_decision_visible_across_instances(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    decider = DbApprovalStore()
    denied = await decider.adecide(decision.nonce, False, now=clock())
    assert denied is not None
    assert denied.status == "denied"

    observer = DbApprovalStore()
    assert await observer.aget(decision.nonce) is not None
    observed = await observer.aget(decision.nonce)
    assert observed is not None
    assert observed.status == "denied"
    assert await observer.alist_pending() == []
    assert await observer.adecide(decision.nonce, True, now=clock()) is None

    rejected = await _aretry(db_gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == "denied"
    assert [event for event, _ in receipts] == ["denied"]


async def test_db_consume_rejects_expired_approval(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """The atomic consume UPDATE refuses an approval past its deadline."""
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    store = DbApprovalStore()
    approved = await store.adecide(decision.nonce, True, now=clock())
    assert approved is not None
    clock.now += 3600 + 1
    assert await store.aconsume(decision.nonce, now=clock()) is None
    record = await store.aget(decision.nonce)
    assert record is not None
    assert record.consumed is False


async def test_db_sync_store_refuses_running_loop() -> None:
    """Sync DB calls inside a loop refuse instead of crossing engine loops."""
    store = DbApprovalStore()
    with pytest.raises(RuntimeError, match="running event loop"):
        store.get(_fresh_id())


async def test_db_adecide_refuses_expired_approval(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """The atomic decide UPDATE refuses an approval past its deadline."""
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    store = DbApprovalStore()
    clock.now += 3600 + 1
    assert await store.adecide(decision.nonce, True, now=clock()) is None
    assert await store.adecide(decision.nonce, False, now=clock()) is None
    record = await store.aget(decision.nonce)
    assert record is not None
    assert record.status == "pending"


async def test_db_alist_pending_excludes_expired(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    first = await _apause(db_gate)
    assert first.nonce is not None
    clock.now += 3600 + 1
    second = await _apause(db_gate)
    assert second.nonce is not None
    pending = await DbApprovalStore().alist_pending(now=clock())
    assert [record.nonce for record in pending] == [second.nonce]


def test_exact_deadline_pending_refuses_gate_and_store(
    gate: ApprovalGate, clock: _Clock, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    """At now == deadline the record is expired for both gate and store."""
    decision = _pause(gate)
    assert decision.nonce is not None
    assert decision.payload is not None
    record = gate.store.get(decision.nonce)
    assert record is not None
    clock.now = record.deadline
    assert gate.store.decide(decision.nonce, True, now=clock()) is None
    rejected = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == "timeout"
    assert gate.store.get(decision.nonce) is not None
    assert gate.store.get(decision.nonce).status == "expired"  # type: ignore[union-attr]
    assert [event for event, _ in receipts] == ["timeout"]


def test_exact_deadline_approved_refuses_gate_and_store(
    gate: ApprovalGate, clock: _Clock, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    """An approval cannot spend at its exact deadline instant."""
    decision = _pause(gate)
    assert decision.nonce is not None
    assert decision.payload is not None
    assert gate.store.decide(decision.nonce, True, now=clock()) is not None
    record = gate.store.get(decision.nonce)
    assert record is not None
    clock.now = record.deadline
    assert gate.store.consume(decision.nonce, now=clock()) is None
    assert gate.store.get(decision.nonce) is not None
    assert gate.store.get(decision.nonce).consumed is False  # type: ignore[union-attr]
    rejected = _retry(gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == "timeout"
    assert [event for event, _ in receipts] == ["timeout"]


async def test_db_exact_deadline_approved_refuses_gate_and_store(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """The atomic consume UPDATE and the gate agree at now == deadline."""
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    assert decision.payload is not None
    store = DbApprovalStore()
    assert await store.adecide(decision.nonce, True, now=clock()) is not None
    record = await store.aget(decision.nonce)
    assert record is not None
    clock.now = record.deadline
    assert await store.aconsume(decision.nonce, now=clock()) is None
    rejected = await _aretry(db_gate, decision.payload["requestState"], decision.nonce)
    assert rejected.action == "REJECT"
    assert rejected.reason == "timeout"
    assert [event for event, _ in receipts] == ["timeout"]


async def test_db_consume_stamps_passed_now(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """decided_at/consumed_at come from the gate clock, not wall clock."""
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    store = DbApprovalStore()
    assert await store.adecide(decision.nonce, True, now=clock()) is not None
    assert await store.aconsume(decision.nonce, now=clock()) is not None
    expected = gate_module._to_db_time(clock())
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ApprovalGateModel).where(ApprovalGateModel.nonce == decision.nonce)
        )
        model = result.scalar_one()
        assert model.decided_at == expected
        assert model.consumed_at == expected


def test_second_first_call_reuses_pending_record(gate: ApprovalGate) -> None:
    """A repeat first call for one key reissues the same pause."""
    first = _pause(gate)
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "PAUSE"
    assert second.nonce == first.nonce
    assert len(gate.store.list_pending()) == 1


def test_first_call_reuses_approved_unconsumed_record(
    gate: ApprovalGate,
) -> None:
    """Reuse covers approved-but-unspent records, and the pause still spends."""
    first = _pause(gate)
    assert first.nonce is not None
    assert gate.store.decide(first.nonce, True, now=gate.clock()) is not None
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "PAUSE"
    assert second.nonce == first.nonce
    assert second.payload is not None
    executed = _retry(gate, second.payload["requestState"], second.nonce)
    assert executed.action == "EXECUTE"


@pytest.mark.parametrize("terminal", ["denied", "expired"])
def test_first_call_mints_after_terminal_record(
    gate: ApprovalGate, clock: _Clock, terminal: str
) -> None:
    """Denied and expired records are not reused for a fresh call."""
    first = _pause(gate)
    assert first.nonce is not None
    assert first.payload is not None
    if terminal == "denied":
        assert gate.store.decide(first.nonce, False, now=gate.clock()) is not None
    else:
        clock.now += 3600 + 1
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "PAUSE"
    assert second.nonce != first.nonce


async def test_async_second_first_call_reuses_active_record(
    gate: ApprovalGate,
) -> None:
    first = await gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    second = await gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "PAUSE"
    assert second.nonce == first.nonce


async def test_db_second_first_call_reuses_active_record(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    first = await _apause(db_gate)
    second = await _apause(db_gate)
    assert second.action == "PAUSE"
    assert second.nonce == first.nonce
    pending = await DbApprovalStore().alist_pending(now=clock())
    assert [record.nonce for record in pending] == [first.nonce]


def _seal_duplicate(nonce: str, deadline: float) -> str:
    """Seal retry state for a duplicate nonce the way the pause path does."""
    return gate_module._seal_pause_state(
        SECRET.encode("utf-8"),
        args_hash=goose_canonical.args_hash(ARGS),
        caller=CALLER,
        tool=TOOL,
        session_id=SESSION,
        idempotency_key=goose_canonical.idempotency_key(
            caller=CALLER, tool=TOOL, arguments=ARGS, session_id=SESSION
        ),
        deadline=deadline,
        nonce=nonce,
    )


def _duplicate_nonce(first_nonce: str) -> str:
    nonce = secrets.token_urlsafe(16)
    while nonce == first_nonce:
        nonce = secrets.token_urlsafe(16)
    return nonce


def test_two_approved_pauses_same_key_execute_exactly_once(
    gate: ApprovalGate, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    """Two approved nonces for one key spend exactly once between them."""
    first = _pause(gate)
    assert first.nonce is not None
    assert first.payload is not None
    record = gate.store.get(first.nonce)
    assert record is not None
    # A duplicate pause for the same key (minted before reuse, or raced in):
    # same key fields, fresh nonce, saved directly.
    duplicate = _duplicate_nonce(first.nonce)
    gate.store.save(replace(record, nonce=duplicate), now=gate.clock())
    assert gate.store.decide(first.nonce, True, now=gate.clock()) is not None
    assert gate.store.decide(duplicate, True, now=gate.clock()) is not None
    executed = _retry(gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    replayed = _retry(gate, _seal_duplicate(duplicate, record.deadline), duplicate)
    assert replayed.action == "REJECT"
    assert replayed.reason == "replayed"
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


async def test_db_two_approved_pauses_same_key_execute_exactly_once(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """The NOT EXISTS consume guard holds on the DB path, either order."""
    decision = await _apause(db_gate)
    assert decision.nonce is not None
    assert decision.payload is not None
    store = DbApprovalStore()
    record = await store.aget(decision.nonce)
    assert record is not None
    duplicate = _duplicate_nonce(decision.nonce)
    await store.asave(replace(record, nonce=duplicate), now=clock())
    assert await store.adecide(decision.nonce, True, now=clock()) is not None
    assert await store.adecide(duplicate, True, now=clock()) is not None
    # Spend the duplicate first: the original must then refuse.
    executed = await _aretry(
        db_gate, _seal_duplicate(duplicate, record.deadline), duplicate
    )
    assert executed.action == "EXECUTE"
    replayed = await _aretry(db_gate, decision.payload["requestState"], decision.nonce)
    assert replayed.action == "REJECT"
    assert replayed.reason == "replayed"
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


def test_default_sink_routes_consumed_like_other_events(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Without an injected sink, consumed logs exactly like other events."""
    gate = ApprovalGate(secret=SECRET, store=InMemoryApprovalStore(), clock=clock)
    with caplog.at_level(logging.INFO, logger="app.services.approval_gate"):
        first = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
        assert first.nonce is not None
        assert first.payload is not None
        assert gate.store.decide(first.nonce, True, now=gate.clock()) is not None
        executed = _retry(gate, first.payload["requestState"], first.nonce)
        assert executed.action == "EXECUTE"
        # Past the spend window a fresh pause mints normally to deny.
        spent = gate.store.get(first.nonce)
        assert spent is not None
        clock.now = spent.deadline + 1
        second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
        assert second.nonce is not None
        assert second.nonce != first.nonce
        assert second.payload is not None
        assert gate.store.decide(second.nonce, False, now=gate.clock()) is not None
        denied = _retry(gate, second.payload["requestState"], second.nonce)
        assert denied.action == "REJECT"
        assert denied.reason == "denied"
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.services.approval_gate"
    ]
    assert any("event=consumed" in line for line in lines)
    assert any("event=denied" in line for line in lines)
    assert all(line.startswith("approval_receipt event=") for line in lines)


def test_first_call_refuses_while_spent_live(
    gate: ApprovalGate, clock: _Clock, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    """Within the spend window a fresh call refuses without minting."""
    first = _pause(gate)
    assert first.nonce is not None
    assert first.payload is not None
    assert gate.store.decide(first.nonce, True, now=gate.clock()) is not None
    executed = _retry(gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "REJECT"
    assert second.reason == "replayed"
    assert second.nonce is None
    assert gate.store.list_pending(now=clock()) == []
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


async def test_async_first_call_refuses_while_spent_live(
    gate: ApprovalGate, clock: _Clock, receipts: list[tuple[str, dict[str, Any]]]
) -> None:
    """Async pause path refuses a fresh mint while the spend is live."""
    first = await _apause(gate)
    assert first.nonce is not None
    assert first.payload is not None
    assert await gate.store.adecide(first.nonce, True, now=gate.clock()) is not None
    executed = await _aretry(gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    second = await gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "REJECT"
    assert second.reason == "replayed"
    assert second.nonce is None
    assert await gate.store.alist_pending(now=clock()) == []
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


async def test_db_first_call_refuses_while_spent_live(
    db_gate: ApprovalGate,
    clock: _Clock,
    receipts: list[tuple[str, dict[str, Any]]],
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """DB pause path refuses a fresh mint while the spend is live."""
    first = await _apause(db_gate)
    assert first.nonce is not None
    assert first.payload is not None
    store = DbApprovalStore()
    assert await store.adecide(first.nonce, True, now=clock()) is not None
    executed = await _aretry(db_gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    second = await db_gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "REJECT"
    assert second.reason == "replayed"
    assert second.nonce is None
    assert await store.alist_pending(now=clock()) == []
    assert [event for event, _ in receipts] == ["consumed", "replayed"]


def test_sibling_refused_stays_refused_past_spent_deadline(
    gate: ApprovalGate, clock: _Clock
) -> None:
    """A sibling blocked by a live spend stays refused past that deadline."""
    first = _pause(gate)
    assert first.nonce is not None
    assert first.payload is not None
    record = gate.store.get(first.nonce)
    assert record is not None
    duplicate = _duplicate_nonce(first.nonce)
    gate.store.save(
        replace(record, nonce=duplicate, deadline=record.deadline + 500),
        now=gate.clock(),
    )
    assert gate.store.decide(first.nonce, True, now=gate.clock()) is not None
    assert gate.store.decide(duplicate, True, now=gate.clock()) is not None
    executed = _retry(gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    assert gate.store.consume(duplicate, now=clock()) is None
    blocked = gate.store.get(duplicate)
    assert blocked is not None
    assert blocked.status == "denied"
    spent = gate.store.get(first.nonce)
    assert spent is not None
    clock.now = spent.deadline + 1
    assert clock() < blocked.deadline
    assert gate.store.consume(duplicate, now=clock()) is None
    assert gate.store.get(duplicate) is not None
    assert gate.store.get(duplicate).status == "denied"  # type: ignore[union-attr]


async def test_db_sibling_refused_stays_refused_past_spent_deadline(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """DB sibling blocked by a live spend stays refused past that deadline."""
    first = await _apause(db_gate)
    assert first.nonce is not None
    assert first.payload is not None
    store = DbApprovalStore()
    record = await store.aget(first.nonce)
    assert record is not None
    duplicate = _duplicate_nonce(first.nonce)
    await store.asave(
        replace(record, nonce=duplicate, deadline=record.deadline + 500),
        now=clock(),
    )
    assert await store.adecide(first.nonce, True, now=clock()) is not None
    assert await store.adecide(duplicate, True, now=clock()) is not None
    executed = await _aretry(db_gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    assert await store.aconsume(duplicate, now=clock()) is None
    blocked = await store.aget(duplicate)
    assert blocked is not None
    assert blocked.status == "denied"
    spent = await store.aget(first.nonce)
    assert spent is not None
    clock.now = spent.deadline + 1
    assert clock() < blocked.deadline
    assert await store.aconsume(duplicate, now=clock()) is None
    refused = await store.aget(duplicate)
    assert refused is not None
    assert refused.status == "denied"


def test_first_call_mints_after_spent_window(gate: ApprovalGate, clock: _Clock) -> None:
    """Past the spend window a brand-new call mints and spends normally."""
    first = _pause(gate)
    assert first.nonce is not None
    assert first.payload is not None
    assert gate.store.decide(first.nonce, True, now=gate.clock()) is not None
    executed = _retry(gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    spent = gate.store.get(first.nonce)
    assert spent is not None
    clock.now = spent.deadline + 1
    second = gate.evaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "PAUSE"
    assert second.nonce is not None
    assert second.nonce != first.nonce
    assert second.payload is not None
    assert gate.store.decide(second.nonce, True, now=gate.clock()) is not None
    replay = _retry(gate, second.payload["requestState"], second.nonce)
    assert replay.action == "EXECUTE"


async def test_db_first_call_mints_after_spent_window(
    db_gate: ApprovalGate,
    clock: _Clock,
    clean_database: Any,
    purge_approval_gate_table: Any,
) -> None:
    """DB path mints and spends normally past the spend window."""
    first = await _apause(db_gate)
    assert first.nonce is not None
    assert first.payload is not None
    store = DbApprovalStore()
    assert await store.adecide(first.nonce, True, now=clock()) is not None
    executed = await _aretry(db_gate, first.payload["requestState"], first.nonce)
    assert executed.action == "EXECUTE"
    spent = await store.aget(first.nonce)
    assert spent is not None
    clock.now = spent.deadline + 1
    second = await db_gate.aevaluate(CALLER, TOOL, ARGS, SESSION, True)
    assert second.action == "PAUSE"
    assert second.nonce is not None
    assert second.nonce != first.nonce
    assert second.payload is not None
    assert await store.adecide(second.nonce, True, now=clock()) is not None
    replay = await _aretry(db_gate, second.payload["requestState"], second.nonce)
    assert replay.action == "EXECUTE"
