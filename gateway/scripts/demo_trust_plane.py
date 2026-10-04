#!/usr/bin/env python3
"""One-command proof for the MCP governance trust plane.

The demo uses a throwaway local SQLite database and the real FastAPI routers.
It proves a governed MCP call can be scoped, charged, receipted, audited,
replayed safely, and denied when outside permit scope. It also times a run of
fresh governed calls, so the public site can publish the gateway's own
per-call handler time with an honest local caveat.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEMO_DB = ROOT / "data" / "demo_trust_plane.db"
ADMIN_KEY = "demo-admin-key"
DEMO_PRIVATE_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
ALLOWED_TOOL = "trust-plane-echo"
BLOCKED_TOOL = "trust-plane-admin-ledger"
# Fresh governed calls timed for the public site's latency line. The loop runs
# under its own wallet-bound key, so it shares nobody's rate-limit bucket, and
# the count must stay below RATE_LIMIT_PER_MINUTE (120 by default): every
# sample is one request inside a single sixty-second window.
LATENCY_SAMPLES = 100
LATENCY_TRANSPORT = "in-process ASGI client, local SQLite, no network"
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
# The offline verifier ships in the SDK and must not import the app — that
# independence is exactly what the proof below demonstrates.
sys.path.insert(0, str(ROOT.parent / "sdk" / "python" / "src"))

from decimal import Decimal  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.database import close_db, get_session_factory, init_db  # noqa: E402
from app.db.models import ControlPlaneAuditEventModel, ReceiptModel  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.billing import ServiceCategory  # noqa: E402
from app.services.service_registry import get_service_registry  # noqa: E402
from b2a_sdk.receipt_verifier import (  # noqa: E402
    key_set_from_document,
    verify_bundle,
)


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
        print(f"[trust-demo] {message}")


def first_jsonrpc_result(response: dict[str, Any]) -> dict[str, Any]:
    require("result" in response, f"expected JSON-RPC result, got: {response}")
    return response["result"]


def first_jsonrpc_error(response: dict[str, Any]) -> dict[str, Any]:
    require("error" in response, f"expected JSON-RPC error, got: {response}")
    return response["error"]


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


def register_demo_tools() -> None:
    registry = get_service_registry()

    def echo(message: str = "ok") -> dict[str, str]:
        return {"message": message, "governed": "true"}

    def admin_ledger_probe() -> dict[str, str]:
        return {"should_not_execute": "true"}

    registry.register_local(
        service_id=ALLOWED_TOOL,
        name="Trust Plane Echo",
        description=(
            "Design-partner reference tool: stand-in for one internal MCP tool. "
            "Replace with a real tool id when onboarding a partner."
        ),
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2.0,
        unit_name="call",
        require_permit=True,
    )
    registry.register_local(
        service_id=BLOCKED_TOOL,
        name="Trust Plane Blocked Admin Tool",
        description="Demo tool intentionally outside the signed permit",
        category=ServiceCategory.AGENT_COMMS,
        func=admin_ledger_probe,
        credits_per_unit=2.0,
        unit_name="call",
        require_permit=True,
    )


def unregister_demo_tools() -> None:
    registry = get_service_registry()
    registry.unregister_local(ALLOWED_TOOL)
    registry.unregister_local(BLOCKED_TOOL)


async def _tamper_receipt(receipt_id: str) -> None:
    """Mutate a signed field on a stored receipt so its signature no longer holds."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ReceiptModel).where(ReceiptModel.receipt_id == receipt_id)
        )
        receipt = result.scalar_one()
        receipt.credits_charged = receipt.credits_charged + Decimal("999")
        session.add(receipt)
        await session.commit()


