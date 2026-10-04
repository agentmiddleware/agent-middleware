#!/usr/bin/env python3
"""One-command Agent Ops War Room proof.

Runs the real FastAPI app in-process and prints an operator timeline for a
wallet-scoped MCP invocation: discovery, authority, receipt, replay safety,
ledger/audit evidence, audit-chain verification, and out-of-scope denial.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALLOWED_TOOL = "war-room-echo"
DENIED_TOOL = "war-room-denied"
SIGNING_PRIVATE_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _line(emit: bool, message: str) -> None:
    if emit:
        print(f"[war-room] {message}")


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = await client.get(path, headers=headers)
    _require(
        response.status_code == 200,
        f"GET {path} returned {response.status_code}: {response.text}",
    )
    return response.json()


async def _post_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    json_body: dict[str, Any],
    headers: dict[str, str],
    expected_status: int = 200,
) -> dict[str, Any]:
    response = await client.post(path, json=json_body, headers=headers)
    _require(
        response.status_code == expected_status,
        f"POST {path} returned {response.status_code}: {response.text}",
    )
    return response.json()


def _jsonrpc_result(payload: dict[str, Any]) -> dict[str, Any]:
    _require("result" in payload, f"expected JSON-RPC result, got: {payload}")
    return payload["result"]


def _jsonrpc_error(payload: dict[str, Any]) -> dict[str, Any]:
    _require("error" in payload, f"expected JSON-RPC error, got: {payload}")
    return payload["error"]


def _matching_echo_debits(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in ledger["entries"]
        if entry["service_category"] == "agent_comms"
        and ALLOWED_TOOL in entry.get("description", "")
    ]


def _mcp_call(
    *,
    request_id: str,
    tool: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments or {},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


def _register_war_room_tools() -> None:
    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry

    registry = get_service_registry()

    def echo(message: str = "ready") -> dict[str, Any]:
        return {"message": message, "controlled": True}

    def denied() -> dict[str, Any]:
        return {"should_not_execute": True}

    registry.register_local(
        service_id=ALLOWED_TOOL,
        name="War Room Echo",
        description="Governed MCP demo tool allowed by the signed permit",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    registry.register_local(
        service_id=DENIED_TOOL,
        name="War Room Denied",
        description="Governed MCP demo tool intentionally outside permit scope",
        category=ServiceCategory.AGENT_COMMS,
        func=denied,
        credits_per_unit=2.0,
        unit_name="call",
    )


def _unregister_war_room_tools() -> None:
    from app.services.service_registry import get_service_registry

    registry = get_service_registry()
    registry.unregister_local(ALLOWED_TOOL)
    registry.unregister_local(DENIED_TOOL)


async def run_war_room(
    *,
    client: httpx.AsyncClient,
    bootstrap_api_key: str,
    emit: bool = True,
) -> dict[str, Any]:
    """Run the Agent Ops proof against an existing in-process client."""

    bootstrap_headers = {"X-API-Key": bootstrap_api_key}
    result: dict[str, Any] = {"status": "running"}

    _register_war_room_tools()
    try:
        _line(emit, "discover platform surfaces")
        agent_manifest = await _get_json(client, "/.well-known/agent.json")
        tools_manifest = await _get_json(client, "/mcp/tools.json")
        openapi = await _get_json(client, "/openapi.json")
        _require(
            any(tool["name"] == ALLOWED_TOOL for tool in tools_manifest["tools"]),
            "war-room-echo missing from MCP tools discovery",
        )

        _line(emit, "create sponsor wallet")
        sponsor = await _post_json(
            client,
            "/v1/billing/wallets/sponsor",
            headers=bootstrap_headers,
            expected_status=201,
            json_body={
                "sponsor_name": "War Room Sponsor",
                "email": "war-room@example.com",
                "initial_credits": 10000,
                "require_kyc": False,
            },
        )

        _line(emit, "create agent wallet")
        agent = await _post_json(
            client,
            "/v1/billing/wallets/agent",
            headers=bootstrap_headers,
            expected_status=201,
            json_body={
                "sponsor_wallet_id": sponsor["wallet_id"],
                "agent_id": "war-room-agent",
                "budget_credits": 1000,
                "daily_limit": 500,
            },
        )
        wallet_id = agent["wallet_id"]

        _line(emit, "mint wallet-scoped agent API key")
        key = await _post_json(
            client,
            "/v1/api-keys",
            headers=bootstrap_headers,
            expected_status=201,
            json_body={
                "wallet_id": wallet_id,
                "key_name": "war-room-runtime",
                "expires_in_days": 30,
            },
        )
        agent_headers = {"X-API-Key": key["api_key"]}

        _line(emit, "issue signed permit scoped to war-room-echo")
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        permit = await _post_json(
            client,
            "/v1/permits",
            headers={**bootstrap_headers, "Idempotency-Key": "war-room-permit-1"},
            expected_status=201,
            json_body={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key["key_id"],
                "allowed_tools": [ALLOWED_TOOL],
                "scopes": [f"tool:{ALLOWED_TOOL}:invoke", "billing:charge"],
                "max_credits": 25,
                "expires_at": expires_at,
            },
        )

        _line(emit, "invoke governed MCP tool")
        invoke_body = _mcp_call(
            request_id="war-room-call-1",
            tool=ALLOWED_TOOL,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            idempotency_key="war-room-invoke-1",
            arguments={"message": "operator timeline"},
        )
        invoke_payload = await _post_json(
            client,
            "/mcp/messages",
            headers=agent_headers,
            json_body=invoke_body,
        )
        invoke_result = _jsonrpc_result(invoke_payload)
        receipt = invoke_result["receipt"]
        _require(receipt["outcome"] == "success", f"unexpected receipt: {receipt}")
        _require(receipt["ledger_entry_id"], "success receipt missing ledger entry")

        _line(emit, "replay same request and confirm same receipt")
        replay_payload = await _post_json(
            client,
            "/mcp/messages",
            headers=agent_headers,
            json_body=invoke_body,
        )
        replay_receipt = _jsonrpc_result(replay_payload)["receipt"]
        same_receipt = replay_receipt["receipt_id"] == receipt["receipt_id"]
        _require(same_receipt, "replay did not return original receipt")

        _line(emit, "inspect ledger for exactly one echo debit")
        ledger_payload = await _get_json(
            client,
            f"/v1/billing/ledger/{wallet_id}",
            headers=agent_headers,
        )
        echo_debits = _matching_echo_debits(ledger_payload)
        _require(len(echo_debits) == 1, f"expected one echo debit, got {echo_debits}")

        _line(emit, "verify signed receipt")
        receipt_verify = await _post_json(
            client,
            "/v1/receipts/verify",
            headers=agent_headers,
            json_body={"receipt_id": receipt["receipt_id"]},
        )
        _require(receipt_verify["valid"] is True, f"receipt invalid: {receipt_verify}")

        _line(emit, "agent inspects its own trust ledger")
        self_permits = await _get_json(
            client,
            "/v1/me/permits?status=active",
            headers=agent_headers,
        )
        _require(
            any(
                row["permit_id"] == permit["permit_id"]
                for row in self_permits["permits"]
            ),
            f"self permit inspection missed permit: {self_permits}",
        )
        self_receipts = await _get_json(
            client,
            f"/v1/me/receipts?permit_id={permit['permit_id']}",
            headers=agent_headers,
        )
        _require(
            any(
                row["receipt_id"] == receipt["receipt_id"]
                for row in self_receipts["receipts"]
            ),
            f"self receipt inspection missed receipt: {self_receipts}",
        )

        _line(emit, "fetch audit events for wallet and tool")
        audit_events = await _get_json(
            client,
            f"/v1/audit/events?wallet_id={wallet_id}&tool={ALLOWED_TOOL}",
            headers=agent_headers,
        )
        _require(audit_events["total"] >= 1, f"missing audit events: {audit_events}")

        _line(emit, "verify audit chain")
        audit_chain = await _post_json(
            client,
            "/v1/audit/verify-chain",
            headers=agent_headers,
            json_body={"wallet_id": wallet_id},
        )
        _require(audit_chain["valid"] is True, f"audit chain invalid: {audit_chain}")
        self_audit = await _get_json(
            client,
            f"/v1/me/audit/events?tool={ALLOWED_TOOL}",
            headers=agent_headers,
        )
        _require(self_audit["total"] >= 1, f"missing self audit events: {self_audit}")

        _line(emit, "attempt out-of-scope tool with same permit")
        denial_body = _mcp_call(
            request_id="war-room-denial-1",
            tool=DENIED_TOOL,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            idempotency_key="war-room-denial-1",
        )
        denial_payload = await _post_json(
            client,
            "/mcp/messages",
            headers=agent_headers,
            json_body=denial_body,
        )
        denial_error = _jsonrpc_error(denial_payload)
        _require(
            denial_error["message"] == "permit_tool_not_allowed",
            f"unexpected denial: {denial_error}",
        )

        ledger_after_denial = await _get_json(
            client,
            f"/v1/billing/ledger/{wallet_id}",
            headers=agent_headers,
        )
        debits_after_denial = len(_matching_echo_debits(ledger_after_denial))
        _require(
            debits_after_denial == len(echo_debits),
            "replay or denial created an extra echo debit",
        )

        result = {
            "status": "pass",
            "discovery": {
                "agent_manifest": agent_manifest.get("name")
                or agent_manifest.get("service"),
                "tools_seen": len(tools_manifest["tools"]),
                "openapi_title": openapi["info"]["title"],
            },
            "sponsor": {"wallet_id": sponsor["wallet_id"]},
            "agent": {
                "wallet_id": f"wallet-{wallet_id}",
                "runtime_wallet_id": wallet_id,
                "key_id": key["key_id"],
            },
            "permit": {
                "permit_id": permit["permit_id"],
                "allowed_tools": permit["allowed_tools"],
                "expires_at": permit["expires_at"],
            },
            "invoke": {
                "receipt": receipt,
                "ledger_entry_id": receipt["ledger_entry_id"],
            },
            "replay": {
                "same_receipt": same_receipt,
                "receipt_id": replay_receipt["receipt_id"],
            },
            "ledger": {
                "matching_debits": len(echo_debits),
                "entry_ids": [entry["entry_id"] for entry in echo_debits],
            },
            "receipt": {
                "valid": receipt_verify["valid"],
                "reason": receipt_verify["reason"],
            },
            "self_inspection": {
                "permits": self_permits["total"],
                "receipts": self_receipts["total"],
                "audit_events": self_audit["total"],
            },
            "audit": {
                "events": audit_events["total"],
                "chain": audit_chain,
            },
            "denial": {
                "reason": denial_error["message"],
                "receipt": denial_error.get("data", {}).get("receipt"),
                "ledger_debits_after": debits_after_denial,
            },
        }
        _line(emit, "control-plane loop proved")
        return result
    finally:
        _unregister_war_room_tools()


def _configure_local_environment(
    *,
    database_url: str,
    bootstrap_api_key: str,
) -> None:
    os.environ["DATABASE_URL"] = database_url
    os.environ["VALID_API_KEYS"] = bootstrap_api_key
    os.environ["TRUST_MODE_ENABLED"] = "true"
    os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
    os.environ["TRUST_SIGNING_KEY_ID"] = "war-room-ed25519"
    os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = SIGNING_PRIVATE_KEY_B64

    from app.core.config import get_settings

    get_settings.cache_clear()


async def run_in_process(
    *,
    database_url: str | None = None,
    bootstrap_api_key: str = "war-room-bootstrap-key",
    emit: bool = True,
) -> dict[str, Any]:
    """Run the demo against the local FastAPI app using ASGITransport."""

    async def run_with_database(db_url: str) -> dict[str, Any]:
        _configure_local_environment(
            database_url=db_url,
            bootstrap_api_key=bootstrap_api_key,
        )

        from app.db.database import close_db, init_db
        from app.main import app

        await close_db()
        await init_db()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://war-room.local",
            ) as client:
                result = await run_war_room(
                    client=client,
                    bootstrap_api_key=bootstrap_api_key,
                    emit=emit,
                )
            if emit:
                print("AGENT OPS WAR ROOM: PASS")
            return result
        finally:
            await close_db()

    if database_url:
        return await run_with_database(database_url)

    with tempfile.TemporaryDirectory(prefix="agent-ops-war-room-") as tmpdir:
        db_path = Path(tmpdir) / "war-room.db"
        return await run_with_database(f"sqlite+aiosqlite:///{db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Agent Ops War Room trust-plane proof."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable proof artifact.",
    )
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        action="store_true",
        help="Compatibility flag for CI; failures already raise AssertionError.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional SQLAlchemy async database URL. Defaults to a temp SQLite DB.",
    )
    parser.add_argument(
        "--bootstrap-api-key",
        default="war-room-bootstrap-key",
        help="Bootstrap API key used to create the proof wallets and key.",
    )
    args = parser.parse_args()

    result = asyncio.run(
        run_in_process(
            database_url=args.database_url,
            bootstrap_api_key=args.bootstrap_api_key,
            emit=not args.json,
        )
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
