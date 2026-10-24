"""Unit tests. Every value comes from the real G1 gate recording or is made at runtime."""

import asyncio
import json
import secrets
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from goose_amw import hook, permits
from goose_amw.canonical import args_hash, canonical_json, idempotency_key, sha256_hex
from goose_amw.ledger import DEFAULT_WINDOW_SECONDS, Ledger
from goose_amw.proxy import Config, Gateway
from goose_amw.receipts import ReceiptSigner, load_or_create_key, verify

FIXTURE = Path(__file__).parent / "fixtures" / "g1_gate_calls.jsonl"
RECORDS = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
WRITES = [r for r in RECORDS if r["tool"] == "write_file"]
READ = next(r for r in RECORDS if r["tool"] != "write_file")
W = WRITES[0]
SESSION = W["meta"]["agent-session-id"]
ARGS = {"path": W["path"]}  # the only argument field the recording holds


def rid(record):
    return record["meta"]["agent-tool-call-request-id"]


def fresh():
    return str(uuid.uuid4())


def test_canonical_hash_ignores_key_order():
    meta = W["meta"]
    reordered = dict(reversed(list(meta.items())))
    assert canonical_json(meta) == canonical_json(reordered)
    assert args_hash(meta) == args_hash(reordered)


def test_key_changes_with_each_input_and_never_uses_request_id(tmp_path):
    caller = fresh()
    k = idempotency_key(caller=caller, tool=W["tool"], arguments=ARGS, session_id=SESSION)
    assert rid(W) not in k
    assert k == idempotency_key(caller=caller, tool=W["tool"], arguments=ARGS, session_id=SESSION)
    for variant in (
        {"caller": fresh(), "tool": W["tool"], "arguments": ARGS, "session_id": SESSION},
        {"caller": caller, "tool": READ["tool"], "arguments": ARGS, "session_id": SESSION},
        {
            "caller": caller,
            "tool": W["tool"],
            "arguments": {"path": str(tmp_path)},
            "session_id": SESSION,
        },
        {"caller": caller, "tool": W["tool"], "arguments": ARGS, "session_id": fresh()},
    ):
        assert idempotency_key(**variant) != k


def test_recorded_gate_retry_maps_to_same_key_despite_new_request_id():
    """Real G1 gate evidence: Goose sent a new request id on the model's retry."""
    assert len(WRITES) == 2
    assert rid(WRITES[0]) != rid(WRITES[1])
    assert all(w["protocol"] == "2026-07-28" for w in WRITES)
    caller = fresh()
    keys = {
        idempotency_key(
            caller=caller,
            tool=w["tool"],
            arguments={"path": w["path"]},
            session_id=w["meta"]["agent-session-id"],
        )
        for w in WRITES
    }
    assert len(keys) == 1


def _gate_key():
    return idempotency_key(caller=fresh(), tool=W["tool"], arguments=ARGS, session_id=SESSION)


def test_ledger_replays_inside_window_and_reruns_after(tmp_path):
    led = Ledger(tmp_path / "l.db")
    key, ahash, t0 = _gate_key(), args_hash(ARGS), W["ts"]
    result = fresh()
    assert (
        led.claim(key, tool=W["tool"], args_hash=ahash, request_id=rid(W), now=t0).action == "run"
    )
    led.finish(key, "completed", result)
    retry = led.claim(
        key, tool=W["tool"], args_hash=ahash, request_id=rid(WRITES[1]), now=WRITES[1]["ts"]
    )
    assert (retry.action, retry.result) == ("replay", result)
    later = t0 + DEFAULT_WINDOW_SECONDS + 1
    assert (
        led.claim(key, tool=W["tool"], args_hash=ahash, request_id=fresh(), now=later).action
        == "run"
    )
    assert led.get(key)["executions"] == 2


def test_failed_unknown_is_never_rerun(tmp_path):
    led = Ledger(tmp_path / "l.db")
    key, ahash, t0 = _gate_key(), args_hash(ARGS), W["ts"]
    led.claim(key, tool=W["tool"], args_hash=ahash, request_id=rid(W), now=t0)
    led.finish(key, "failed_unknown")
    for now in (WRITES[1]["ts"], t0 + 10 * DEFAULT_WINDOW_SECONDS):
        claim = led.claim(key, tool=W["tool"], args_hash=ahash, request_id=rid(WRITES[1]), now=now)
        assert claim.action == "refuse"
    assert led.get(key)["executions"] == 1


