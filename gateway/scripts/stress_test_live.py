"""Hyper-edge-case stress test against live trust plane.

Usage:
    export AGENT_MIDDLEWARE_API_KEY=...        # required, never hardcode
    export AGENT_MIDDLEWARE_API_URL=https://...  # required unless --api-url is set
    python scripts/stress_test_live.py

Pass ``--confirm-production`` when intentionally targeting the canonical
production origin. Remote targets require HTTPS; loopback targets may use HTTP.

This script creates persistent wallets, permits, and receipts on its target. It
has no cleanup. Point it at staging unless you intend to retain its test data in
production.
"""

import argparse
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

if __package__:
    from .live_script_target import LiveTargetError, resolve_live_target
else:
    from live_script_target import LiveTargetError, resolve_live_target

API_URL = ""
API_KEY = ""

# Every idempotency key is namespaced with a fresh per-run prefix. Without this
# a second run replays the first run's cached responses instead of re-testing,
# so the suite would silently stop exercising the code paths it claims to cover.
RUN_ID = os.environ.get("STRESS_RUN_ID") or uuid.uuid4().hex[:12]


def ikey(name: str) -> str:
    """Build a run-scoped Idempotency-Key."""
    return f"stress-{RUN_ID}-{name}"


# Rate-limiting semaphore
SEM = asyncio.Semaphore(10)


def require(condition: object, message: str) -> None:
    """Fail the run on a broken expectation.

    An explicit raise, unlike a bare assert statement, survives python -O.
    """
    if not condition:
        raise AssertionError(message)


def is_zero_credits(value: object) -> bool:
    try:
        return Decimal(str(value)) == 0
    except InvalidOperation:
        return False


def successful_receipt_id(response: httpx.Response) -> str:
    """Require evidence of a successful governed invocation or replay."""
    require(response.status_code == 200, f"invoke returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise AssertionError("invoke returned invalid JSON") from exc
    require(
        isinstance(body, dict)
        and body.get("jsonrpc") == "2.0"
        and "id" in body
        and "error" not in body,
        "invoke did not return a successful JSON-RPC response",
    )
    result = body.get("result")
    require(isinstance(result, dict), "invoke result is missing or malformed")
    receipt = result.get("receipt")
    require(isinstance(receipt, dict), "invoke receipt is missing or malformed")
    receipt_id = receipt.get("receipt_id")
    require(
        isinstance(receipt_id, str) and receipt_id.strip(),
        "invoke receipt ID is missing or malformed",
    )
    return receipt_id


async def req(method, path, **kwargs):
    async with SEM:
        async with httpx.AsyncClient(base_url=API_URL, timeout=30) as c:
            headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
            headers.update(kwargs.pop("headers", {}))
            return await c.request(method, path, headers=headers, **kwargs)


async def setup_wallets():
    """Create sponsor + agent for stress tests."""
    sponsor = await req(
        "POST",
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Stress",
            "email": "stress@example.com",
            "initial_credits": 10000,
        },
    )
    assert sponsor.status_code == 201, f"sponsor: {sponsor.text}"
    spn = sponsor.json()["wallet_id"]

    agent = await req(
        "POST",
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": spn,
            "agent_id": ikey("bot"),
            "budget_credits": 1000,
        },
    )
    assert agent.status_code == 201, f"agent: {agent.text}"
    agt = agent.json()["wallet_id"]

    return spn, agt


async def test_budget_exhaustion(spn, agt):
    """Spend exact budget, then one more."""
    print("\n[STRESS] Budget exhaustion...")
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("budget")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": "4",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    assert permit.status_code == 201
    pid = permit.json()["permit_id"]

    # 4 credits / 2 per call = 2 calls exactly
    for i in range(2):
        r = await req(
            "POST",
            "/mcp/messages",
            headers={"Idempotency-Key": ikey(f"budget-invoke-{i}")},
            json={
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": f"call {i}"},
                    "mcpContext": {"wallet_id": agt, "permit_id": pid},
                },
            },
        )
        assert r.status_code == 200, f"call {i}: {r.text}"
        assert "error" not in r.json(), f"call {i} failed: {r.json()}"
        print(f"  call {i + 1}: ✅")

    # Third call should exceed budget
    r = await req(
        "POST",
        "/mcp/messages",
        headers={"Idempotency-Key": ikey("budget-invoke-2")},
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "partner.notes.write",
                "arguments": {"text": "over budget"},
                "mcpContext": {"wallet_id": agt, "permit_id": pid},
            },
        },
    )
    assert r.status_code == 200
    err = r.json().get("error", {})
    assert err.get("message") == "permit_budget_exceeded", f"unexpected: {err}"
    print("  over-budget call: ✅ denied")


