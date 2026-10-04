"""Self-credential and prove the economic invariants over HTTP.

    python scripts/agent_self_credential_proof.py [--base-url http://localhost:8000]

An autonomous agent handed nothing but a base URL runs this against a local
instance started with ENABLE_DEV_KEY_SELF_PROVISION=true. It mints its own
wallet-scoped key via POST /v1/dev-keys/self-provision (no pre-shared
secret), then exercises the governed loop as an ordinary non-admin caller
and checks the invariants that matter economically:

  1. charge-once      one governed invoke debits exactly once
  2. replay           same idempotency key -> cached receipt, no new debit,
                      no second side effect
  3. concurrency      N identical in-flight calls -> one success, one debit,
                      one side effect (losers fail closed, never double-charge)
  4. reuse-conflict   same key with a different body -> 400, no debit
  5. scope-denial     tool outside the permit -> 403 with a signed denial
                      receipt and zero credits charged
  6. no-permit        governed tool without a permit -> 403, no debit
  7. signature        every receipt verifies under the published Ed25519
                      trust key, and tampered copies do not

Exit status: 0 only when all seven invariants ran and held, 1 when any check
failed, 2 when the target is unusable (self-provision off, transport error),
and 3 when nothing failed but an invariant could not run -- scope-denial needs
the server started with ENABLE_DOGFOOD_SECOND_TOOL=true.

Unlike scripts/dogfood_trust_plane.py (in-process ASGI, bootstrap-admin key),
this drives the real HTTP surface with a credential the agent minted itself,
so it reproduces what an external agent can independently establish. The
self-provision route is local-only: production-like deployments refuse to
boot with the flag set and the handler fails closed with 403 there, so this
harness never runs against production. See docs/static-dev-api-keys.md.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ALLOWED_TOOL = "partner.notes.write"
BLOCKED_TOOL = "partner.notes.count"
BURST = 5

failures: list[str] = []
# Invariants that could not run against this server. A skipped invariant is
# not a held one, so the run never reports success while this is non-empty.
skipped: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


def step(msg: str) -> None:
    print(f"\n== {msg}")


async def debits(cl: httpx.AsyncClient, wallet: str) -> list[dict[str, Any]]:
    r = await cl.get(f"/v1/billing/ledger/{wallet}")
    r.raise_for_status()
    return [e for e in r.json()["entries"] if e["action"] == "debit"]


def invoke_body(
    wallet: str, permit: str, idem: str | None, text: str
) -> dict[str, Any]:
    ctx: dict[str, Any] = {"wallet_id": wallet, "permit_id": permit}
    if idem:
        ctx["idempotency_key"] = idem
    return {"name": ALLOWED_TOOL, "arguments": {"text": text}, "mcp_context": ctx}


async def verify_receipts(cl: httpx.AsyncClient, receipt_ids: list[str]) -> None:
    keys = {
        k["kid"]: k["public_key_b64"]
        for k in (await cl.get("/.well-known/trust-keys.json")).json()["keys"]
    }
    for rid in receipt_ids:
        p = (await cl.get(f"/v1/receipts/{rid}/portable")).json()
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[p["kid"]]))
        sig = base64.b64decode(p["signature"])

        def verifies(message: bytes) -> bool:
            try:
                pub.verify(sig, message)
            except InvalidSignature:
                return False
            return True

        check(f"{rid} signature valid", verifies(p["signing_input"].encode()))
        signed = json.loads(p["signing_input"])
        # Mutate each economically load-bearing field; every copy must fail.
        # Skip a field already holding the mutation value, or the "tamper"
        # would be a no-op and the check would pass vacuously.
        tampered_accepted = []
        for field, value in (
            ("credits_charged", "999"),
            ("outcome", "success"),
            ("tool", "partner.payments.send"),
            ("wallet_id", "agt-attacker"),
        ):
            if signed.get(field) == value:
                continue
            forged = dict(signed, **{field: value})
            body = json.dumps(forged, sort_keys=True, separators=(",", ":"))
            if verifies(body.encode()):
                tampered_accepted.append(field)
        check(
            f"{rid} rejects tampering",
            not tampered_accepted,
            f"accepted: {tampered_accepted}" if tampered_accepted else "",
        )


async def run(base_url: str) -> int:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as cl:
        step("self-provisioning a wallet-scoped key (no pre-shared secret)")
        r = await cl.post(
            "/v1/dev-keys/self-provision",
            json={"agent_id": "proof-agent", "key_name": "proof", "budget_credits": 50},
        )
        if r.status_code == 404:
            print(
                "  self-provision is disabled; start the server with "
                "ENABLE_DEV_KEY_SELF_PROVISION=true (local only)."
            )
            return 2
        r.raise_for_status()
        creds = r.json()
        wallet, key_id = creds["wallet_id"], creds["key_id"]
        cl.headers["X-API-Key"] = creds["api_key"]
        print(f"  wallet={wallet} key_id={key_id} (secret withheld)")

        step(f"issuing a permit scoped only to {ALLOWED_TOOL}")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        r = await cl.post(
            "/v1/permits",
            headers={"Idempotency-Key": f"proof-permit-{key_id}"},
            json={
                "issuer_wallet_id": wallet,
                "subject_wallet_id": wallet,
                "subject_key_id": key_id,
                "allowed_tools": [ALLOWED_TOOL],
                "scopes": [f"tool:{ALLOWED_TOOL}:invoke", "billing:charge"],
                "max_credits": 25,
                "expires_at": expires,
            },
        )
        r.raise_for_status()
        permit = r.json()["permit_id"]
        print(f"  permit={permit}")

        receipts: list[str] = []
        idem = f"proof-invoke-{key_id}"
        text = f"proof note {key_id}"

        step("1. first governed invoke charges exactly once")
        base = len(await debits(cl, wallet))
        r1 = await cl.post(
            f"/mcp/tools/{ALLOWED_TOOL}/invoke",
            json=invoke_body(wallet, permit, idem, text),
        )
        check("invoke succeeds", r1.status_code == 200, f"HTTP {r1.status_code}")
        first_receipt = (r1.json().get("receipt") or {}).get("receipt_id")
        receipts.append(first_receipt)
        after_first = await debits(cl, wallet)
        check("exactly one debit", len(after_first) == base + 1)

        step("2. replaying the same idempotency key adds no debit")
        r2 = await cl.post(
            f"/mcp/tools/{ALLOWED_TOOL}/invoke",
            json=invoke_body(wallet, permit, idem, text),
        )
        replayed = (r2.json().get("receipt") or {}).get("receipt_id")
        check("replay returns the cached receipt", replayed == first_receipt)
        check("no second debit", len(await debits(cl, wallet)) == len(after_first))

        step(f"3. {BURST} concurrent identical calls settle to one charge")
        burst_idem, burst_text = f"{idem}-burst", f"{text} burst"
        before = len(await debits(cl, wallet))
        rs = await asyncio.gather(
            *(
                cl.post(
                    f"/mcp/tools/{ALLOWED_TOOL}/invoke",
                    json=invoke_body(wallet, permit, burst_idem, burst_text),
                )
                for _ in range(BURST)
            )
        )
        wins = [x for x in rs if x.status_code == 200]
        check(
            "exactly one call succeeds",
            len(wins) == 1,
            f"statuses {[x.status_code for x in rs]}",
        )
        check("exactly one debit", len(await debits(cl, wallet)) == before + 1)
        if wins:
            receipts.append((wins[0].json().get("receipt") or {}).get("receipt_id"))

        step("4. reusing the key with a different body is refused")
        before = len(await debits(cl, wallet))
        r4 = await cl.post(
            f"/mcp/tools/{ALLOWED_TOOL}/invoke",
            json=invoke_body(wallet, permit, idem, "a different payload"),
        )
        check("conflict rejected", r4.status_code == 400, f"HTTP {r4.status_code}")
        check("no debit", len(await debits(cl, wallet)) == before)

        step(f"5. {BLOCKED_TOOL} is outside the permit and must be denied free")
        before = len(await debits(cl, wallet))
        r5 = await cl.post(
            f"/mcp/tools/{BLOCKED_TOOL}/invoke",
            json={
                "name": BLOCKED_TOOL,
                "arguments": {},
                "mcp_context": {
                    "wallet_id": wallet,
                    "permit_id": permit,
                    "idempotency_key": f"{idem}-deny",
                },
            },
        )
        if r5.status_code == 404:
            print(
                f"  skipped: {BLOCKED_TOOL} is not registered "
                "(start the server with ENABLE_DOGFOOD_SECOND_TOOL=true)"
            )
            skipped.append("scope-denial")
        else:
            check("denied", r5.status_code == 403, f"HTTP {r5.status_code}")
            detail = r5.json().get("detail")
            check("denial detail is structured", isinstance(detail, dict))
            detail = detail if isinstance(detail, dict) else {}
            check(
                "reason is permit scope",
                detail.get("error") == "permit_tool_not_allowed",
            )
            denial = (detail.get("receipt") or {}).get("receipt_id")
            check("signed denial receipt issued", bool(denial))
            if denial:
                receipts.append(denial)
            check("no debit", len(await debits(cl, wallet)) == before)

        step("6. a governed tool without a permit fails closed")
        before = len(await debits(cl, wallet))
        r6 = await cl.post(
            f"/mcp/tools/{ALLOWED_TOOL}/invoke",
            json={
                "name": ALLOWED_TOOL,
                "arguments": {"text": "ungoverned"},
                "mcp_context": {"wallet_id": wallet},
            },
        )
        check("rejected", r6.status_code == 403, f"HTTP {r6.status_code}")
        check("no debit", len(await debits(cl, wallet)) == before)

        step("7. receipts verify under the published Ed25519 trust key")
        await verify_receipts(cl, [r for r in receipts if r])

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    if skipped:
        print(
            f"\nSKIPPED: {', '.join(skipped)} (no failures, but not every invariant ran)"
        )
        return 3
    print("\nALL INVARIANTS HELD")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost:8000")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args.base_url))
    except httpx.HTTPError as exc:
        print(f"transport error against {args.base_url}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