def test_crash_in_flight_becomes_failed_unknown(tmp_path):
    led = Ledger(tmp_path / "l.db")
    key = _gate_key()
    led.claim(key, tool=W["tool"], args_hash=args_hash(ARGS), request_id=rid(W))
    assert Ledger(tmp_path / "l.db").mark_stale_in_flight_unknown() == 1
    assert led.get(key)["status"] == "failed_unknown"


def test_definite_failure_may_rerun(tmp_path):
    led = Ledger(tmp_path / "l.db")
    key = _gate_key()
    led.claim(key, tool=W["tool"], args_hash=args_hash(ARGS), request_id=rid(W))
    led.finish(key, "failed")
    claim = led.claim(key, tool=W["tool"], args_hash=args_hash(ARGS), request_id=rid(WRITES[1]))
    assert claim.action == "run"


def test_window_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path / "l.db", window_seconds=0)


def _policy(tmp_path):
    allowed = tmp_path / fresh()
    allowed.mkdir()
    return allowed, {"rules": [{"id": fresh(), "tool": W["tool"], "folder": str(allowed)}]}


def test_permits(tmp_path):
    allowed, policy = _policy(tmp_path)
    inside = str(allowed / fresh())
    prefixed = f"amw__{W['tool']}"
    assert permits.check(policy, prefixed, {"path": inside}).allowed
    assert not permits.check(policy, prefixed, {"path": f"{allowed}/../{fresh()}"}).allowed
    assert not permits.check(policy, prefixed, {"path": str(tmp_path / fresh())}).allowed
    assert not permits.check(policy, f"amw__{READ['tool']}", {"path": inside}).allowed
    assert not permits.check(policy, W["tool"], {}).allowed


def test_hook_blocks_with_reason_and_allows(tmp_path):
    allowed, policy = _policy(tmp_path)
    name = f"amw__{W['tool']}"
    out = hook.decide({"tool_name": name, "tool_input": {"path": str(tmp_path / fresh())}}, policy)
    assert out["decision"] == "block" and "no permit" in out["reason"]
    ok = {"tool_name": name, "tool_input": {"path": str(allowed / fresh())}}
    assert hook.decide(ok, policy) == {"decision": "allow"}


def test_receipts_are_signed_and_tamper_evident(tmp_path):
    signer = ReceiptSigner(load_or_create_key(tmp_path / "k.pem"), tmp_path / "r.jsonl")
    r = signer.issue(
        tool=W["tool"], args_hash=args_hash(ARGS), decision="allow", outcome="completed"
    )
    assert {"tool", "args_hash", "decision", "issued_at"} <= r["body"].keys()
    assert verify(r, signer.public_key_b64)
    r["body"]["decision"] = "deny"
    assert not verify(r, signer.public_key_b64)
    assert (tmp_path / "k.pem").stat().st_mode & 0o077 == 0


def test_write_without_session_id_is_refused_and_not_run(tmp_path):
    allowed, policy = _policy(tmp_path)
    token = secrets.token_hex(24)
    cfg = Config(
        policy=policy,
        state_dir=tmp_path / "state",
        callers={sha256_hex(token): fresh()},
        fs_root=str(tmp_path),
    )
    calls = []

    async def upstream(tool, arguments):
        calls.append(tool)
        return fresh()

    gw = Gateway(cfg, upstream=upstream)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(meta={}),
        headers={"authorization": f"Bearer {token}"},
        protocol_version=None,
    )
    args = {"path": str(allowed / fresh()), "content": fresh()}
    with pytest.raises(ToolError, match="agent-session-id"):
        asyncio.run(gw.handle(W["tool"], args, ctx, write=True))
    assert calls == []
    receipt = json.loads((cfg.state_dir / "receipts.jsonl").read_text().splitlines()[-1])
    assert receipt["body"]["decision"] == "refuse"
    assert receipt["body"]["outcome"] == "not_run"
    assert verify(receipt, gw.signer.public_key_b64)
