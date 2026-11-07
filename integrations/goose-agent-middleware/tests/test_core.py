"""Unit tests. Every value comes from the real G1 gate recording or is made at runtime."""

import asyncio
import json
import secrets
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from goose_amw import hook, permits
from goose_amw.canonical import args_hash, canonical_json, idempotency_key, sha256_hex
from goose_amw.ledger import DEFAULT_WINDOW_SECONDS, Ledger
from goose_amw.proxy import (
    Config,
    Gateway,
    UpstreamError,
    UpstreamNotDispatched,
    UpstreamUnknown,
    stdio_upstream,
    verify_caller,
)
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


def _gateway(tmp_path, upstream):
    allowed, policy = _policy(tmp_path)
    token = secrets.token_hex(24)
    cfg = Config(
        policy=policy,
        state_dir=tmp_path / "state",
        callers={sha256_hex(token): fresh()},
        fs_root=str(tmp_path),
    )
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(meta={"agent-session-id": fresh()}),
        headers={"authorization": f"Bearer {token}"},
        protocol_version=None,
    )
    return (
        Gateway(cfg, upstream=upstream),
        ctx,
        {"path": str(allowed / fresh()), "content": fresh()},
    )


def test_verify_caller_rejects_missing_and_wrong_tokens():
    token = secrets.token_hex(24)
    caller = fresh()
    callers = {sha256_hex(token): caller}
    assert verify_caller({"authorization": f"Bearer {token}"}, callers) == caller
    for headers in (
        None,
        {},
        {"authorization": token},
        {"authorization": f"Bearer {secrets.token_hex(24)}"},
        {"authorization": f"Bearer {token[:-1]}"},
    ):
        with pytest.raises(ToolError, match="caller not verified"):
            verify_caller(headers, callers)


def test_unverified_caller_never_reaches_upstream(tmp_path):
    calls = []

    async def upstream(tool, arguments):
        calls.append(tool)
        return fresh()

    gw, ctx, args = _gateway(tmp_path, upstream)
    ctx.headers = {"authorization": f"Bearer {secrets.token_hex(24)}"}
    with pytest.raises(ToolError, match="caller not verified"):
        asyncio.run(gw.handle(W["tool"], args, ctx, write=True))
    assert calls == []


def test_pre_dispatch_failure_leaves_key_untouched_and_retryable(tmp_path):
    attempts = []

    async def upstream(tool, arguments):
        attempts.append(tool)
        if len(attempts) == 1:
            raise UpstreamNotDispatched(fresh())
        return fresh()

    gw, ctx, args = _gateway(tmp_path, upstream)
    with pytest.raises(ToolError, match="safe to retry"):
        asyncio.run(gw.handle(W["tool"], args, ctx, write=True))
    assert gw.ledger._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    asyncio.run(gw.handle(W["tool"], args, ctx, write=True))
    rows = gw.ledger._db.execute("SELECT status, executions FROM calls").fetchall()
    assert rows == [("completed", 1)]


def test_release_restores_the_previous_row(tmp_path):
    led = Ledger(tmp_path / "l.db")
    key = _gate_key()
    led.claim(key, tool=W["tool"], args_hash=args_hash(ARGS), request_id=rid(W))
    led.finish(key, "failed")
    before = led.get(key)
    claim = led.claim(key, tool=W["tool"], args_hash=args_hash(ARGS), request_id=rid(WRITES[1]))
    led.release(key, claim)
    assert led.get(key) == before


def test_server_that_dies_before_initialize_is_not_dispatched():
    from mcp.client.stdio import StdioServerParameters

    call = stdio_upstream(StdioServerParameters(command=sys.executable, args=["-c", ""]))
    with pytest.raises(UpstreamNotDispatched):
        asyncio.run(asyncio.wait_for(call(W["tool"], ARGS), 30))


def test_handle_denies_unpermitted_path_without_reaching_upstream(tmp_path):
    calls = []

    async def upstream(tool, arguments):
        calls.append(tool)
        return fresh()

    gw, ctx, args = _gateway(tmp_path, upstream)
    for path in (str(tmp_path / fresh()), f"{Path(args['path']).parent}/../{fresh()}"):
        with pytest.raises(ToolError, match="permit denied"):
            asyncio.run(gw.handle(W["tool"], {**args, "path": path}, ctx, write=True))
    assert calls == []
    assert gw.ledger._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    lines = (tmp_path / "state" / "receipts.jsonl").read_text().splitlines()
    for line in lines:
        receipt = json.loads(line)
        assert receipt["body"]["decision"] == "deny"
        assert receipt["body"]["outcome"] == "not_run"
        assert verify(receipt, gw.signer.public_key_b64)
    assert len(lines) == 2


def test_write_error_result_is_failed_unknown_and_not_rerun(tmp_path):
    calls = []

    async def upstream(tool, arguments):
        calls.append(tool)
        raise UpstreamError(fresh())

    gw, ctx, args = _gateway(tmp_path, upstream)
    with pytest.raises(ToolError, match="partly applied"):
        asyncio.run(gw.handle(W["tool"], args, ctx, write=True))
    with pytest.raises(ToolError, match="will not run again"):
        asyncio.run(gw.handle(W["tool"], args, ctx, write=True))
    assert calls == [W["tool"]]
    rows = gw.ledger._db.execute("SELECT status FROM calls").fetchall()
    assert rows == [("failed_unknown",)]


def test_relative_and_home_paths_are_denied(tmp_path, monkeypatch):
    allowed, policy = _policy(tmp_path)
    monkeypatch.chdir(allowed)
    name = fresh()
    for raw in (name, f"./{name}", f"~/{name}"):
        decision = permits.check(policy, W["tool"], {"path": raw})
        assert not decision.allowed
        assert "must be absolute" in decision.reason
    assert permits.check(policy, W["tool"], {"path": str(allowed / name)}).allowed


def _fake_stdio(monkeypatch, result, *, fail_on_exit):
    import contextlib

    import mcp.client.session
    import mcp.client.stdio

    @contextlib.asynccontextmanager
    async def client(params):
        yield (None, None)
        if fail_on_exit:
            raise OSError(fresh())

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, tool, arguments):
            return result

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", client)
    monkeypatch.setattr(mcp.client.session, "ClientSession", Session)


def test_teardown_error_after_result_keeps_the_result(monkeypatch):
    text = fresh()
    result = SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=False)
    _fake_stdio(monkeypatch, result, fail_on_exit=True)
    assert asyncio.run(stdio_upstream(object())(W["tool"], ARGS)) == text


def test_teardown_error_without_result_is_unknown(monkeypatch):
    _fake_stdio(monkeypatch, None, fail_on_exit=True)
    with pytest.raises(UpstreamUnknown):
        asyncio.run(stdio_upstream(object())(W["tool"], ARGS))