async def _tamper_audit_event(wallet_id: str) -> None:
    """Mutate a stored audit event's payload so the chain hash no longer matches."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ControlPlaneAuditEventModel)
            .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
            .order_by(ControlPlaneAuditEventModel.created_at)
        )
        event = result.scalars().first()
        require(event is not None, "no audit event to tamper")
        event.metadata_json = '{"tampered": true}'
        session.add(event)
        await session.commit()


def build_mcp_call(
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


def _percentile_ms(sorted_ms: list[float], quantile: float) -> float:
    """Nearest-rank percentile of already-sorted millisecond samples."""
    rank = max(1, math.ceil(quantile * len(sorted_ms)))
    return sorted_ms[rank - 1]


async def sample_gateway_latency(
    client: AsyncClient,
    *,
    admin_headers: dict[str, str],
    agent_wallet_id: str,
    samples: int,
) -> dict[str, Any]:
    """Time a run of fresh governed calls and summarise the distribution.

    Every call carries a new idempotency key, so each one is a real permit
    reservation, dispatch, debit, receipt and audit event rather than a
    replay. The loop runs under its own wallet-bound key and its own permit,
    so it neither shares the demo agent key's rate-limit bucket nor touches
    the budget the recorded transcript narrates. The stopwatch wraps the raw
    client post with no helper and no recording in between, so the number is
    the gateway's handler time and nothing else. It is measured over the
    in-process ASGI transport against local SQLite: a floor for what the
    boundary adds, not a production latency.
    """
    key = await post_json(
        client,
        "/v1/api-keys",
        headers=admin_headers,
        expected_status=201,
        json_body={
            "wallet_id": agent_wallet_id,
            "key_name": "trust-plane-demo-latency",
            "expires_in_days": 30,
        },
    )
    sample_headers = {"X-API-Key": key["api_key"]}
    permit = await post_json(
        client,
        "/v1/permits",
        headers={**admin_headers, "Idempotency-Key": "demo-permit-latency"},
        expected_status=201,
        json_body={
            "issuer_wallet_id": agent_wallet_id,
            "subject_wallet_id": agent_wallet_id,
            "subject_key_id": key["key_id"],
            "allowed_tools": [ALLOWED_TOOL],
            "scopes": [f"tool:{ALLOWED_TOOL}:invoke", "billing:charge"],
            # Two credits per call, plus headroom so the last sample does not
            # land exactly on the ceiling and raise a budget-exhausted alert.
            "max_credits": samples * 2 + 2,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
    )

    durations_ms: list[float] = []
    for index in range(samples):
        body = build_mcp_call(
            request_id=f"demo-latency-{index}",
            tool=ALLOWED_TOOL,
            wallet_id=agent_wallet_id,
            permit_id=permit["permit_id"],
            idempotency_key=f"demo-latency-{index}",
            arguments={"message": f"latency sample {index}"},
        )
        started = time.perf_counter()
        response = await client.post("/mcp/messages", headers=sample_headers, json=body)
        durations_ms.append((time.perf_counter() - started) * 1000.0)
        require(
            response.status_code == 200,
            f"latency sample {index} returned {response.status_code}: {response.text}",
        )
        result = first_jsonrpc_result(response.json())
        require(result["isError"] is False, f"latency sample {index} failed: {result}")

    ordered = sorted(durations_ms)
    return {
        "samples": samples,
        "path": "POST /mcp/messages",
        "tool": ALLOWED_TOOL,
        "transport": LATENCY_TRANSPORT,
        "p50_ms": round(_percentile_ms(ordered, 0.50), 1),
        "p95_ms": round(_percentile_ms(ordered, 0.95), 1),
        "min_ms": round(ordered[0], 1),
        "max_ms": round(ordered[-1], 1),
        "mean_ms": round(sum(ordered) / len(ordered), 1),
    }


async def run_demo(json_output: bool = False) -> dict[str, Any]:
    global PRINT_STEPS

    PRINT_STEPS = not json_output
    await close_db()
    remove_demo_db()
    await init_db()
    register_demo_tools()

    admin_headers = {"X-API-Key": ADMIN_KEY}
    summary: dict[str, Any] = {}

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://demo") as client:
            step("creating sponsor wallet")
            sponsor = await post_json(
                client,
                "/v1/billing/wallets/sponsor",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_name": "Demo Sponsor",
                    "email": "demo-sponsor@example.com",
                    "initial_credits": 10000,
                    "require_kyc": False,
                },
            )
            sponsor_wallet_id = sponsor["wallet_id"]

            step("creating agent wallet")
            agent = await post_json(
                client,
                "/v1/billing/wallets/agent",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "sponsor_wallet_id": sponsor_wallet_id,
                    "agent_id": "trust-plane-demo-agent",
                    "budget_credits": 1000,
                    "daily_limit": 500,
                },
            )
            agent_wallet_id = agent["wallet_id"]

            step("creating wallet-bound API key")
            key = await post_json(
                client,
                "/v1/api-keys",
                headers=admin_headers,
                expected_status=201,
                json_body={
                    "wallet_id": agent_wallet_id,
                    "key_name": "trust-plane-demo-runtime",
                    "expires_in_days": 30,
                },
            )
            agent_headers = {"X-API-Key": key["api_key"]}

            step("discovering MCP tools")
            tools = await get_json(
                client,
                "/mcp/tools.json",
                headers=agent_headers,
                expected_status=200,
            )
            require(
                any(tool["name"] == ALLOWED_TOOL for tool in tools["tools"]),
                "allowed demo tool missing from MCP discovery",
            )

            step("issuing signed permit scoped to one MCP tool")
            permit = await post_json(
                client,
                "/v1/permits",
                headers={**admin_headers, "Idempotency-Key": "demo-permit-1"},
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

            step("verifying signed permit")
            permit_check = await post_json(
                client,
                "/v1/permits/verify",
                headers=agent_headers,
                expected_status=200,
                json_body={
                    "permit_id": permit["permit_id"],
                    "wallet_id": agent_wallet_id,
                    "tool": ALLOWED_TOOL,
                    "estimated_credits": 2,
                },
            )
            require(permit_check["valid"] is True, f"permit invalid: {permit_check}")

            step("inspecting permit and active signing key metadata")
            permit_lookup = await get_json(
                client,
                f"/v1/permits/{permit['permit_id']}",
                headers=agent_headers,
                expected_status=200,
            )
            require(
                permit_lookup["subject_key_id"] == key["key_id"],
                f"permit lookup missing key binding: {permit_lookup}",
            )
            active_signing_key = await get_json(
                client,
                "/v1/signing-keys/active",
                headers=admin_headers,
                expected_status=200,
            )
            require(
                active_signing_key["key_id"] == permit["key_id"],
                f"active signing key mismatch: {active_signing_key}",
            )
            require(
                "private_key" not in active_signing_key
                and "private_key_b64" not in active_signing_key,
                "signing key metadata leaked private material",
            )

            call_body = build_mcp_call(
                request_id="demo-call-1",
                tool=ALLOWED_TOOL,
                wallet_id=agent_wallet_id,
                permit_id=permit["permit_id"],
                idempotency_key="demo-invoke-1",
                arguments={"message": "hello trust plane"},
            )

            step("invoking governed MCP tool")
            first_call = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=call_body,
            )
            result = first_jsonrpc_result(first_call)
            require(result["isError"] is False, f"tool call failed: {result}")
            receipt = result["receipt"]
            require(receipt["outcome"] == "success", "success receipt missing")
            require(receipt["ledger_entry_id"], "receipt missing ledger entry")

            step("verifying signed receipt")
            receipt_check = await post_json(
                client,
                "/v1/receipts/verify",
                headers=agent_headers,
                expected_status=200,
                json_body={"receipt_id": receipt["receipt_id"]},
            )
            require(receipt_check["valid"] is True, f"receipt invalid: {receipt_check}")

            step("inspecting receipt through operator filters")
            receipt_list = await get_json(
                client,
                (
                    "/v1/receipts"
                    f"?permit_id={permit['permit_id']}"
                    f"&wallet_id={agent_wallet_id}"
                    f"&tool={ALLOWED_TOOL}"
                    "&outcome=success"
                ),
                headers=agent_headers,
                expected_status=200,
            )
            require(
                receipt_list["total"] == 1, f"receipt filter failed: {receipt_list}"
            )
            permit_receipts = await get_json(
                client,
                f"/v1/permits/{permit['permit_id']}/receipts",
                headers=agent_headers,
                expected_status=200,
            )
            require(
                permit_receipts["receipts"][0]["receipt_id"] == receipt["receipt_id"],
                f"permit receipt drilldown failed: {permit_receipts}",
            )

            step("showing ledger debit")
            ledger = await get_json(
                client,
                f"/v1/billing/ledger/{agent_wallet_id}",
                headers=agent_headers,
                expected_status=200,
            )
            echo_debits = [
                entry
                for entry in ledger["entries"]
                if entry["service_category"] == "agent_comms"
                and ALLOWED_TOOL in entry.get("description", "")
            ]
            require(len(echo_debits) == 1, f"expected one debit, got {echo_debits}")

            step("inspecting wallet audit event")
            audit_events = await get_json(
                client,
                (
                    "/v1/audit/events"
                    f"?wallet_id={agent_wallet_id}"
                    "&event=mcp.invoke"
                    f"&tool={ALLOWED_TOOL}"
                    "&ok=true"
                ),
                headers=agent_headers,
                expected_status=200,
            )
            require(audit_events["total"] >= 1, f"audit filter failed: {audit_events}")
            audit_metadata = audit_events["events"][0]["metadata"]
            require(
                audit_metadata["permit_id"] == permit["permit_id"],
                f"audit missing permit linkage: {audit_metadata}",
            )
            require(
                audit_metadata["idempotency_key"] == "demo-invoke-1",
                f"audit missing idempotency linkage: {audit_metadata}",
            )
            require(
                audit_metadata["ledger_entry_id"] == receipt["ledger_entry_id"],
                f"audit missing ledger linkage: {audit_metadata}",
            )
            require(
                audit_metadata["request_hash"],
                f"audit missing request hash: {audit_metadata}",
            )

            step("replaying same idempotency key")
            replay = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=call_body,
            )
            replay_receipt = first_jsonrpc_result(replay)["receipt"]
            require(
                replay_receipt["receipt_id"] == receipt["receipt_id"],
                "replay did not return original receipt",
            )
            ledger_after_replay = await get_json(
                client,
                f"/v1/billing/ledger/{agent_wallet_id}",
                headers=agent_headers,
                expected_status=200,
            )
            echo_debits_after_replay = [
                entry
                for entry in ledger_after_replay["entries"]
                if entry["service_category"] == "agent_comms"
                and ALLOWED_TOOL in entry.get("description", "")
            ]
            require(
                len(echo_debits_after_replay) == 1,
                "replay created a duplicate ledger debit",
            )

            # Checked after the replay on purpose: the public transcript shows
            # this step after the replay, so the count it prints covers every
            # governed call the reader has seen by then.
            step("verifying audit chain")
            audit_check = await post_json(
                client,
                "/v1/audit/verify-chain",
                headers=agent_headers,
                expected_status=200,
                json_body={"wallet_id": agent_wallet_id},
            )
            require(audit_check["valid"] is True, f"audit invalid: {audit_check}")
            require(audit_check["checked_events"] >= 1, "audit chain checked no events")

            step("attempting out-of-scope MCP tool")
            denial_body = build_mcp_call(
                request_id="demo-denial-1",
                tool=BLOCKED_TOOL,
                wallet_id=agent_wallet_id,
                permit_id=permit["permit_id"],
                idempotency_key="demo-denial-1",
            )
            denial_call = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=denial_body,
            )
            denial_error = first_jsonrpc_error(denial_call)
            require(
                denial_error["message"] == "permit_tool_not_allowed",
                f"unexpected denial: {denial_error}",
            )
            denial_receipt = denial_error["data"]["receipt"]
            require(denial_receipt["outcome"] == "denied", "denial receipt missing")
            require(
                denial_receipt["ledger_entry_id"] is None,
                "denied call should not have a ledger debit",
            )

            denial_replay = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=denial_body,
            )
            denial_replay_error = first_jsonrpc_error(denial_replay)
            require(
                denial_replay_error["data"]["receipt"]["receipt_id"]
                == denial_receipt["receipt_id"],
                "denied replay did not return original denial receipt",
            )

            step("attempting MCP call with no permit (trust-mode fail-closed)")
            unpermitted_body = {
                "jsonrpc": "2.0",
                "id": "demo-unpermitted-1",
                "method": "tools/call",
                "params": {
                    "name": ALLOWED_TOOL,
                    "arguments": {"message": "should never reach the tool"},
                    "mcpContext": {
                        "wallet_id": agent_wallet_id,
                        "idempotency_key": "demo-unpermitted-1",
                    },
                },
            }
            unpermitted_call = await post_json(
                client,
                "/mcp/messages",
                headers=agent_headers,
                expected_status=200,
                json_body=unpermitted_body,
            )
            unpermitted_error = first_jsonrpc_error(unpermitted_call)
            require(
                unpermitted_error["message"] == "permit_required",
                f"ungoverned call was not denied: {unpermitted_error}",
            )
            ledger_after_unpermitted = await get_json(
                client,
                f"/v1/billing/ledger/{agent_wallet_id}",
                headers=agent_headers,
                expected_status=200,
            )
            require(
                len(
                    [
                        entry
                        for entry in ledger_after_unpermitted["entries"]
                        if entry["service_category"] == "agent_comms"
                        and ALLOWED_TOOL in entry.get("description", "")
                    ]
                )
                == 1,
                "ungoverned call produced a ledger debit",
            )

            step("proving tenant isolation")
            cross_wallet = await client.get(
                f"/v1/billing/wallets/{sponsor_wallet_id}",
                headers=agent_headers,
            )
            require(
                cross_wallet.status_code == 403,
                f"cross-wallet read was not denied: {cross_wallet.text}",
            )

            step("inspecting buyer-facing evidence bundle")
            bundle = await get_json(
                client,
                f"/v1/evidence/{receipt['receipt_id']}",
                headers=agent_headers,
                expected_status=200,
            )
            require(bundle["valid"] is True, f"evidence bundle invalid: {bundle}")
            verification = bundle["verification"]
            for field_name in (
                "receipt_signature",
                "permit_signature",
                "audit_chain",
                "request_hash",
            ):
                require(
                    verification.get(field_name) == "ok",
                    f"evidence verification {field_name} not ok: {verification}",
                )
            require(
                bundle["permit"]["permit_id"] == permit["permit_id"],
                f"evidence bundle missing permit linkage: {bundle}",
            )
            require(
                bundle["ledger_entry"]
                and bundle["ledger_entry"]["entry_id"] == receipt["ledger_entry_id"],
                f"evidence bundle missing ledger linkage: {bundle}",
            )

            step("proving the receipt verifies offline, with no credentials")
            portable = await get_json(
                client,
                f"/v1/receipts/{receipt['receipt_id']}/portable",
                headers=agent_headers,
                expected_status=200,
            )
            # Deliberately unauthenticated: a third party holding this receipt
            # has no key here, and must still be able to fetch what it takes to
            # check the signature.
            key_document = await get_json(
                client,
                "/.well-known/trust-keys.json",
                headers={},
                expected_status=200,
            )
            key_set = key_set_from_document(key_document)
            offline_result = verify_bundle(portable, key_set)
            require(
                offline_result.ok,
                f"receipt did not verify offline: {offline_result.reason}",
            )
            require(
                offline_result.claims.get("permit_id") == permit["permit_id"],
                "offline claims do not name the permit that authorized the call",
            )

            forged = dict(portable)
            forged["signing_input"] = portable["signing_input"].replace(
                '"outcome":"success"', '"outcome":"failed_refunded"'
            )
            require(
                forged["signing_input"] != portable["signing_input"],
                "forgery setup did not modify the signed bytes",
            )
            forged_result = verify_bundle(forged, key_set)
            require(
                forged_result.is_tampered,
                f"edited receipt still verified offline: {forged_result.status}",
            )
            require(
                verify_bundle(portable, {}).status.value == "unknown_key",
                "a missing key was reported as tampering rather than unknown",
            )

            step(f"timing {LATENCY_SAMPLES} fresh governed calls")
            require(
                LATENCY_SAMPLES < get_settings().RATE_LIMIT_PER_MINUTE,
                "LATENCY_SAMPLES must stay below RATE_LIMIT_PER_MINUTE",
            )
            gateway_latency = await sample_gateway_latency(
                client,
                admin_headers=admin_headers,
                agent_wallet_id=agent_wallet_id,
                samples=LATENCY_SAMPLES,
            )
            step(
                f"gateway latency over {gateway_latency['samples']} governed calls: "
                f"p50 {gateway_latency['p50_ms']} ms · p95 {gateway_latency['p95_ms']} ms "
                f"({gateway_latency['transport']})"
            )

            # The remaining proofs mutate stored rows, so they run last on the
            # throwaway demo database.
            step("proving a tampered receipt fails verification")
            await _tamper_receipt(receipt["receipt_id"])
            tampered_receipt_check = await post_json(
                client,
                "/v1/receipts/verify",
                headers=admin_headers,
                expected_status=200,
                json_body={"receipt_id": receipt["receipt_id"]},
            )
            require(
                tampered_receipt_check["valid"] is False,
                f"tampered receipt still verified: {tampered_receipt_check}",
            )
            require(
                tampered_receipt_check["reason"] == "receipt_signature_invalid",
                f"unexpected tampered-receipt reason: {tampered_receipt_check}",
            )

            step("proving a tampered audit event fails chain verification")
            await _tamper_audit_event(agent_wallet_id)
            tampered_audit_check = await post_json(
                client,
                "/v1/audit/verify-chain",
                headers=admin_headers,
                expected_status=200,
                json_body={"wallet_id": agent_wallet_id},
            )
            require(
                tampered_audit_check["valid"] is False,
                f"tampered audit chain still verified: {tampered_audit_check}",
            )

            summary = {
                "sponsor_wallet_id": sponsor_wallet_id,
                "agent_wallet_id": agent_wallet_id,
                "agent_key_id": key["key_id"],
                "permit_id": permit["permit_id"],
                "success_receipt_id": receipt["receipt_id"],
                "ledger_entry_id": receipt["ledger_entry_id"],
                "signing_key_id": active_signing_key["key_id"],
                "inspected_receipts": receipt_list["total"],
                "inspected_audit_events": audit_events["total"],
                "audit_chain_checked_events": audit_check["checked_events"],
                "replay_receipt_id": replay_receipt["receipt_id"],
                "denial_receipt_id": denial_receipt["receipt_id"],
                "denial_replay_receipt_id": denial_replay_error["data"]["receipt"][
                    "receipt_id"
                ],
                "denial_reason": denial_error["message"],
                "ungoverned_denial_reason": unpermitted_error["message"],
                "cross_wallet_status": cross_wallet.status_code,
                "evidence_bundle_valid": bundle["valid"],
                "offline_verified": offline_result.ok,
                "offline_signing_key_id": offline_result.key_id,
                "offline_forgery_detected": forged_result.is_tampered,
                "tampered_receipt_valid": tampered_receipt_check["valid"],
                "tampered_receipt_reason": tampered_receipt_check["reason"],
                "tampered_audit_valid": tampered_audit_check["valid"],
                "tampered_audit_reason": tampered_audit_check["reason"],
                "gateway_latency": gateway_latency,
            }

        if json_output:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            step("trust-plane proof complete")
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
    asyncio.run(run_demo(json_output=args.json))


if __name__ == "__main__":
    main()
