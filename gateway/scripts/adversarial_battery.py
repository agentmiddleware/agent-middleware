#!/usr/bin/env python3
"""Authenticated adversarial battery for the governed trust plane.

Run this against a deployment YOU operate, with YOUR bootstrap key. It
provisions throwaway, low-budget wallets/keys/permits, probes the governed
path for the failure modes that matter, prints a PASS/FAIL/SKIP summary, and
always revokes the keys it minted (even on error).

    export API_URL="https://staging.example.test"   # or pass --api-url
    export BOOTSTRAP_KEY=...     # from Railway variables — never commit or paste
    python3 scripts/adversarial_battery.py

Remote targets require HTTPS (HTTP is loopback-only, so the bootstrap key
never crosses the network in cleartext), and the canonical production origin
additionally requires ``--confirm-production``.

Checks
------
* wallet_isolation      — an agent key reads only its own wallet (403 on others)
* invalid_key           — a bogus key is rejected on a guarded endpoint
* forged_receipt        — verifying an unknown receipt reports valid=false
* permit_key_binding    — a permit is denied for a different key on the SAME
                          wallet (permit_key_mismatch), not merely a wallet
                          mismatch
* expired_permit        — an expired permit cannot be created or used
* revoked_key           — a revoked key stops working immediately
* replay_idempotent     — replaying a governed call reuses the same receipt

Notes
-----
* This creates real rows on the target (tiny throwaway wallets). A target is
  required — there is no default — and production needs explicit
  confirmation, so you can't accidentally hit it.
* MCP-invocation checks require an invokable ``golden-path-echo`` governed tool;
  when the deployment exposes none they are reported SKIP (never a false PASS),
  and a failed ``/mcp/tools.json`` discovery is a FAIL.
* Budget/over-spend containment is NOT exercised here (it needs a tool with a
  known per-call cost); verify it manually against the ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

if __package__:
    from .live_script_target import LiveTargetError, resolve_live_target
else:
    from live_script_target import LiveTargetError, resolve_live_target

# Name the target explicitly — never default to a live host, so an operator who
# exports only BOOTSTRAP_KEY cannot unintentionally provision against production.
# Both are resolved in main(), never at import.
API_URL = ""
BOOTSTRAP_KEY: str | None = None

# Per-run token so idempotency keys and resource names are unique each run;
# otherwise the IdempotencyService cache can short-circuit a repeat run and
# report PASS without re-exercising the governed path.
RUN_ID = uuid.uuid4().hex[:8]

RESULTS: list[tuple[str, str, str]] = []  # (name, PASS/FAIL/SKIP, detail)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The operator authorized only the selected target. Never forward a
        # credential or a mutation to a redirect destination.
        return None


def req(
    method: str,
    path: str,
    key: str | None = None,
    body: dict | None = None,
    extra_headers: dict | None = None,
) -> tuple[int, object]:
    url = f"{API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    if extra_headers:
        headers.update(extra_headers)
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(r, timeout=30) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def record(name: str, passed: bool | None, detail: str) -> None:
    verdict = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
    RESULTS.append((name, verdict, detail))
    print(f"[{verdict}] {name}: {detail}")


def iso_in(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _require(status: int, body: object, what: str) -> object:
    if not 200 <= status < 300:
        print(f"ERROR: {what} failed (status={status}): {body}", file=sys.stderr)
        raise SystemExit(2)
    return body


def _issue_key(wallet_id: str, name: str, issued: list[dict]) -> dict:
    s, k = req(
        "POST",
        "/v1/api-keys",
        BOOTSTRAP_KEY,
        {
            "wallet_id": wallet_id,
            "key_name": name,
            "expires_in_days": 1,
        },
    )
    _require(s, k, f"issue key {name}")
    entry = {
        "wallet_id": wallet_id,
        "key_id": k["key_id"],
        "api_key": k["api_key"],
        "label": name,
    }
    issued.append(entry)  # track before anything else can fail, so cleanup sees it
    return entry


def provision(issued: list[dict]) -> tuple[str, dict, dict, dict, dict]:
    """Create sponsor + two agent wallets, keys, and a scoped permit.

    Agent A gets two keys: key1 binds the permit; key2 is a different key on the
    SAME wallet, used to isolate permit_key_mismatch from permit_wallet_mismatch.
    """
    s, sponsor = req(
        "POST",
        "/v1/billing/wallets/sponsor",
        BOOTSTRAP_KEY,
        {
            "sponsor_name": f"adv-battery-{RUN_ID}",
            "email": "adv@example.com",
            "initial_credits": 500,
            "require_kyc": False,
        },
    )
    _require(s, sponsor, "create sponsor wallet")
    sponsor_id = sponsor["wallet_id"]

    def agent_wallet(i: int) -> str:
        s, a = req(
            "POST",
            "/v1/billing/wallets/agent",
            BOOTSTRAP_KEY,
            {
                "sponsor_wallet_id": sponsor_id,
                "agent_id": f"adv-agent-{i}-{RUN_ID}",
                "budget_credits": 100,
                "daily_limit": 50,
            },
        )
        _require(s, a, f"create agent wallet {i}")
        return a["wallet_id"]

    a_wallet = agent_wallet(1)
    b_wallet = agent_wallet(2)
    a_key1 = _issue_key(a_wallet, f"adv-a-key1-{RUN_ID}", issued)
    a_key2 = _issue_key(a_wallet, f"adv-a-key2-{RUN_ID}", issued)
    b_key = _issue_key(b_wallet, f"adv-b-key-{RUN_ID}", issued)

    s, permit = req(
        "POST",
        "/v1/permits",
        BOOTSTRAP_KEY,
        {
            "issuer_wallet_id": a_wallet,
            "subject_wallet_id": a_wallet,
            "subject_key_id": a_key1["key_id"],
            "allowed_tools": ["golden-path-echo"],
            "scopes": ["tool:golden-path-echo:invoke", "billing:charge"],
            "max_credits": 5,
            "expires_at": iso_in(30),
        },
        {"Idempotency-Key": f"adv-permit-{RUN_ID}"},
    )
    _require(s, permit, "issue scoped permit")
    if not permit.get("permit_id"):
        print(f"ERROR: permit response missing permit_id: {permit}", file=sys.stderr)
        raise SystemExit(2)

    a = {"wallet_id": a_wallet, "key1": a_key1, "key2": a_key2}
    b = {"wallet_id": b_wallet, "key": b_key}
    return sponsor_id, a, b, permit, {}


def revoke_all(issued: list[dict]) -> None:
    print("\n=== cleanup ===")
    for k in issued:
        s, _ = req(
            "DELETE", f"/v1/api-keys/{k['wallet_id']}/{k['key_id']}", BOOTSTRAP_KEY
        )
        if s in (200, 202, 204):
            status = "revoked"
        elif s in (404, 410):
            status = "already-gone"
        else:
            status = f"FAILED(status={s}) — revoke manually"
        print(f"  {k['label']}: {status}")


def discover_echo_tool(agent_key: str) -> bool | None:
    """True if golden-path-echo is invokable, False if discovery worked but the
    tool is absent, None if discovery itself failed (a hard error)."""
    s, tools = req("GET", "/mcp/tools.json", agent_key)
    if not 200 <= s < 300:
        record("mcp_discovery", False, f"/mcp/tools.json status={s} (discovery failed)")
        return None
    names = (
        [t.get("name") for t in tools.get("tools", []) if isinstance(t, dict)]
        if isinstance(tools, dict)
        else []
    )
    return "golden-path-echo" in names


def invoke(
    agent_key: str, wallet_id: str, permit_id: str, idem: str
) -> tuple[int, object]:
    return req(
        "POST",
        "/mcp/messages",
        agent_key,
        {
            "jsonrpc": "2.0",
            "id": idem,
            "method": "tools/call",
            "params": {
                "name": "golden-path-echo",
                "arguments": {"message": "adv"},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idem,
                },
            },
        },
    )


def run_checks(sponsor_id: str, a: dict, b: dict, permit: dict) -> None:
    permit_id = permit["permit_id"]
    a_key1, a_key2 = a["key1"]["api_key"], a["key2"]["api_key"]

    # 1. Cross-wallet isolation: agent A's key reads only its own wallet.
    s1, _ = req("GET", f"/v1/billing/wallets/{sponsor_id}", a_key1)
    s2, _ = req("GET", f"/v1/billing/wallets/{b['wallet_id']}", a_key1)
    s3, _ = req("GET", f"/v1/billing/wallets/{a['wallet_id']}", a_key1)
    record(
        "wallet_isolation",
        s1 == 403 and s2 == 403 and s3 == 200,
        f"sponsor={s1} otherAgent={s2} ownWallet={s3} (want 403/403/200)",
    )

    # 2. Invalid key rejected on a guarded endpoint.
    s, _ = req("GET", "/v1/permits", "totally-invalid-key-0000")
    record("invalid_key_rejected", s in (401, 403), f"status={s} (want 401/403)")

    # 3. Forged receipt reports valid=false. An unknown receipt_id routes through
    #    require_bootstrap_admin, so this probe uses the bootstrap key and reads
    #    the `valid` field (not a status code).
    s, body = req(
        "POST",
        "/v1/receipts/verify",
        BOOTSTRAP_KEY,
        {"receipt_id": f"rcpt_forged_{RUN_ID}"},
    )
    valid = body.get("valid") if isinstance(body, dict) else None
    record(
        "forged_receipt_rejected",
        s == 200 and valid is False,
        f"status={s} valid={valid} (want 200 / valid=False)",
    )

    # Discover the governed tool once; MCP-dependent checks gate on it.
    echo = discover_echo_tool(a_key1)

    # 4. Permit/key binding: a DIFFERENT key on the SAME wallet must be denied
    #    with permit_key_mismatch (wallet matches, so this is not a wallet
    #    mismatch). Requires the tool so the denial isn't a "Tool not found".
    if echo is None:
        record("permit_key_binding", None, "skipped: MCP discovery failed")
    elif not echo:
        record("permit_key_binding", None, "skipped: golden-path-echo not exposed")
    else:
        s, body = invoke(a_key2, a["wallet_id"], permit_id, f"adv-bind-{RUN_ID}")
        blob = json.dumps(body) if not isinstance(body, str) else body
        record(
            "permit_key_binding",
            "permit_key_mismatch" in blob,
            f"status={s} reason~={_short(blob)} (want permit_key_mismatch)",
        )

    # 5. Expired permit cannot be created or used.
    s, exp = req(
        "POST",
        "/v1/permits",
        BOOTSTRAP_KEY,
        {
            "issuer_wallet_id": a["wallet_id"],
            "subject_wallet_id": a["wallet_id"],
            "subject_key_id": a["key1"]["key_id"],
            "allowed_tools": ["golden-path-echo"],
            "scopes": ["tool:golden-path-echo:invoke"],
            "max_credits": 5,
            "expires_at": iso_in(-30),
        },
        {"Idempotency-Key": f"adv-permit-expired-{RUN_ID}"},
    )
    if s >= 400:
        record("expired_permit_denied", True, f"creation rejected status={s}")
    elif not echo:
        record(
            "expired_permit_denied",
            None,
            "creation accepted; invoke check skipped (tool unavailable/discovery failed)",
        )
    else:
        si, bi = invoke(
            a_key1, a["wallet_id"], exp.get("permit_id"), f"adv-exp-{RUN_ID}"
        )
        blob = json.dumps(bi) if not isinstance(bi, str) else bi
        record(
            "expired_permit_denied",
            "expire" in blob.lower(),
            f"invoke status={si} reason~={_short(blob)} (want an expiry denial)",
        )

    # 6. Revocation containment: revoke agent B's key, then reuse must fail.
    req("DELETE", f"/v1/api-keys/{b['wallet_id']}/{b['key']['key_id']}", BOOTSTRAP_KEY)
    s, _ = req("GET", f"/v1/billing/wallets/{b['wallet_id']}", b["key"]["api_key"])
    record(
        "revoked_key_denied", s in (401, 403), f"status={s} after revoke (want 401/403)"
    )

    # 7. Replay / idempotency (needs an invokable tool).
    if echo is None:
        record("replay_idempotent", None, "skipped: MCP discovery failed")
    elif not echo:
        record("replay_idempotent", None, "skipped: golden-path-echo not exposed")
    else:
        idem = f"adv-replay-{RUN_ID}"
        _, r1 = invoke(a_key1, a["wallet_id"], permit_id, idem)
        _, r2 = invoke(a_key1, a["wallet_id"], permit_id, idem)

        def rid(x):
            return (
                (x or {}).get("result", {}).get("receipt", {}).get("receipt_id")
                if isinstance(x, dict)
                else None
            )

        same = rid(r1) and rid(r1) == rid(r2)
        record(
            "replay_idempotent",
            bool(same),
            f"receipt1={rid(r1)} receipt2={rid(r2)} (replay must reuse the receipt)",
        )


def _short(text: str, n: int = 120) -> str:
    return text[:n]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the authenticated adversarial battery against an explicit target."
    )
    parser.add_argument("--api-url", help="API origin; overrides API_URL")
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="confirm intentional use of https://api.thisisatest.tech",
    )
    args = parser.parse_args(argv)

    target = args.api_url if args.api_url is not None else os.environ.get("API_URL")
    if not target:
        print(
            "ERROR: set API_URL (or pass --api-url) to the deployment you want "
            "to test, e.g. export API_URL=https://staging.example.test",
            file=sys.stderr,
        )
        return 2
    try:
        # environ={}: this script names its target with API_URL only, never
        # the AGENT_MIDDLEWARE_API_URL the shared resolver would fall back to.
        api_url = resolve_live_target(
            target, confirm_production=args.confirm_production, environ={}
        )
    except LiveTargetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    bootstrap_key = os.environ.get("BOOTSTRAP_KEY")
    if not bootstrap_key:
        print(
            "ERROR: set BOOTSTRAP_KEY (operator key from Railway variables).",
            file=sys.stderr,
        )
        return 2

    global API_URL, BOOTSTRAP_KEY
    API_URL = api_url
    BOOTSTRAP_KEY = bootstrap_key
    print(f"Target: {API_URL}  (run {RUN_ID})\n")

    issued: list[dict] = []
    try:
        sponsor_id, a, b, permit, _ = provision(issued)
        run_checks(sponsor_id, a, b, permit)
    finally:
        revoke_all(issued)

    print("\n=== SUMMARY ===")
    for name, verdict, _ in RESULTS:
        print(f"  {verdict:4}  {name}")
    failed = [n for n, v, _ in RESULTS if v == "FAIL"]
    print(f"\n{len(RESULTS)} checks — {len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
