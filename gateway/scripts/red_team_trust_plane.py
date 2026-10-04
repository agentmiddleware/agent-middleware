#!/usr/bin/env python3
"""Adversarial proof for the MCP governance trust plane.

Where ``demo_trust_plane.py`` proves the happy path plus a couple of denials,
this script is the red-team battery: it drives one valid permit and then
attacks it ten different ways, asserting that each attack is denied with a
concrete, specific reason code and that *none* of them produces a ledger
debit. A single positive control at the end proves the harness can still tell
an allowed call from a denied one, and that exactly one charge landed.

It runs against a throwaway local SQLite database and the real FastAPI
routers, with the trust plane in fail-closed mode
(``ALLOW_LEGACY_UNPERMITTED_MCP=false``).

Attacks covered (reason code asserted for each):

    no permit on a governed call ............ permit_required
    unknown permit id ....................... permit_not_found
    tool outside the permit ................. permit_tool_not_allowed
    permit missing the invoke scope ......... permit_scope_missing
    budget too small for the call ........... permit_budget_exceeded
    stolen permit used by another wallet .... permit_wallet_mismatch
    permit used with the wrong key .......... permit_key_mismatch
    expired permit .......................... permit_expired
    revoked permit .......................... permit_revoked
    tampered (re-signed) permit ............. permit_signature_invalid
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEMO_DB = ROOT / "data" / "red_team_trust_plane.db"
ADMIN_KEY = "demo-admin-key"
DEMO_PRIVATE_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
ALLOWED_TOOL = "trust-plane-echo"
BLOCKED_TOOL = "trust-plane-admin-ledger"
TOOL_COST = 2.0
PRINT_STEPS = True


def configure_environment() -> None:
    """Set demo-safe defaults before importing the app."""
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DEMO_DB}"
    os.environ["VALID_API_KEYS"] = ADMIN_KEY
    os.environ["TRUST_MODE_ENABLED"] = "true"
    os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
    os.environ["TRUST_SIGNING_KEY_ID"] = "demo-ed25519"
    os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = DEMO_PRIVATE_KEY_B64


configure_environment()
sys.path.insert(0, str(ROOT))

from decimal import Decimal  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.database import close_db, get_session_factory, init_db  # noqa: E402
from app.db.models import PermitModel  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.billing import ServiceCategory  # noqa: E402
from app.services.service_registry import get_service_registry  # noqa: E402


def remove_demo_db() -> None:
    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{DEMO_DB}{suffix}")
        if path.exists():
            path.unlink()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def step(message: str) -> None:
    if PRINT_STEPS:
        print(f"[red-team] {message}")


def register_demo_tools() -> None:
    registry = get_service_registry()

    def echo(message: str = "ok") -> dict[str, str]:
        return {"message": message, "governed": "true"}

    def admin_ledger_probe() -> dict[str, str]:
        return {"should_not_execute": "true"}

    registry.register_local(
        service_id=ALLOWED_TOOL,
        name="Trust Plane Echo",
        description="Demo tool allowed by the signed permit",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    registry.register_local(
        service_id=BLOCKED_TOOL,
        name="Trust Plane Blocked Admin Tool",
        description="Demo tool intentionally outside the signed permit",
        category=ServiceCategory.AGENT_COMMS,
        func=admin_ledger_probe,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )


def unregister_demo_tools() -> None:
    registry = get_service_registry()
    registry.unregister_local(ALLOWED_TOOL)
    registry.unregister_local(BLOCKED_TOOL)


async def post_json(
    client: AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    expected_status: int,
) -> dict[str, Any]:
    response = await client.post(path, headers=headers, json=json_body)
    require(
        response.status_code == expected_status,
        f"{path} returned {response.status_code}: {response.text}",
    )
    return response.json()


async def get_json(
    client: AsyncClient,
    path: str,
    *,
    headers: dict[str, str],
    expected_status: int,
) -> dict[str, Any]:
    response = await client.get(path, headers=headers)
    require(
        response.status_code == expected_status,
        f"{path} returned {response.status_code}: {response.text}",
    )
    return response.json()


def build_mcp_call(
    *,
    request_id: str,
    tool: str,
    wallet_id: str,
    idempotency_key: str,
    permit_id: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mcp_context: dict[str, Any] = {
        "wallet_id": wallet_id,
        "idempotency_key": idempotency_key,
    }
    if permit_id is not None:
        mcp_context["permit_id"] = permit_id
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments or {},
            "mcpContext": mcp_context,
        },
    }


async def issue_permit(
    client: AsyncClient,
    *,
    admin_headers: dict[str, str],
    wallet_id: str,
    key_id: str,
    allowed_tools: list[str],
    scopes: list[str],
    max_credits: int,
    idem_key: str,
) -> dict[str, Any]:
    return await post_json(
        client,
        "/v1/permits",
        headers={**admin_headers, "Idempotency-Key": idem_key},
        expected_status=201,
        json_body={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": allowed_tools,
            "scopes": scopes,
            "max_credits": max_credits,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
    )


async def _force_permit_expired(permit_id: str) -> None:
    """Push a previously-valid permit's expiry into the past."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PermitModel).where(PermitModel.permit_id == permit_id)
        )
        model = result.scalar_one()
        model.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.add(model)
        await session.commit()


