#!/usr/bin/env python3
"""Dogfood playground: one fake partner tool behind the trust plane.

Registers ``partner.notes.write`` (appends to a real local notes file) and
``partner.notes.admin`` (intentionally out of permit scope). Exercises:

  permit → governed invoke → charge → receipt → replay → out-of-scope deny

Run::

    make dogfood-trust-plane
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
DOGFOOD_DB = ROOT / "data" / "dogfood_trust_plane.db"
NOTES_FILE = ROOT / "data" / "dogfood_partner_notes.jsonl"
ADMIN_KEY = "dogfood-admin-key"
DEMO_PRIVATE_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
ALLOWED_TOOL = "partner.notes.write"
BLOCKED_TOOL = "partner.notes.admin"
PRINT_STEPS = True


def configure_environment() -> None:
    DOGFOOD_DB.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DOGFOOD_DB}"
    os.environ["VALID_API_KEYS"] = ADMIN_KEY
    os.environ["TRUST_MODE_ENABLED"] = "true"
    os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
    os.environ["ENABLE_PROOF_SURFACES"] = "false"
    os.environ["TRUST_SIGNING_KEY_ID"] = "demo-ed25519"
    os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = DEMO_PRIVATE_KEY_B64


configure_environment()
sys.path.insert(0, str(ROOT))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.database import close_db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.billing import ServiceCategory  # noqa: E402
from app.services.service_registry import get_service_registry  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def step(message: str) -> None:
    if PRINT_STEPS:
        print(f"[dogfood] {message}")


def remove_paths() -> None:
    for path in (DOGFOOD_DB, Path(f"{DOGFOOD_DB}-shm"), Path(f"{DOGFOOD_DB}-wal")):
        if path.exists():
            path.unlink()
    if NOTES_FILE.exists():
        NOTES_FILE.unlink()


def read_notes() -> list[dict[str, Any]]:
    if not NOTES_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in NOTES_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def register_partner_tools() -> None:
    registry = get_service_registry()
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text("", encoding="utf-8")

    def write_note(text: str = "hello") -> dict[str, Any]:
        """Stand-in for one internal partner tool (writes a durable note)."""
        entry = {
            "text": text,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "governed": True,
        }
        with NOTES_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return {
            "status": "ok",
            "tool": ALLOWED_TOOL,
            "note": entry,
            "notes_path": str(NOTES_FILE.relative_to(ROOT)),
            "note_count": len(read_notes()),
        }

    def admin_probe() -> dict[str, str]:
        return {"should_not_execute": "true"}

    registry.register_local(
        service_id=ALLOWED_TOOL,
        name="Partner Notes Write",
        description=(
            "Dogfood stand-in for one internal MCP tool. "
            "Appends a note to a local JSONL file under permit control."
        ),
        category=ServiceCategory.AGENT_COMMS,
        func=write_note,
        credits_per_unit=2.0,
        unit_name="call",
        require_permit=True,
    )
    registry.register_local(
        service_id=BLOCKED_TOOL,
        name="Partner Notes Admin",
        description="Out-of-scope tool used to prove permit denial",
        category=ServiceCategory.AGENT_COMMS,
        func=admin_probe,
        credits_per_unit=2.0,
        unit_name="call",
        require_permit=True,
    )


def unregister_partner_tools() -> None:
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


def mcp_call(
    *,
    request_id: str,
    tool: str,
    wallet_id: str,
    permit_id: str | None,
    idempotency_key: str | None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"wallet_id": wallet_id}
    if permit_id:
        context["permit_id"] = permit_id
    if idempotency_key:
        context["idempotency_key"] = idempotency_key
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments or {},
            "mcpContext": context,
        },
    }


async def run_dogfood(json_output: bool = False) -> dict[str, Any]:
    global PRINT_STEPS
    PRINT_STEPS = not json_output

    await close_db()
    remove_paths()
    await init_db()
    register_partner_tools()

    admin_headers = {"X-API-Key": ADMIN_KEY}
    summary: dict[str, Any] = {
        "allowed_tool": ALLOWED_TOOL,
        "blocked_tool": BLOCKED_TOOL,
        "notes_path": str(NOTES_FILE.relative_to(ROOT)),
    }

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://dogfood"
        ) as client:
            step("creating sponsor + agent wallet + API key")
            sponsor = await post_json(
                client,
                "/v1/billing/wallets/sponsor",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_name": "Dogfood Sponsor",
                    "email": "dogfood@example.com",
                    "initial_credits": 5000,
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
                    "agent_id": "dogfood-agent",
                    "budget_credits": 500,
                    "daily_limit": 250,
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
                    "key_name": "dogfood-runtime",
                    "expires_in_days": 7,
                },
            )
            agent_headers = {"X-API-Key": key["api_key"]}

            step(f"discovering MCP tools (expect {ALLOWED_TOOL})")
            tools = await get_json(
                client,
                "/mcp/tools.json",
                headers=agent_headers,
                expected_status=200,
            )
            by_name = {t["name"]: t for t in tools["tools"]}
            require(ALLOWED_TOOL in by_name, f"{ALLOWED_TOOL} missing from discovery")
            require(
                by_name[ALLOWED_TOOL]["annotations"].get("requirePermit") is True,
                f"{ALLOWED_TOOL} must requirePermit",
            )

            step(f"issuing permit scoped only to {ALLOWED_TOOL}")
            permit = await post_json(
                client,
                "/v1/permits",
                headers={**admin_headers, "Idempotency-Key": "dogfood-permit-1"},
                expected_status=201,
                json_body={
                    "issuer_wallet_id": agent_wallet_id,
                    "subject_wallet_id": agent_wallet_id,
                    "subject_key_id": key["key_id"],
                    "allowed_tools": [ALLOWED_TOOL],
                    "scopes": [f"tool:{ALLOWED_TOOL}:invoke", "billing:charge"],
                    "max_credits": 25,
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=30)
                    ).isoformat(),
                },
            )

            note_text = "dogfood: first governed note"
            call_body = mcp_call(
                request_id="dogfood-1",
                tool=ALLOWED_TOOL,
                wallet_id=agent_wallet_id,
                permit_id=permit["permit_id"],
                idempotency_key="dogfood-invoke-1",
                arguments={"text": note_text},
            )

            step(f"invoking {ALLOWED_TOOL} (writes a real note file)")
            first = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=call_body,
            )
            require("error" not in first, f"unexpected error: {first}")
            result = first["result"]
            require(result.get("isError") is False, f"tool failed: {result}")
            receipt = result["receipt"]
            require(receipt["outcome"] == "success", "expected success receipt")

            notes = read_notes()
            require(len(notes) == 1, f"expected 1 note on disk, got {notes}")
            require(notes[0]["text"] == note_text, f"note text mismatch: {notes[0]}")
            step(f"note on disk: {NOTES_FILE.relative_to(ROOT)} → {notes[0]['text']!r}")

            step("verifying signed receipt")
            receipt_check = await post_json(
                client,
                "/v1/receipts/verify",
                headers=agent_headers,
                expected_status=200,
                json_body={"receipt_id": receipt["receipt_id"]},
            )
            require(receipt_check["valid"] is True, f"receipt invalid: {receipt_check}")

            step("replaying same idempotency key (no second note / no second debit)")
            replay = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=call_body,
            )
            replay_receipt = replay["result"]["receipt"]
            require(
                replay_receipt["receipt_id"] == receipt["receipt_id"],
                "replay must return same receipt",
            )
            require(
                len(read_notes()) == 1,
                "replay must not write a second note",
            )

            ledger = await get_json(
                client,
                f"/v1/billing/ledger/{agent_wallet_id}",
                headers=agent_headers,
                expected_status=200,
            )
            debits = [
                e
                for e in ledger["entries"]
                if e["action"] == "debit" and e.get("service_category") == "agent_comms"
            ]
            require(len(debits) == 1, f"expected one debit after replay, got {debits}")

            step(f"out-of-scope invoke of {BLOCKED_TOOL} (must deny)")
            denied = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=mcp_call(
                    request_id="dogfood-deny",
                    tool=BLOCKED_TOOL,
                    wallet_id=agent_wallet_id,
                    permit_id=permit["permit_id"],
                    idempotency_key="dogfood-deny-1",
                ),
            )
            err = denied.get("error") or {}
            denial_reason = err.get("message") or (denied.get("result") or {}).get(
                "error"
            )
            # Governed path may return JSON-RPC error or isError result.
            if "error" in denied:
                denial_reason = denied["error"].get("message", denial_reason)
            else:
                denial_result = denied["result"]
                require(
                    denial_result.get("isError") is True, f"expected deny: {denied}"
                )
                denial_reason = denial_result.get("error") or denial_reason
            require(
                denial_reason == "permit_tool_not_allowed",
                f"expected permit_tool_not_allowed, got {denial_reason!r} / {denied}",
            )
            require(len(read_notes()) == 1, "denial must not write a note")

            step("ungoverned invoke (no permit) must fail closed")
            ungoverned = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=mcp_call(
                    request_id="dogfood-no-permit",
                    tool=ALLOWED_TOOL,
                    wallet_id=agent_wallet_id,
                    permit_id=None,
                    idempotency_key=None,
                    arguments={"text": "should not land"},
                ),
            )
            ug_reason = None
            if "error" in ungoverned:
                ug_reason = ungoverned["error"].get("message")
            else:
                ug_result = ungoverned["result"]
                require(
                    ug_result.get("isError") is True, f"expected deny: {ungoverned}"
                )
                ug_reason = ug_result.get("error")
            require(
                ug_reason == "permit_required",
                f"expected permit_required, got {ug_reason!r}",
            )
            require(len(read_notes()) == 1, "ungoverned call must not write")

            summary.update(
                {
                    "agent_wallet_id": agent_wallet_id,
                    "permit_id": permit["permit_id"],
                    "success_receipt_id": receipt["receipt_id"],
                    "ledger_entry_id": receipt["ledger_entry_id"],
                    "replay_receipt_id": replay_receipt["receipt_id"],
                    "denial_reason": denial_reason,
                    "ungoverned_denial_reason": ug_reason,
                    "notes_on_disk": read_notes(),
                    "note_count": len(read_notes()),
                }
            )
            step(
                "dogfood complete — one partner tool, real side effect, trust loop held"
            )
            if PRINT_STEPS:
                print(f"  tool: {ALLOWED_TOOL}")
                print(f"  notes: {NOTES_FILE.relative_to(ROOT)}")
                print(f"  note_count: {summary['note_count']}")
                print(f"  receipt: {summary['success_receipt_id']}")
                print(f"  denial: {denial_reason}")
                print(f"  ungoverned: {ug_reason}")
    finally:
        unregister_partner_tools()
        await close_db()

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable summary only",
    )
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        action="store_true",
        help="Exit non-zero if the dogfood loop fails",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = asyncio.run(run_dogfood(json_output=args.json))
    except Exception as exc:
        if args.assert_mode or args.json:
            print(str(exc), file=sys.stderr)
            return 1
        raise
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