async def test_expired_permit(spn, agt):
    """Permit that expired 1 second ago."""
    print("\n[STRESS] Expired permit...")
    one_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("expired")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": "10",
            "expires_at": one_sec_ago,
        },
    )
    # Should be rejected at creation
    assert permit.status_code == 400 or "permit_expired" in permit.text, (
        f"expected expired rejection, got: {permit.status_code} {permit.text}"
    )
    print("  expired-at-creation: ✅ rejected")


async def test_concurrent_permit_creation(spn, agt):
    """20 parallel permit creations with same idempotency key."""
    print("\n[STRESS] Concurrent permit creation (same idempotency key)...")

    async def create(i):
        return await req(
            "POST",
            "/v1/permits",
            headers={"Idempotency-Key": ikey("concurrent-permit")},
            json={
                "issuer_wallet_id": spn,
                "subject_wallet_id": agt,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": "10",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )

    results = await asyncio.gather(*[create(i) for i in range(20)])
    codes = [r.status_code for r in results]
    ok_count = codes.count(201)
    conflict_count = codes.count(409)
    # Race: first writer wins (201), rest get 409 conflict — correct behavior
    assert ok_count >= 1, f"expected at least one 201, got {codes}"
    pids = [r.json().get("permit_id") for r in results if r.status_code == 201]
    assert len(set(pids)) == 1, f"expected same permit_id, got {set(pids)}"
    print(
        f"  20 concurrent creates: ✅ {ok_count}x201, {conflict_count}x409, same permit_id"
    )


async def test_concurrent_governed_invokes(spn, agt):
    """20 parallel invokes with fresh idempotency keys."""
    print("\n[STRESS] Concurrent governed invokes...")
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("concurrent-invoke-permit")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": "100",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    pid = permit.json()["permit_id"]

    async def invoke(i):
        return await req(
            "POST",
            "/mcp/messages",
            headers={"Idempotency-Key": ikey(f"invoke-{i}")},
            json={
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": f"concurrent {i}"},
                    "mcpContext": {"wallet_id": agt, "permit_id": pid},
                },
            },
        )

    start = time.monotonic()
    results = await asyncio.gather(*[invoke(i) for i in range(20)])
    elapsed = time.monotonic() - start
    codes = [r.status_code for r in results]
    success = len([successful_receipt_id(r) for r in results])
    print(
        f"  20 parallel invokes in {elapsed:.2f}s: {success}/20 success, codes={set(codes)}"
    )
    require(success == 20, "expected all success, got failures")


async def test_unicode_payload(spn, agt):
    """Unicode, emoji, and special chars in note text."""
    print("\n[STRESS] Unicode/emoji payload...")
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("unicode-permit")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            # 7 payloads x 2 credits: the cap must cover every payload, or a
            # budget denial would mask the result for the last ones.
            "max_credits": "14",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    pid = permit.json()["permit_id"]

    oversized = "A" * 10000  # beyond the tool's note length limit
    texts = [
        "Hello 世界 🌍",
        "<script>alert('xss')</script>",
        "' OR 1=1 --",
        "\x00\x01\x02",
        oversized,
        "日本語テスト",
        "🔥💀🎉💰",
    ]
    failed = []
    for i, text in enumerate(texts):
        r = await req(
            "POST",
            "/mcp/messages",
            headers={"Idempotency-Key": ikey(f"unicode-{i}")},
            json={
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": text},
                    "mcpContext": {"wallet_id": agt, "permit_id": pid},
                },
            },
        )
        body = r.json()
        error = body.get("error")
        if error is None:
            print(f"  text-{i} ({text[:30]}...): ✅")
            continue
        # Only the oversized note may be refused, and the refusal must not
        # charge: a refused call that moves money is a failure, not a rejection.
        receipt = (error.get("data") or {}).get("receipt") or {}
        if text is oversized and is_zero_credits(receipt.get("credits_charged")):
            print(f"  text-{i} ({text[:30]}...): ✅ refused, uncharged")
        else:
            print(f"  text-{i} ({text[:30]}...): ❌ {error}")
            failed.append(f"text-{i}")
    require(not failed, f"unicode payloads failed: {failed}")


