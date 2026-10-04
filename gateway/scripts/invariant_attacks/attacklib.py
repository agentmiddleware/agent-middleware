"""Adversarial invariant-testing harness for the local trust plane.

Stdlib-only (urllib + threading + sqlite3) so it is trivially reproducible.
Fires real HTTP against http://127.0.0.1:8000, captures exact request/response
pairs, and reconciles committed state by reading the SQLite database directly
(non-mutating) for the crash-consistency attack.

Never prints full API keys; keys are redacted to a short prefix in any
captured evidence.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import os

# All configurable via env so the harness reproduces against a stock
# `make quickstart`. Defaults resolve relative to the repo root (two levels up
# from this file), so the scripts work regardless of the current directory.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
API = os.environ.get("API_URL", "http://127.0.0.1:8000")
DB_PATH = os.environ.get(
    "TP_DB_PATH", os.path.join(_REPO_ROOT, "data/quickstart/api.db")
)
NOTES_PATH = os.environ.get(
    "TP_NOTES_PATH", os.path.join(_REPO_ROOT, "data/dogfood_partner_notes.jsonl")
)


def verdict_exit_code(verdict: str) -> int:
    """Return success only when the invariant verdict is exactly HELD."""
    return 0 if verdict == "HELD" else 1


def redact(key: str | None) -> str:
    if not key:
        return "<none>"
    return key[:8] + "..." if len(key) > 8 else key


def signed_receipt_tamper_cases(
    bundle: dict, *, fallback_wallet_id: str | None = None
) -> dict[str, tuple[str, str]]:
    """Return representative high-value signed-field mutations.

    Missing dynamic fields deliberately produce a pattern that cannot occur in a
    valid signing input. The attack scripts then record the case as skipped and
    fail closed instead of silently reducing their forgery coverage.
    """

    receipt = bundle.get("receipt") if isinstance(bundle.get("receipt"), dict) else {}
    try:
        signed = json.loads(bundle.get("signing_input") or "{}")
    except (TypeError, json.JSONDecodeError):
        signed = {}
    if not isinstance(signed, dict):
        signed = {}
    wallet_id = (
        signed.get("wallet_id")
        or receipt.get("wallet_id")
        or fallback_wallet_id
        or "<missing-wallet-id>"
    )
    ledger_entry_id = (
        signed.get("ledger_entry_id")
        or receipt.get("ledger_entry_id")
        or "<missing-ledger-entry-id>"
    )
    return {
        "credits_charged": ('"credits_charged":"2"', '"credits_charged":"0"'),
        "outcome": ('"outcome":"success"', '"outcome":"denied"'),
        "tool": ("partner.notes.write", "evil.tool.exfiltrate"),
        "wallet_id": (wallet_id, "agt-forged-wallet"),
        "ledger_entry_id": (ledger_entry_id, "ledger-forged-entry"),
    }


def _headers(api_key: str | None, idem: str | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key is not None:
        h["X-API-Key"] = api_key
    if idem is not None:
        h["Idempotency-Key"] = idem
    return h


def http(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    body: dict | None = None,
    idem: str | None = None,
    extra_headers: dict | None = None,
) -> dict:
    """Perform one HTTP call. Returns {status, json|text, error}."""
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    headers = _headers(api_key, idem)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    except Exception as e:  # connection reset etc. (crash test)
        return {
            "status": None,
            "error": repr(e),
            "latency_ms": round((time.time() - t0) * 1000, 1),
        }
    out = {"status": status, "latency_ms": round((time.time() - t0) * 1000, 1)}
    try:
        out["json"] = json.loads(raw)
    except Exception:
        out["text"] = raw
    return out


# ---- trust-plane convenience wrappers --------------------------------------


def provision(agent_id: str) -> dict:
    r = http("POST", "/v1/dev-keys/self-provision", body={"agent_id": agent_id})
    j = r["json"]
    return {
        "api_key": j["api_key"],
        "wallet_id": j["wallet_id"],
        "key_id": j["key_id"],
        "sponsor_wallet_id": j["sponsor_wallet_id"],
        "key_prefix": j.get("key_prefix"),
    }


def expires_in(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def issue_permit(
    cred: dict,
    *,
    allowed_tools,
    scopes,
    max_credits,
    idem,
    expires=None,
    subject_wallet_id=None,
    subject_key_id=None,
    issuer_wallet_id=None,
) -> dict:
    body = {
        "issuer_wallet_id": issuer_wallet_id or cred["wallet_id"],
        "subject_wallet_id": subject_wallet_id or cred["wallet_id"],
        "subject_key_id": subject_key_id or cred["key_id"],
        "allowed_tools": allowed_tools,
        "scopes": scopes,
        "max_credits": max_credits,
        "expires_at": expires or expires_in(30),
    }
    return http("POST", "/v1/permits", api_key=cred["api_key"], idem=idem, body=body)


def invoke_body(wallet_id, permit_id, idem_key, text, rpc_id="call"):
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {
            "name": "partner.notes.write",
            "arguments": {"text": text},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idem_key,
            },
        },
    }


def invoke(api_key, wallet_id, permit_id, idem_key, text, rpc_id="call") -> dict:
    return http(
        "POST",
        "/mcp/messages",
        api_key=api_key,
        body=invoke_body(wallet_id, permit_id, idem_key, text, rpc_id),
    )


def ledger(cred: dict) -> dict:
    return http(
        "GET", f"/v1/billing/ledger/{cred['wallet_id']}", api_key=cred["api_key"]
    )


def receipt_of(resp: dict):
    """Return the signed receipt whether success (result.receipt) or a governed
    denial (error.data.receipt over the JSON-RPC transport)."""
    j = resp.get("json") or {}
    try:
        return j["result"]["receipt"]
    except Exception:
        pass
    try:
        return j["error"]["data"]["receipt"]
    except Exception:
        return None


def outcome_of(resp: dict):
    r = receipt_of(resp)
    return r.get("outcome") if r else None


def reason_of(resp: dict):
    r = receipt_of(resp)
    if r and r.get("reason_code"):
        return r["reason_code"]
    # denials may also surface as a jsonrpc error object; prefer the human
    # message string (e.g. "idempotency_key_reused") over the numeric code.
    j = resp.get("json") or {}
    if "error" in j and isinstance(j["error"], dict):
        return j["error"].get("message") or j["error"].get("code")
    if "error" in j:
        return j.get("error")
    if "detail" in j:
        d = j["detail"]
        if isinstance(d, dict):
            return d.get("error") or d.get("reason") or d.get("message")
        return d
    return None


# ---- concurrency ------------------------------------------------------------


def fire_parallel(n: int, fn):
    """Run fn(i) on n OS threads that all release from a barrier simultaneously.

    Returns results ordered by launch index. True wall-clock simultaneity so
    check-then-act races (if any) are actually exercised.
    """
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = fn(i)

    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(worker, i) for i in range(n)]
        for f in futs:
            f.result()
    return results


# ---- non-mutating DB reconciliation (crash test) ---------------------------


def db_rows(query: str, params: tuple = ()):  # read committed state directly
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(query, params).fetchall()]
    finally:
        con.close()


def reconcile_wallet(wallet_id: str, endpoint: str = "/mcp/messages"):
    """Return committed idempotency records, receipts, ledger debits for a wallet
    and a per-idempotency-key pairing check (charge <-> receipt)."""
    idem = db_rows(
        "SELECT record_id, idempotency_key, request_hash, status_code, ledger_entry_id, response_reference "
        "FROM idempotency_records WHERE wallet_id=? AND endpoint=? ORDER BY created_at",
        (wallet_id, endpoint),
    )
    rcpts = db_rows(
        "SELECT receipt_id, idempotency_record_id, ledger_entry_id, credits_charged, outcome, reason_code "
        "FROM receipts WHERE wallet_id=? ORDER BY created_at",
        (wallet_id,),
    )
    debits = db_rows(
        "SELECT entry_id, amount, operation_key, description FROM ledger_entries "
        "WHERE wallet_id=? AND action='debit' ORDER BY timestamp",
        (wallet_id,),
    )
    return {"idempotency_records": idem, "receipts": rcpts, "debits": debits}


def notes_count_for(marker: str | None = None) -> int:
    """Count notes in the dogfood side-effect file (optionally matching a text marker)."""
    try:
        with open(NOTES_PATH, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
    except FileNotFoundError:
        return 0
    if marker is None:
        return len(lines)
    n = 0
    for ln in lines:
        try:
            if marker in json.loads(ln).get("text", ""):
                n += 1
        except Exception:
            pass
    return n
