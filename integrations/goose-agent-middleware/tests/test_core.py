import json
from pathlib import Path

import pytest

from goose_amw import hook, permits
from goose_amw.canonical import args_hash, canonical_json, idempotency_key
from goose_amw.ledger import Ledger
from goose_amw.receipts import ReceiptSigner, load_or_create_key, verify

FIXTURE = Path(__file__).parent / "fixtures" / "g1_gate_calls.jsonl"


def test_canonical_hash_ignores_key_order():
    assert canonical_json({"b": 1, "a": [2]}) == '{"a":[2],"b":1}'
    assert args_hash({"path": "p", "content": "c"}) == args_hash({"content": "c", "path": "p"})


def test_key_inputs():
    k = idempotency_key(caller="c", tool="write_file", arguments={"x": 1}, session_id="s")
    assert k == idempotency_key(caller="c", tool="write_file", arguments={"x": 1}, session_id="s")
    assert k != idempotency_key(caller="d", tool="write_file", arguments={"x": 1}, session_id="s")
    assert k != idempotency_key(
        caller="c", tool="read_text_file", arguments={"x": 1}, session_id="s"
    )
    assert k != idempotency_key(caller="c", tool="write_file", arguments={"x": 2}, session_id="s")
    assert k != idempotency_key(caller="c", tool="write_file", arguments={"x": 1}, session_id="t")


def test_recorded_gate_retry_maps_to_same_key_despite_new_request_id():
    """Real G1 gate evidence: Goose sent a new request id on the model's retry."""
    writes = [
        r for r in map(json.loads, FIXTURE.read_text().splitlines()) if r["tool"] == "write_file"
    ]
    assert len(writes) == 2
    ids = [w["meta"]["agent-tool-call-request-id"] for w in writes]
    assert ids[0] != ids[1]
    assert all(w["protocol"] == "2026-07-28" for w in writes)
    args = {"path": writes[0]["path"], "content": "G1 gate"}
    keys = {
        idempotency_key(
            caller="goose-local",
            tool="write_file",
            arguments=args,
            session_id=w["meta"]["agent-session-id"],
        )
        for w in writes
    }
    assert len(keys) == 1


def test_ledger_replays_inside_window_and_reruns_after(tmp_path):
    led = Ledger(tmp_path / "l.db", window_seconds=10)
    assert led.claim("k", tool="t", args_hash="h", request_id="r1", now=100).action == "run"
    led.finish("k", "completed", "ok")
    c = led.claim("k", tool="t", args_hash="h", request_id="r2", now=105)
    assert (c.action, c.result) == ("replay", "ok")
    assert led.claim("k", tool="t", args_hash="h", request_id="r3", now=111).action == "run"
    assert led.get("k")["executions"] == 2


def test_failed_unknown_is_never_rerun(tmp_path):
    led = Ledger(tmp_path / "l.db", window_seconds=10)
    led.claim("k", tool="t", args_hash="h", request_id=None, now=0)
    led.finish("k", "failed_unknown")
    for now in (1, 10_000):
        assert led.claim("k", tool="t", args_hash="h", request_id=None, now=now).action == "refuse"
    assert led.get("k")["executions"] == 1


def test_crash_in_flight_becomes_failed_unknown(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.claim("k", tool="t", args_hash="h", request_id=None)
    assert Ledger(tmp_path / "l.db").mark_stale_in_flight_unknown() == 1
    assert led.get("k")["status"] == "failed_unknown"


def test_definite_failure_may_rerun(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.claim("k", tool="t", args_hash="h", request_id=None)
    led.finish("k", "failed")
    assert led.claim("k", tool="t", args_hash="h", request_id=None).action == "run"


def test_window_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path / "l.db", window_seconds=0)


POLICY = {"rules": [{"id": "w", "tool": "write_file", "folder": "/tmp/amw-allowed"}]}


def test_permits():
    assert permits.check(POLICY, "amw__write_file", {"path": "/tmp/amw-allowed/a"}).allowed
    assert not permits.check(POLICY, "amw__write_file", {"path": "/tmp/amw-allowed/../x"}).allowed
    assert not permits.check(POLICY, "amw__write_file", {"path": "/etc/x"}).allowed
    assert not permits.check(POLICY, "amw__read_text_file", {"path": "/tmp/amw-allowed/a"}).allowed
    assert not permits.check(POLICY, "write_file", {}).allowed


def test_hook_blocks_with_reason_and_allows():
    out = hook.decide({"tool_name": "amw__write_file", "tool_input": {"path": "/etc/x"}}, POLICY)
    assert out["decision"] == "block" and "no permit" in out["reason"]
    ok = {"tool_name": "amw__write_file", "tool_input": {"path": "/tmp/amw-allowed/a"}}
    assert hook.decide(ok, POLICY) == {"decision": "allow"}


def test_receipts_are_signed_and_tamper_evident(tmp_path):
    signer = ReceiptSigner(load_or_create_key(tmp_path / "k.pem"), tmp_path / "r.jsonl")
    r = signer.issue(tool="write_file", args_hash="sha256:x", decision="allow", outcome="completed")
    assert {"tool", "args_hash", "decision", "issued_at"} <= r["body"].keys()
    assert verify(r, signer.public_key_b64)
    r["body"]["decision"] = "deny"
    assert not verify(r, signer.public_key_b64)
    assert (tmp_path / "k.pem").stat().st_mode & 0o077 == 0