async def test_tampered_permit(spn, agt):
    """Try to use a forged permit ID."""
    print("\n[STRESS] Tampered/forged permit...")
    r = await req(
        "POST",
        "/mcp/messages",
        headers={"Idempotency-Key": ikey("forged")},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "partner.notes.write",
                "arguments": {"text": "forged"},
                "mcpContext": {"wallet_id": agt, "permit_id": "permit-fake123456789"},
            },
        },
    )
    assert r.status_code == 200
    err = r.json().get("error", {})
    assert err.get("message") in ("permit_not_found", "permit_required"), (
        f"unexpected: {err}"
    )
    print(f"  forged permit: ✅ denied ({err.get('message')})")


async def test_cross_wallet_access(spn, agt):
    """Agent A tries to use Agent B's permit."""
    print("\n[STRESS] Cross-wallet access...")
    # Create a second agent
    agent2 = await req(
        "POST",
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": spn,
            "agent_id": ikey("bot-2"),
            "budget_credits": 100,
        },
    )
    agt2 = agent2.json()["wallet_id"]

    # Create permit for agent 1
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("cross-permit")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": "10",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    pid = permit.json()["permit_id"]

    # Agent 2 tries to use it
    r = await req(
        "POST",
        "/mcp/messages",
        headers={"Idempotency-Key": ikey("cross-invoke")},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "partner.notes.write",
                "arguments": {"text": "cross"},
                "mcpContext": {"wallet_id": agt2, "permit_id": pid},
            },
        },
    )
    assert r.status_code == 200
    err = r.json().get("error", {})
    assert err.get("message") == "permit_wallet_mismatch", f"unexpected: {err}"
    print(f"  cross-wallet invoke: ✅ denied ({err.get('message')})")


async def test_timezone_extremes(spn, agt):
    """Extreme timezone offsets."""
    print("\n[STRESS] Timezone extremes...")
    offsets = [
        "2099-01-01T00:00:00+14:00",  # max offset
        "2099-01-01T00:00:00-12:00",  # min offset
        "2099-01-01T00:00:00+00:30",  # half-hour offset
        "2099-01-01T00:00:00+05:30",  # India
        "2099-06-01T00:00:00+00:00",  # summer
    ]
    failed = []
    for i, offset in enumerate(offsets):
        r = await req(
            "POST",
            "/v1/permits",
            headers={"Idempotency-Key": ikey(f"tz-{i}")},
            json={
                "issuer_wallet_id": spn,
                "subject_wallet_id": agt,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": "10",
                "expires_at": offset,
            },
        )
        if r.status_code == 201:
            exp = r.json()["expires_at"]
            print(f"  {offset} -> {exp}: ✅")
        else:
            print(f"  {offset}: ❌ {r.status_code} {r.text[:100]}")
            failed.append(offset)
    require(not failed, f"permit expiries rejected: {failed}")


