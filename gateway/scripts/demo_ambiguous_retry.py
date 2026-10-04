#!/usr/bin/env python3
"""The ambiguous retry: an agent pays an invoice it never saw succeed.

One consequential action, one lost response, one retry. Without a transaction
boundary the vendor is paid twice. With one, the second attempt moves no money,
debits nothing, and hands the agent back the confirmation it lost.

Everything here runs the real FastAPI routers against a throwaway SQLite
database. The "bank" is an in-process ledger the governed tool writes to, so the
payout count printed below is the actual number of times the tool body ran --
not a claim about it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEMO_DB = ROOT / "data" / "demo_ambiguous_retry.db"
ADMIN_KEY = "ambiguous-retry-admin-key"
DEMO_PRIVATE_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

PAYOUT_TOOL = "vendor.payout.send"
VENDOR = "Northwind Supply"
INVOICE = "INV-4417"
AMOUNT_USD = Decimal("250.00")
CREDITS_PER_CALL = 5.0

PRINT_STEPS = True


def configure_environment() -> None:
    """Set demo-safe defaults before importing the app."""
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DEMO_DB}"
    os.environ["VALID_API_KEYS"] = ADMIN_KEY
    os.environ["TRUST_MODE_ENABLED"] = "true"
    os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
    os.environ["ENABLE_PROOF_SURFACES"] = "false"
    os.environ["TRUST_SIGNING_KEY_ID"] = "demo-ed25519"
    os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = DEMO_PRIVATE_KEY_B64


configure_environment()
sys.path.insert(0, str(ROOT))
# The offline verifier ships in the SDK and must not import the app.
sys.path.insert(0, str(ROOT.parent / "sdk" / "python" / "src"))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.database import close_db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.billing import ServiceCategory  # noqa: E402
from app.services.service_registry import get_service_registry  # noqa: E402
from b2a_sdk.receipt_verifier import (  # noqa: E402
    key_set_from_document,
    verify_bundle,
)


# --------------------------------------------------------------------------- #
# The downstream effect: a bank that records every payout it actually executes  #
# --------------------------------------------------------------------------- #


@dataclass
class Payout:
    confirmation: str
    vendor: str
    invoice: str
    amount_usd: Decimal


@dataclass
class PayoutBook:
    """Stands in for the system of record the consequential tool writes to.

    Nothing in the gateway can reach into this. It counts executions of the
    tool body, which is what makes the "paid once" line below evidence rather
    than an assertion.
    """

    payouts: list[Payout] = field(default_factory=list)

    def execute(self, vendor: str, invoice: str, amount_usd: Decimal) -> Payout:
        payout = Payout(
            confirmation=f"PAY-{len(self.payouts) + 1:04d}",
            vendor=vendor,
            invoice=invoice,
            amount_usd=amount_usd,
        )
        self.payouts.append(payout)
        return payout

    def reset(self) -> None:
        self.payouts.clear()

    @property
    def count(self) -> int:
        return len(self.payouts)

    @property
    def total_usd(self) -> Decimal:
        return sum((p.amount_usd for p in self.payouts), Decimal("0.00"))


BANK = PayoutBook()


def send_payout(
    vendor: str = "",
    invoice: str = "",
    amount_usd: str = "0.00",
) -> dict[str, str]:
    """The consequential tool. Every call moves money."""
    payout = BANK.execute(vendor, invoice, Decimal(amount_usd))
    return {
        "confirmation": payout.confirmation,
        "vendor": payout.vendor,
        "invoice": payout.invoice,
        "amount_usd": str(payout.amount_usd),
        "status": "settled",
    }


def register_demo_tool() -> None:
    get_service_registry().register_local(
        service_id=PAYOUT_TOOL,
        name="Vendor Payout",
        description="Send a vendor payout. Executing it twice pays twice.",
        category=ServiceCategory.AGENT_COMMS,
        func=send_payout,
        credits_per_unit=CREDITS_PER_CALL,
        unit_name="call",
        require_permit=True,
    )


def unregister_demo_tool() -> None:
    get_service_registry().unregister_local(PAYOUT_TOOL)


# --------------------------------------------------------------------------- #
# Presentation                                                                  #
# --------------------------------------------------------------------------- #

WIDTH = 66


def banner(title: str) -> None:
    if not PRINT_STEPS:
        return
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def section(title: str) -> None:
    if not PRINT_STEPS:
        return
    print()
    print(f"-- {title} ".ljust(WIDTH, "-"))


def line(text: str = "") -> None:
    if PRINT_STEPS:
        print(text)


def bank_state(label: str) -> None:
    line()
    line(f"  {label}")
    for payout in BANK.payouts:
        line(
            f"    {payout.confirmation}  {payout.vendor}  "
            f"{payout.invoice}  ${payout.amount_usd}"
        )
    if not BANK.payouts:
        line("    (no payouts)")
    line(f"    TOTAL: {BANK.count} payout(s), ${BANK.total_usd}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def remove_demo_db() -> None:
    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{DEMO_DB}{suffix}")
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                  #
# --------------------------------------------------------------------------- #


async def post_json(
    client: AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    expected_status: int,
) -> dict[str, Any]:
    response = await client.post(url, headers=headers, json=json_body)
    require(
        response.status_code == expected_status,
        f"POST {url} -> {response.status_code}: {response.text}",
    )
    return response.json()


async def get_json(
    client: AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    expected_status: int,
) -> dict[str, Any]:
    response = await client.get(url, headers=headers)
    require(
        response.status_code == expected_status,
        f"GET {url} -> {response.status_code}: {response.text}",
    )
    return response.json()


def payout_call(
    *,
    request_id: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    amount_usd: Decimal = AMOUNT_USD,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": PAYOUT_TOOL,
            "arguments": {
                "vendor": VENDOR,
                "invoice": INVOICE,
                "amount_usd": str(amount_usd),
            },
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


async def payout_debits(
    client: AsyncClient, headers: dict[str, str], wallet_id: str
) -> list[dict[str, Any]]:
    ledger = await get_json(
        client,
        f"/v1/billing/ledger/{wallet_id}",
        headers=headers,
        expected_status=200,
    )
    return [
        entry
        for entry in ledger["entries"]
        if PAYOUT_TOOL in entry.get("description", "")
    ]


# --------------------------------------------------------------------------- #
# The demo                                                                      #
# --------------------------------------------------------------------------- #


async def run_demo(json_output: bool = False) -> dict[str, Any]:
    global PRINT_STEPS
    PRINT_STEPS = not json_output

    await close_db()
    remove_demo_db()
    await init_db()
    register_demo_tool()
    BANK.reset()

    admin_headers = {"X-API-Key": ADMIN_KEY}
    summary: dict[str, Any] = {}

    try:
        banner("An agent retries a payout it never saw succeed")
        line()
        line(f"  An accounts-payable agent pays invoice {INVOICE}:")
        line(f"  ${AMOUNT_USD} to {VENDOR}.")
        line()
        line("  The payout succeeds. The response never reaches the agent.")
        line("  The agent, correctly, retries.")

        # ------------------------------------------------------------------ #
        # Act 1: no boundary                                                   #
        # ------------------------------------------------------------------ #
        section("WITHOUT a transaction boundary")
        line()
        line("  The agent calls the tool directly. Nothing sits in between.")
        line()
        first = send_payout(VENDOR, INVOICE, str(AMOUNT_USD))
        line(f"    -> send_payout({INVOICE}, ${AMOUNT_USD})")
        line(f"       executed: {first['confirmation']}")
        line("    !! response lost in transit -- the agent sees a timeout")
        retry = send_payout(VENDOR, INVOICE, str(AMOUNT_USD))
        line(f"    -> send_payout({INVOICE}, ${AMOUNT_USD})   [retry]")
        line(f"       executed AGAIN: {retry['confirmation']}")
        bank_state(f"{VENDOR} received:")
        line()
        line(f"  {INVOICE} is paid twice. The agent did nothing wrong.")

        ungoverned_payouts = BANK.count
        ungoverned_total = BANK.total_usd
        require(ungoverned_payouts == 2, "expected the naive retry to pay twice")

        BANK.reset()

        # ------------------------------------------------------------------ #
        # Act 2: the boundary                                                  #
        # ------------------------------------------------------------------ #
        section("WITH Agent Middleware in front of the same tool")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://demo") as client:
            sponsor = await post_json(
                client,
                "/v1/billing/wallets/sponsor",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_name": "Accounts Payable",
                    "email": "ap@example.com",
                    "initial_credits": 10000,
                    "require_kyc": False,
                },
            )
            agent = await post_json(
                client,
                "/v1/billing/wallets/agent",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_wallet_id": sponsor["wallet_id"],
                    "agent_id": "ap-agent",
                    "budget_credits": 1000,
                    "daily_limit": 500,
                },
            )
            agent_wallet_id = agent["wallet_id"]
            key = await post_json(
                client,
                "/v1/api-keys",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "wallet_id": agent_wallet_id,
                    "key_name": "ap-agent-runtime",
                    "expires_in_days": 30,
                },
            )
            agent_headers = {"X-API-Key": key["api_key"]}

            permit = await post_json(
                client,
                "/v1/permits",
                headers={**admin_headers, "Idempotency-Key": "ambiguous-retry-permit"},
                expected_status=201,
                json_body={
                    "issuer_wallet_id": agent_wallet_id,
                    "subject_wallet_id": agent_wallet_id,
                    "subject_key_id": key["key_id"],
                    "allowed_tools": [PAYOUT_TOOL],
                    "scopes": [f"tool:{PAYOUT_TOOL}:invoke", "billing:charge"],
                    "max_credits": 50,
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=30)
                    ).isoformat(),
                },
            )
            permit_id = permit["permit_id"]

            line()
            line("  The agent holds a signed permit scoped to this one tool,")
            line("  and sends the payout under idempotency key 'pay-INV-4417'.")
            line()

            body = payout_call(
                request_id="payout-1",
                wallet_id=agent_wallet_id,
                permit_id=permit_id,
                idempotency_key="pay-INV-4417",
            )

            # The gateway completes and commits. The agent's connection dies
            # before the body reaches it -- so the agent holds nothing at all.
            committed = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=body,
            )
            committed_result = committed["result"]
            committed_receipt = committed_result["receipt"]
            committed_confirmation = json.loads(committed_result["content"][0]["text"])[
                "confirmation"
            ]

            line(f"    -> tools/call {PAYOUT_TOOL}   [key: pay-INV-4417]")
            line("       gateway: dispatched, charged, receipt written")
            line("    !! the agent's connection drops before the response lands")
            line("       the agent knows nothing: paid? not paid? charged?")

            after_first = await payout_debits(client, agent_headers, agent_wallet_id)
            require(BANK.count == 1, "first governed call did not execute once")
            require(len(after_first) == 1, "first governed call did not debit once")

            line()
            line("    -> tools/call ... same key, byte-identical   [retry]")

            replayed = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=body,
            )
            replayed_result = replayed["result"]
            replayed_receipt = replayed_result["receipt"]
            replayed_confirmation = json.loads(replayed_result["content"][0]["text"])[
                "confirmation"
            ]

            line("       gateway: key already terminal -- no dispatch, no debit")
            line(f"       returned the stored outcome: {replayed_confirmation}")

            after_replay = await payout_debits(client, agent_headers, agent_wallet_id)

            require(
                BANK.count == 1,
                f"retry executed the tool again: {BANK.count} payouts",
            )
            require(
                len(after_replay) == 1,
                f"retry created a second debit: {after_replay}",
            )
            require(
                replayed_receipt["receipt_id"] == committed_receipt["receipt_id"],
                "retry did not return the original receipt",
            )
            require(
                replayed_confirmation == committed_confirmation,
                "retry did not return the original payout confirmation",
            )

            bank_state(f"{VENDOR} received:")
            line()
            line(
                f"    Wallet debited:  {len(after_replay)} x {CREDITS_PER_CALL} credits"
            )
            line(f"    Receipt id:      {committed_receipt['receipt_id']}")
            line("    Same receipt on both attempts: yes")
            line()
            line(f"  {INVOICE} is paid once. The agent recovered the")
            line(f"  confirmation it lost ({replayed_confirmation}) without")
            line("  moving another dollar.")

            # -------------------------------------------------------------- #
            # Act 3: the key cannot be reused for a different payout           #
            # -------------------------------------------------------------- #
            section("A spent key cannot smuggle a different payout")
            line()
            line("  Same key, different amount -- the classic attack.")
            line()

            tampered = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=payout_call(
                    request_id="payout-tampered",
                    wallet_id=agent_wallet_id,
                    permit_id=permit_id,
                    idempotency_key="pay-INV-4417",
                    amount_usd=Decimal("9500.00"),
                ),
            )
            conflict = tampered["error"]
            line("    -> tools/call ... $9500.00 under key 'pay-INV-4417'")
            line(f"       refused: {conflict['message']}")

            after_conflict = await payout_debits(client, agent_headers, agent_wallet_id)
            require(
                conflict["message"] == "idempotency_key_reused",
                f"expected idempotency_key_reused, got {conflict}",
            )
            require(BANK.count == 1, "conflicting payload executed the tool")
            require(len(after_conflict) == 1, "conflicting payload created a debit")

            bank_state(f"{VENDOR} received:")

            # -------------------------------------------------------------- #
            # Act 4: the evidence outlives the gateway                         #
            # -------------------------------------------------------------- #
            section("The receipt proves it, with no access to this server")

            portable = await get_json(
                client,
                f"/v1/receipts/{committed_receipt['receipt_id']}/portable",
                headers=agent_headers,
                expected_status=200,
            )
            key_document = await get_json(
                client,
                "/.well-known/trust-keys.json",
                headers={},
                expected_status=200,
            )
            offline = verify_bundle(portable, key_set_from_document(key_document))
            require(offline.ok, f"receipt failed offline verify: {offline.reason}")

            forged = dict(portable)
            forged["signing_input"] = portable["signing_input"].replace("a", "b", 1)
            forged_result = verify_bundle(forged, key_set_from_document(key_document))
            require(forged_result.is_tampered, "forged receipt verified")

            line()
            line("    Ed25519 signature checks against the published key set,")
            line("    with no credentials and no call back to this gateway.")
            line(f"      verified:          {offline.ok}")
            line(f"      signing key:       {offline.key_id}")
            line(f"      edited copy:       rejected ({forged_result.status.value})")

            # -------------------------------------------------------------- #
            banner("What this demo does and does not show")
            line()
            line("  Shown: one accepted idempotency key permits at most one")
            line("  dispatch and at most one debit; the retry returns the")
            line("  stored outcome; a changed payload under that key fails")
            line("  closed; the receipt verifies offline.")
            line()
            line("  Not shown: a crash. The order is charge, then tool")
            line("  body, then receipt -- the wallet is already debited")
            line("  when the body runs. This demo cannot exhibit that")
            line("  window -- its bank is an in-memory list that dies")
            line("  with the process -- but a tool whose effect is")
            line("  durable can: the effect lands, the debit stands, no")
            line("  receipt is written, and the local path has no")
            line("  dispatch state machine, so it goes to manual review.")
            line()
            line("  A remote tool is no better off from the gateway's side:")
            line("  a send claimed but unconfirmed is receipted")
            line("  delivery_uncertain and never silently redispatched, and")
            line("  that record still is not proof the effect happened.")
            line("  Both cases: docs/failure-semantics.md.")
            line()

            summary = {
                "ungoverned_payouts": ungoverned_payouts,
                "ungoverned_total_usd": str(ungoverned_total),
                "governed_payouts": BANK.count,
                "governed_total_usd": str(BANK.total_usd),
                "governed_debits": len(after_conflict),
                "receipt_id": committed_receipt["receipt_id"],
                "replay_receipt_id": replayed_receipt["receipt_id"],
                "payout_confirmation": committed_confirmation,
                "replay_confirmation": replayed_confirmation,
                "conflict_reason": conflict["message"],
                "offline_verified": offline.ok,
                "offline_signing_key_id": offline.key_id,
                "offline_forgery_detected": forged_result.is_tampered,
            }

        if json_output:
            print(json.dumps(summary, indent=2, sort_keys=True))
        return summary
    finally:
        unregister_demo_tool()
        await close_db()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the final artifact as JSON.",
    )
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        action="store_true",
        help="Compatibility flag: the script always asserts its invariants.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_demo(json_output=args.json))


if __name__ == "__main__":
    main()
