"""Live conformance suite for the governed MCP trust plane.

Unlike ``stress_test_live.py`` (edge-case / load fuzzing), this suite asserts
the *security and correctness invariants* the product sells, each as a discrete
pass/fail check with a clear failure message. It exits non-zero if any invariant
is violated, so it can gate a staging deployment in CI.

What it verifies
----------------
  * Golden path: discover -> wallet hierarchy -> scoped permit -> governed
    invoke -> signed receipt -> receipt verifies -> audit chain verifies.
  * Exactly-once: a completed replay returns the *same* receipt; concurrent
    identical requests expose only that receipt identity or an explicit
    in-progress response; and the operation is charged once.
  * Budget: a permit denies the invocation that would exceed max_credits.
  * Permit lifecycle: expired and forged permits are rejected.
  * Tenant isolation (the strongest check here): a scoped API key bound to
    wallet B cannot read or spend a permit issued for wallet A, even when it
    supplies A's wallet_id and permit_id directly in the request body. This is
    the authorization-bypass surface that matters most for a multi-tenant
    trust plane.

Blast radius
------------
Creates persistent test data (wallets, permits, receipts, and one scoped API
key), all tagged with a per-run id. The suite has no cleanup and never deletes
pre-existing data. Point it at staging unless you intend to retain its test
data in production.

Usage
-----
    export AGENT_MIDDLEWARE_API_KEY=...          # a bootstrap/admin key
    export AGENT_MIDDLEWARE_API_URL=https://...  # required unless --api-url is set
    python scripts/trust_plane_conformance.py

Pass ``--confirm-production`` when intentionally targeting the canonical
production origin. Remote targets require HTTPS; loopback targets may use HTTP.

The key is never defaulted or hardcoded; the suite exits if it is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

if __package__:
    from .live_script_target import LiveTargetError, resolve_live_target
else:
    from live_script_target import LiveTargetError, resolve_live_target

API_URL = ""
API_KEY = ""

# Per-run namespace so the suite is re-runnable: fixed Idempotency-Keys would
# otherwise replay a previous run's cached responses instead of re-testing.
RUN = os.environ.get("CONFORMANCE_RUN_ID") or uuid.uuid4().hex[:12]
TAG = f"conf-{RUN}"

# Cost of one partner.notes.write invocation, in credits. Asserted by the
# budget/no-double-charge checks; kept in one place so a pricing change is a
# one-line update here rather than scattered magic numbers.
COST_PER_CALL = 2


@dataclass
class Suite:
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        tail = f"  — {detail}" if detail else ""
        if ok:
            self.passed += 1
            print(f"[PASS] {name}{tail}")
        else:
            self.failed += 1
            self.failures.append(name)
            print(f"[FAIL] {name}{tail}")
        return ok


def admin_headers(extra: dict | None = None) -> dict:
    h = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def key_headers(key: str, extra: dict | None = None) -> dict:
    h = {"X-API-Key": key, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def invoke_body(wallet_id: str, permit_id: str, text: str, req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": "partner.notes.write",
            "arguments": {"text": text},
            "mcpContext": {"wallet_id": wallet_id, "permit_id": permit_id},
        },
    }


async def make_permit(
    c, admin, spn, wallet, name, *, max_credits="20", expires="2099-01-01T00:00:00Z"
):
    r = await c.post(
        "/v1/permits",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-{name}"}),
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": wallet,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": max_credits,
            "expires_at": expires,
        },
    )
    return r


async def run(c: httpx.AsyncClient, s: Suite) -> None:
    admin = admin_headers()

    # ---- Setup: sponsor + two isolated agent wallets ---------------------
    r = await c.post(
        "/v1/billing/wallets/sponsor",
        headers=admin,
        json={
            "sponsor_name": TAG,
            "email": f"{TAG}@example.com",
            "initial_credits": 500,
        },
    )
    if not s.check("setup: create sponsor", r.status_code == 201, r.text[:120]):
        return
    spn = r.json()["wallet_id"]

    wallets = {}
    for role in ("a", "b"):
        r = await c.post(
            "/v1/billing/wallets/agent",
            headers=admin,
            json={
                "sponsor_wallet_id": spn,
                "agent_id": f"{TAG}-{role}",
                "budget_credits": 100,
            },
        )
        if not s.check(
            f"setup: create agent wallet {role}", r.status_code == 201, r.text[:120]
        ):
            return
        wallets[role] = r.json()["wallet_id"]
    wallet_a, wallet_b = wallets["a"], wallets["b"]

    # A scoped, DB-backed API key bound to wallet B — the "other tenant".
    r = await c.post(
        "/v1/api-keys",
        headers=admin,
        json={"wallet_id": wallet_b, "key_name": f"{TAG}-b"},
    )
    if not s.check(
        "setup: mint scoped key for wallet B",
        r.status_code == 201,
        f"HTTP {r.status_code}",
    ):
        return
    key_b = r.json()["api_key"]

    # ---- Golden path -----------------------------------------------------
    r = await c.get("/v1/discover")
    s.check("discover returns capability index", r.status_code == 200)

    r = await make_permit(c, admin, spn, wallet_a, "golden")
    s.check("issue scoped permit for wallet A", r.status_code == 201, r.text[:120])
    permit = r.json()
    pid = permit["permit_id"]
    s.check("permit carries a nonce", bool(permit.get("nonce")))

    # Keep the exact body: a true replay is byte-identical. The idempotency
    # layer keys on (wallet, endpoint, key) and hashes the payload, so resending
    # a *different* payload under the same key is a conflict, not a replay —
    # asserted separately below.
    golden_body = invoke_body(wallet_a, pid, f"golden {RUN}")
    r = await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-inv1"}),
        json=golden_body,
    )
    body = r.json()
    rcpt = body.get("result", {}).get("receipt", {})
    rid = rcpt.get("receipt_id")
    s.check(
        "governed invoke succeeds",
        "error" not in body and bool(rid),
        rid or str(body)[:120],
    )
    s.check(
        "receipt is signed",
        bool(rcpt.get("signature") or rcpt.get("signature_key_id")),
        f"key_id={rcpt.get('signature_key_id')}",
    )

    # ---- Exactly-once (sequential) --------------------------------------
    r = await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-inv1"}),
        json=golden_body,
    )
    rid2 = r.json().get("result", {}).get("receipt", {}).get("receipt_id")
    s.check("replay returns identical receipt", rid2 == rid, f"{rid} vs {rid2}")

    r = await c.get(f"/v1/permits/{pid}", headers=admin)
    spent = str(r.json().get("spent_credits"))
    s.check(
        "replay did not double-charge",
        float(spent) == COST_PER_CALL,
        f"spent={spent}, expected {COST_PER_CALL}",
    )

    # Same key, different payload -> must NOT be silently served the cached
    # receipt, and must NOT charge again. A conflict error is the correct answer.
    r = await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-inv1"}),
        json=invoke_body(wallet_a, pid, "DIFFERENT payload under same key"),
    )
    j = r.json()
    served_cached = j.get("result", {}).get("receipt", {}).get("receipt_id") == rid
    s.check(
        "same key + different payload is not served the cached receipt",
        not served_cached,
        str(j.get("error") or j.get("result"))[:120],
    )
    r = await c.get(f"/v1/permits/{pid}", headers=admin)
    s.check(
        "payload-mismatch replay did not charge again",
        float(str(r.json().get("spent_credits"))) == COST_PER_CALL,
        f"spent={r.json().get('spent_credits')}",
    )

    # ---- Exactly-once (concurrent) --------------------------------------
    # Fresh permit so this check is independent of the counter above.
    r = await make_permit(c, admin, spn, wallet_a, "concurrent")
    pid_c = r.json()["permit_id"]
    key = f"{TAG}-concurrent-once"
    concurrent_body = invoke_body(
        wallet_a, pid_c, f"concurrent identical {RUN}", req_id=15
    )

    async def fire():
        return await c.post(
            "/mcp/messages",
            headers=admin_headers({"Idempotency-Key": key}),
            json=concurrent_body,
        )

    responses = await asyncio.gather(
        *[fire() for _ in range(15)], return_exceptions=True
    )
    rids, receipt_responses, in_progress, errors = set(), 0, 0, 0
    for resp in responses:
        if isinstance(resp, Exception):
            errors += 1
            continue
        j = resp.json()
        got = j.get("result", {}).get("receipt", {}).get("receipt_id")
        if got:
            receipt_responses += 1
            rids.add(got)
        elif j.get("error", {}).get("message") == "idempotency_in_progress":
            in_progress += 1
        else:
            errors += 1
    s.check(
        "15 concurrent identical invokes expose one receipt identity or in-progress",
        len(rids) == 1
        and receipt_responses + in_progress == len(responses)
        and errors == 0,
        (
            f"receipt_responses={receipt_responses} in_progress={in_progress} "
            f"distinct_receipts={len(rids)} errors={errors}"
        ),
    )

    r = await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": key}),
        json=concurrent_body,
    )
    completed_rid = r.json().get("result", {}).get("receipt", {}).get("receipt_id")
    s.check(
        "post-completion replay returns the concurrent winner's receipt",
        bool(completed_rid) and completed_rid in rids,
        f"winner={next(iter(rids), None)} replay={completed_rid}",
    )

    r = await c.get(f"/v1/permits/{pid_c}", headers=admin)
    spent_c = str(r.json().get("spent_credits"))
    s.check(
        "concurrent replays charged exactly once",
        float(spent_c) == COST_PER_CALL,
        f"spent={spent_c}, expected {COST_PER_CALL}",
    )

    # ---- Budget enforcement ---------------------------------------------
    r = await make_permit(
        c, admin, spn, wallet_a, "budget", max_credits=str(COST_PER_CALL)
    )
    pid_b = r.json()["permit_id"]
    # First call consumes the whole budget.
    await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-budget-1"}),
        json=invoke_body(wallet_a, pid_b, "fills budget"),
    )
    # Second call must be denied.
    r = await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-budget-2"}),
        json=invoke_body(wallet_a, pid_b, "over budget"),
    )
    err = r.json().get("error", {})
    s.check(
        "over-budget invoke denied",
        err.get("message") == "permit_budget_exceeded",
        err.get("message"),
    )

    # ---- Permit lifecycle: expired & forged -----------------------------
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    r = await make_permit(c, admin, spn, wallet_a, "expired", expires=past)
    s.check(
        "permit expiring in the past is rejected",
        r.status_code == 400 or "permit_expired" in r.text,
        f"HTTP {r.status_code}",
    )

    r = await c.post(
        "/mcp/messages",
        headers=admin_headers({"Idempotency-Key": f"{TAG}-forged"}),
        json=invoke_body(wallet_a, "permit-forged000000000", "forged"),
    )
    err = r.json().get("error", {})
    s.check(
        "forged permit id is rejected",
        err.get("message") in ("permit_not_found", "permit_required"),
        err.get("message"),
    )

    # ---- Verification surface -------------------------------------------
    r = await c.post("/v1/receipts/verify", headers=admin, json={"receipt_id": rid})
    vr = r.json()
    s.check(
        "receipt verifies as valid",
        r.status_code == 200 and vr.get("valid") is True,
        f"reason={vr.get('reason')}",
    )

    r = await c.get(f"/v1/receipts/{rid}/evidence", headers=admin)
    s.check("evidence bundle retrievable", r.status_code == 200)

    r = await c.post("/v1/audit/verify-chain", headers=admin, json={})
    av = r.json() if r.status_code == 200 else {}
    s.check(
        "audit chain verifies clean",
        r.status_code == 200 and (av.get("valid") is True or av.get("ok") is True),
        f"checked_events={av.get('checked_events')}",
    )

    # ---- Tenant isolation (the crux) ------------------------------------
    # Wallet B's scoped key must not touch wallet A's permit, even when it
    # supplies A's ids directly. A permit issued for A is still active here.
    r = await make_permit(c, admin, spn, wallet_a, "victim")
    pid_v = r.json()["permit_id"]

    r = await c.get(f"/v1/permits/{pid_v}", headers=key_headers(key_b))
    s.check(
        "scoped key cannot READ another tenant's permit",
        r.status_code in (403, 404),
        f"HTTP {r.status_code}",
    )

    r = await c.post(
        "/mcp/messages",
        headers=key_headers(key_b, {"Idempotency-Key": f"{TAG}-crosstenant"}),
        json=invoke_body(wallet_a, pid_v, "cross-tenant spend"),
    )
    err = r.json().get("error", {})
    denied = err.get("message") in ("wallet_access_denied", "permit_wallet_mismatch")
    s.check(
        "scoped key cannot SPEND another tenant's permit", denied, err.get("message")
    )

    # And the victim's permit must be untouched by the denied attempt.
    r = await c.get(f"/v1/permits/{pid_v}", headers=admin)
    s.check(
        "denied cross-tenant attempt did not charge the victim",
        float(str(r.json().get("spent_credits"))) == 0.0,
        f"spent={r.json().get('spent_credits')}",
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run live trust-plane conformance checks against an explicit target."
    )
    parser.add_argument(
        "--api-url",
        help="API origin; overrides AGENT_MIDDLEWARE_API_URL",
    )
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="confirm intentional use of https://api.thisisatest.tech",
    )
    args = parser.parse_args(argv)

    try:
        api_url = resolve_live_target(
            args.api_url,
            confirm_production=args.confirm_production,
        )
    except LiveTargetError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    api_key = (os.environ.get("AGENT_MIDDLEWARE_API_KEY") or "").strip()
    if not api_key:
        print(
            "AGENT_MIDDLEWARE_API_KEY is not set. Export a key for the target "
            "deployment before running; credentials are never hardcoded.",
            file=sys.stderr,
        )
        return 2

    global API_KEY, API_URL
    API_URL = api_url
    API_KEY = api_key

    s = Suite()
    print("=" * 64)
    print("TRUST PLANE CONFORMANCE SUITE")
    print(f"Target: {API_URL}")
    print(f"Run tag: {TAG}")
    print("=" * 64)
    async with httpx.AsyncClient(base_url=API_URL, timeout=45) as c:
        try:
            await run(c, s)
        except Exception as exc:  # noqa: BLE001 — a crash is a suite failure
            s.check(
                f"suite completed without unhandled error ({type(exc).__name__})",
                False,
                str(exc),
            )

    print("\n" + "=" * 64)
    print(f"RESULT: {s.passed}/{s.passed + s.failed} invariants held")
    if s.failures:
        print("FAILED:")
        for name in s.failures:
            print(f"  - {name}")
    print("=" * 64)
    return 1 if s.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