async def _tamper_permit_budget(permit_id: str) -> None:
    """Raise a signed field (max_credits) so the signature no longer holds.

    The budget check passes (raising the cap can only make more room), so
    validation proceeds to the signature check and fails there — proving the
    signature, not just the stored fields, gates authorization.
    """
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PermitModel).where(PermitModel.permit_id == permit_id)
        )
        model = result.scalar_one()
        model.max_credits = model.max_credits + Decimal("2000")
        session.add(model)
        await session.commit()


async def _allowed_tool_debits(
    client: AsyncClient, wallet_id: str, headers: dict[str, str]
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
        if entry["service_category"] == "agent_comms"
        and ALLOWED_TOOL in entry.get("description", "")
    ]


async def _any_demo_tool_debits(
    client: AsyncClient, wallet_id: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    """Return every ledger entry referencing any demo tool on the given wallet.

    The narrow ALLOWED_TOOL+victim_wallet filter elsewhere is only sufficient
    for the positive control. The red-team battery also targets BLOCKED_TOOL
    and uses the attacker wallet for the stolen-permit case, so the no-debit
    invariant has to be checked across every wallet/tool combination the
    attacks actually touched.
    """
    ledger = await get_json(
        client,
        f"/v1/billing/ledger/{wallet_id}",
        headers=headers,
        expected_status=200,
    )
    return [
        entry
        for entry in ledger["entries"]
        if entry["service_category"] == "agent_comms"
        and (
            ALLOWED_TOOL in entry.get("description", "")
            or BLOCKED_TOOL in entry.get("description", "")
        )
    ]


async def run_red_team(json_output: bool = False) -> dict[str, Any]:
    global PRINT_STEPS

    PRINT_STEPS = not json_output
    await close_db()
    remove_demo_db()
    await init_db()
    register_demo_tools()

    admin_headers = {"X-API-Key": ADMIN_KEY}
    attacks: list[dict[str, Any]] = []

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://red-team"
        ) as client:
            step("provisioning sponsor, victim agent, and attacker agent")
            sponsor = await post_json(
                client,
                "/v1/billing/wallets/sponsor",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_name": "Red Team Sponsor",
                    "email": "red-team-sponsor@example.com",
                    "initial_credits": 10000,
                    "require_kyc": False,
                },
            )
            sponsor_wallet_id = sponsor["wallet_id"]

            victim = await post_json(
                client,
                "/v1/billing/wallets/agent",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_wallet_id": sponsor_wallet_id,
                    "agent_id": "victim-agent",
                    "budget_credits": 1000,
                    "daily_limit": 500,
                },
            )
            victim_wallet_id = victim["wallet_id"]

            attacker = await post_json(
                client,
                "/v1/billing/wallets/agent",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_wallet_id": sponsor_wallet_id,
                    "agent_id": "attacker-agent",
                    "budget_credits": 1000,
                    "daily_limit": 500,
                },
            )
            attacker_wallet_id = attacker["wallet_id"]

            # Two keys for the victim wallet: the permit binds to key #1, so a
            # call presenting key #2 must be rejected for key mismatch.
            victim_key1 = await post_json(
                client,
                "/v1/api-keys",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "wallet_id": victim_wallet_id,
                    "key_name": "victim-runtime-1",
                    "expires_in_days": 30,
                },
            )
            victim_key2 = await post_json(
                client,
                "/v1/api-keys",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "wallet_id": victim_wallet_id,
                    "key_name": "victim-runtime-2",
                    "expires_in_days": 30,
                },
            )
            attacker_key = await post_json(
                client,
                "/v1/api-keys",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "wallet_id": attacker_wallet_id,
                    "key_name": "attacker-runtime",
                    "expires_in_days": 30,
                },
            )

            victim_headers = {"X-API-Key": victim_key1["api_key"]}
            victim_headers_key2 = {"X-API-Key": victim_key2["api_key"]}
            attacker_headers = {"X-API-Key": attacker_key["api_key"]}

            step("issuing one valid permit bound to victim wallet + key #1")
            valid_scopes = [f"tool:{ALLOWED_TOOL}:invoke", "billing:charge"]
            valid_permit = await issue_permit(
                client,
                admin_headers=admin_headers,
                wallet_id=victim_wallet_id,
                key_id=victim_key1["key_id"],
                allowed_tools=[ALLOWED_TOOL],
                scopes=valid_scopes,
                max_credits=25,
                idem_key="rt-permit-valid",
            )
            valid_permit_id = valid_permit["permit_id"]

            async def attack(
                name: str,
                *,
                headers: dict[str, str],
                wallet_id: str,
                tool: str,
                expected_reason: str,
                permit_id: str | None,
                idem: str,
            ) -> None:
                response = await post_json(
                    client,
                    "/mcp/messages",
                    headers=headers,
                    expected_status=200,
                    json_body=build_mcp_call(
                        request_id=idem,
                        tool=tool,
                        wallet_id=wallet_id,
                        idempotency_key=idem,
                        permit_id=permit_id,
                    ),
                )
                require(
                    "error" in response,
                    f"{name}: expected a JSON-RPC denial, got {response}",
                )
                error = response["error"]
                reason = error["message"]
                require(
                    reason == expected_reason,
                    f"{name}: expected reason {expected_reason!r}, got {reason!r}",
                )
                # Denials that go through the permit-validation path produce a
                # signed denial receipt. When one is present, it must carry
                # outcome=denied and no ledger linkage — that is the per-call
                # guarantee that complements the cross-wallet ledger sweep
                # below.
                receipt = (error.get("data") or {}).get("receipt")
                receipt_id = None
                if receipt is not None:
                    require(
                        receipt.get("outcome") == "denied",
                        f"{name}: denial receipt outcome was {receipt.get('outcome')!r}",
                    )
                    require(
                        receipt.get("ledger_entry_id") is None,
                        f"{name}: denial receipt carried ledger_entry_id "
                        f"{receipt.get('ledger_entry_id')!r}",
                    )
                    receipt_id = receipt.get("receipt_id")
                step(f"  denied: {name} -> {reason}")
                attacks.append(
                    {
                        "attack": name,
                        "reason": reason,
                        "receipt_id": receipt_id,
                        "wallet_id": wallet_id,
                        "tool": tool,
                    }
                )

            step("running the adversarial battery")

            await attack(
                "no_permit",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_required",
                permit_id=None,
                idem="rt-no-permit",
            )

            await attack(
                "unknown_permit",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_not_found",
                permit_id="permit-does-not-exist",
                idem="rt-unknown-permit",
            )

            await attack(
                "tool_not_allowed",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=BLOCKED_TOOL,
                expected_reason="permit_tool_not_allowed",
                permit_id=valid_permit_id,
                idem="rt-tool-not-allowed",
            )

            # A permit that allows the tool but lacks the invoke scope.
            scope_permit = await issue_permit(
                client,
                admin_headers=admin_headers,
                wallet_id=victim_wallet_id,
                key_id=victim_key1["key_id"],
                allowed_tools=[ALLOWED_TOOL],
                scopes=["billing:charge"],
                max_credits=25,
                idem_key="rt-permit-scope",
            )
            await attack(
                "scope_missing",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_scope_missing",
                permit_id=scope_permit["permit_id"],
                idem="rt-scope-missing",
            )

            # A permit whose cap is below the tool's per-call cost.
            budget_permit = await issue_permit(
                client,
                admin_headers=admin_headers,
                wallet_id=victim_wallet_id,
                key_id=victim_key1["key_id"],
                allowed_tools=[ALLOWED_TOOL],
                scopes=valid_scopes,
                max_credits=1,
                idem_key="rt-permit-budget",
            )
            await attack(
                "budget_exceeded",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_budget_exceeded",
                permit_id=budget_permit["permit_id"],
                idem="rt-budget-exceeded",
            )

            # Stolen permit: the attacker authenticates as itself (own wallet +
            # key) but presents the victim's permit. Access control passes
            # because the attacker is touching its own wallet; the permit's
            # subject wallet does not match, so it is denied.
            await attack(
                "wallet_mismatch",
                headers=attacker_headers,
                wallet_id=attacker_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_wallet_mismatch",
                permit_id=valid_permit_id,
                idem="rt-wallet-mismatch",
            )

            # Right wallet, wrong key: the permit binds to key #1, the call
            # presents key #2 (both belong to the victim wallet).
            await attack(
                "key_mismatch",
                headers=victim_headers_key2,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_key_mismatch",
                permit_id=valid_permit_id,
                idem="rt-key-mismatch",
            )

            expired_permit = await issue_permit(
                client,
                admin_headers=admin_headers,
                wallet_id=victim_wallet_id,
                key_id=victim_key1["key_id"],
                allowed_tools=[ALLOWED_TOOL],
                scopes=valid_scopes,
                max_credits=25,
                idem_key="rt-permit-expired",
            )
            await _force_permit_expired(expired_permit["permit_id"])
            await attack(
                "expired",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_expired",
                permit_id=expired_permit["permit_id"],
                idem="rt-expired",
            )

            revoked_permit = await issue_permit(
                client,
                admin_headers=admin_headers,
                wallet_id=victim_wallet_id,
                key_id=victim_key1["key_id"],
                allowed_tools=[ALLOWED_TOOL],
                scopes=valid_scopes,
                max_credits=25,
                idem_key="rt-permit-revoked",
            )
            revoke = await client.post(
                f"/v1/permits/{revoked_permit['permit_id']}/revoke",
                headers=admin_headers,
            )
            require(
                revoke.status_code == 200,
                f"permit revoke failed: {revoke.status_code} {revoke.text}",
            )
            await attack(
                "revoked",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_revoked",
                permit_id=revoked_permit["permit_id"],
                idem="rt-revoked",
            )

            tampered_permit = await issue_permit(
                client,
                admin_headers=admin_headers,
                wallet_id=victim_wallet_id,
                key_id=victim_key1["key_id"],
                allowed_tools=[ALLOWED_TOOL],
                scopes=valid_scopes,
                max_credits=25,
                idem_key="rt-permit-tampered",
            )
            await _tamper_permit_budget(tampered_permit["permit_id"])
            await attack(
                "signature_invalid",
                headers=victim_headers,
                wallet_id=victim_wallet_id,
                tool=ALLOWED_TOOL,
                expected_reason="permit_signature_invalid",
                permit_id=tampered_permit["permit_id"],
                idem="rt-signature-invalid",
            )

            step("confirming no attack produced a ledger debit on any touched wallet")
            # Sweep every wallet the battery touched, for every demo tool. The
            # earlier per-call receipt checks already prove each denial that
            # carried a receipt has no ledger linkage; this cross-wallet sweep
            # additionally catches a regression that wrote a debit without a
            # receipt or under an unexpected tool/wallet pair (e.g. a leak in
            # the wallet_mismatch path that hit the attacker wallet, or a leak
            # in the tool_not_allowed path that hit BLOCKED_TOOL).
            victim_debits_after_attacks = await _any_demo_tool_debits(
                client, victim_wallet_id, victim_headers
            )
            attacker_debits_after_attacks = await _any_demo_tool_debits(
                client, attacker_wallet_id, attacker_headers
            )
            require(
                victim_debits_after_attacks == [],
                f"an attack produced a victim ledger debit: "
                f"{victim_debits_after_attacks}",
            )
            require(
                attacker_debits_after_attacks == [],
                f"an attack produced an attacker ledger debit: "
                f"{attacker_debits_after_attacks}",
            )

            step("positive control: the one valid call succeeds and charges once")
            success = await post_json(
                client,
                "/mcp/messages",
                headers=victim_headers,
                expected_status=200,
                json_body=build_mcp_call(
                    request_id="rt-positive-control",
                    tool=ALLOWED_TOOL,
                    wallet_id=victim_wallet_id,
                    idempotency_key="rt-positive-control",
                    permit_id=valid_permit_id,
                    arguments={"message": "hello after the siege"},
                ),
            )
            require("result" in success, f"positive control failed: {success}")
            success_receipt = success["result"]["receipt"]
            require(
                success_receipt["outcome"] == "success",
                f"positive control did not succeed: {success_receipt}",
            )

            final_debits = await _allowed_tool_debits(
                client, victim_wallet_id, victim_headers
            )
            require(
                len(final_debits) == 1,
                f"expected exactly one debit after the run, got {final_debits}",
            )

            step("verifying the victim wallet audit chain survived the siege")
            audit_check = await post_json(
                client,
                "/v1/audit/verify-chain",
                headers=victim_headers,
                expected_status=200,
                json_body={"wallet_id": victim_wallet_id},
            )
            require(
                audit_check["valid"] is True,
                f"audit chain invalid after attacks: {audit_check}",
            )

            require(
                len(attacks) == 10,
                f"expected 10 denied attacks, recorded {len(attacks)}",
            )

            attacks_with_receipts = sum(1 for a in attacks if a["receipt_id"])
            summary = {
                "victim_wallet_id": victim_wallet_id,
                "attacker_wallet_id": attacker_wallet_id,
                "valid_permit_id": valid_permit_id,
                "attacks_denied": len(attacks),
                "attacks_with_signed_denial_receipts": attacks_with_receipts,
                "denial_reasons": [a["reason"] for a in attacks],
                "victim_debits_after_attacks": len(victim_debits_after_attacks),
                "attacker_debits_after_attacks": len(attacker_debits_after_attacks),
                "success_receipt_id": success_receipt["receipt_id"],
                "ledger_debits_total": len(final_debits),
                "audit_chain_valid": audit_check["valid"],
                "audit_chain_checked_events": audit_check["checked_events"],
            }

        if json_output:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            step("red-team proof complete: every attack denied, one charge landed")
            for key_name, value in summary.items():
                print(f"  {key_name}: {value}")
        return summary
    finally:
        unregister_demo_tools()
        await close_db()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the final proof artifact as JSON.",
    )
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        action="store_true",
        help="Compatibility flag: the script always asserts invariants.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_red_team(json_output=args.json))


if __name__ == "__main__":
    main()