async def test_decimal_precision(spn, agt):
    """Edge-case credit amounts."""
    print("\n[STRESS] Decimal precision...")
    amounts = ["0.01", "0.001", "999999", "-1", "0", "abc", ""]
    # Only positive amounts the 1000-credit agent wallet covers may mint a
    # permit. Non-positive, non-numeric, and over-balance amounts must be
    # refused as a client error -- a 201 for any of them is a failure.
    creatable = {"0.01", "0.001"}
    failed = []
    for i, amt in enumerate(amounts):
        r = await req(
            "POST",
            "/v1/permits",
            headers={"Idempotency-Key": ikey(f"decimal-{i}")},
            json={
                "issuer_wallet_id": spn,
                "subject_wallet_id": agt,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": amt,
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        code = r.status_code
        body = r.json()
        ok = code == 201 if amt in creatable else code in (400, 422)
        mark = "✅" if ok else "❌"
        if code == 201:
            print(f"  max_credits={amt}: {mark} created")
        elif "max_credits_must_be_positive" in r.text:
            print(f"  max_credits={amt}: {mark} rejected (positive required)")
        else:
            print(
                f"  max_credits={amt}: {mark} {code} {body.get('detail', r.text[:80])}"
            )
        if not ok:
            failed.append(amt)
    require(not failed, f"max_credits edge cases misjudged: {failed}")


async def test_rapid_fire_idempotency(spn, agt):
    """Hammer same idempotency key 50 times in <1s."""
    print("\n[STRESS] Rapid-fire idempotency (50x same key)...")
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("rapid-permit")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": "100",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    pid = permit.json()["permit_id"]

    start = time.monotonic()
    tasks = []
    for _ in range(50):
        tasks.append(
            req(
                "POST",
                "/mcp/messages",
                headers={"Idempotency-Key": ikey("rapid-same-key")},
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "tools/call",
                    "params": {
                        "name": "partner.notes.write",
                        "arguments": {"text": "rapid replay"},
                        "mcpContext": {"wallet_id": agt, "permit_id": pid},
                    },
                },
            )
        )
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    # Every identical request must return a successful replay with a receipt.
    # An in-progress conflict is a failed check, not a successful replay.
    receipt_ids = [successful_receipt_id(r) for r in results]
    successes = len(receipt_ids)
    print(f"  50 calls in {elapsed:.2f}s: {successes}/50 success")
    if len(set(receipt_ids)) == 1 and receipt_ids:
        print(f"  All returned same receipt: ✅ {receipt_ids[0]}")
    else:
        print(f"  Receipt IDs varied: ❌ {set(receipt_ids)}")
    require(
        receipt_ids and len(set(receipt_ids)) == 1,
        f"50 same-key calls must share one receipt, got {set(receipt_ids)}",
    )


async def test_permit_reuse_after_replay(spn, agt):
    """Use same permit after many replays."""
    print("\n[STRESS] Permit reuse after heavy load...")
    permit = await req(
        "POST",
        "/v1/permits",
        headers={"Idempotency-Key": ikey("reuse-permit")},
        json={
            "issuer_wallet_id": spn,
            "subject_wallet_id": agt,
            "allowed_tools": ["partner.notes.write"],
            "max_credits": "100",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )
    pid = permit.json()["permit_id"]

    # Burn 48 credits (24 calls * 2)
    for i in range(24):
        r = await req(
            "POST",
            "/mcp/messages",
            headers={"Idempotency-Key": ikey(f"reuse-{i}")},
            json={
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": f"burn {i}"},
                    "mcpContext": {"wallet_id": agt, "permit_id": pid},
                },
            },
        )
        assert "error" not in r.json(), f"burn {i} failed: {r.json()}"

    # Check spent
    p = await req("GET", f"/v1/permits/{pid}")
    spent = p.json()["spent_credits"]
    # Compare as a number: the API serializes Decimals with fixed precision.
    spent_ok = Decimal(str(spent)) == Decimal("48")
    print(f"  24 calls, spent_credits={spent}: {'✅' if spent_ok else '❌'}")
    require(spent_ok, f"24 calls at 2 credits must spend 48, got {spent}")

    # One more should work
    r = await req(
        "POST",
        "/mcp/messages",
        headers={"Idempotency-Key": ikey("reuse-final")},
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "partner.notes.write",
                "arguments": {"text": "final"},
                "mcpContext": {"wallet_id": agt, "permit_id": pid},
            },
        },
    )
    assert "error" not in r.json()
    print("  25th call (spent=50/100): ✅")


async def test_health_under_load():
    """Hit health endpoint during stress."""
    print("\n[STRESS] Health under load...")
    # Start some background load
    bg_tasks = [req("GET", "/v1/discover") for _ in range(20)]
    # Health check in parallel
    health = await req("GET", "/health")
    await asyncio.gather(*bg_tasks)
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    print("  Health during 20 parallel discoveries: ✅")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run live trust-plane stress checks against an explicit target."
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

    print("=" * 60)
    print("HYPER EDGE-CASE STRESS TEST")
    print(f"Target: {API_URL}")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)

    try:
        # Setup
        print("\n[SETUP] Creating wallets...")
        spn, agt = await setup_wallets()
        print(f"  Sponsor: {spn}")
        print(f"  Agent: {agt}")

        # Run all stress tests
        await test_budget_exhaustion(spn, agt)
        await test_expired_permit(spn, agt)
        await test_concurrent_permit_creation(spn, agt)
        await test_concurrent_governed_invokes(spn, agt)
        await test_unicode_payload(spn, agt)
        await test_tampered_permit(spn, agt)
        await test_cross_wallet_access(spn, agt)
        await test_timezone_extremes(spn, agt)
        await test_decimal_precision(spn, agt)
        await test_rapid_fire_idempotency(spn, agt)
        await test_permit_reuse_after_replay(spn, agt)
        await test_health_under_load()
    except AssertionError as exc:
        print("\n" + "=" * 60)
        print(f"STRESS TEST FAILED ❌: {exc}")
        print("=" * 60)
        return 1

    print("\n" + "=" * 60)
    print("ALL STRESS TESTS PASSED ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
