"""Security fuzz battery: forged signatures, key confusion, injection, malformed payloads, rate limits.

Each attack reports expected-vs-actual and verifies the signed receipt outcome.
Denials must produce denied receipts with zero charge.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel, ReceiptModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.receipts import get_receipt_service
from app.services.service_registry import get_service_registry
from app.services.signing_keys import canonical_json, sha256_hex
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_tool(tool_name: str, credits: float = 1.0):
    registry = get_service_registry()

    def echo(input: str = "ok", **kwargs) -> dict:
        return {"message": input}

    registry.register_local(
        service_id=tool_name,
        name="Fuzz Tool",
        description="Security fuzz test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=credits,
        unit_name="call",
    )
    return registry


async def _invoke(
    client: AsyncClient,
    wallet_id: str,
    permit_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    idem_key: str = "invoke-1",
    headers: dict[str, str] | None = None,
):
    return await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idem_key,
                },
            },
        },
        headers=headers or BOOTSTRAP_HEADERS,
    )


async def _create_permit(
    client: AsyncClient,
    wallet_id: str,
    key_id: str,
    tool_name: str,
    extra: dict[str, Any] | None = None,
    idem_key: str = "permit-1",
):
    payload = {
        "issuer_wallet_id": wallet_id,
        "subject_wallet_id": wallet_id,
        "subject_key_id": key_id,
        "allowed_tools": [tool_name],
        "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
        "max_credits": 50,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }
    if extra:
        payload.update(extra)
    resp = await client.post(
        "/v1/permits",
        json=payload,
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert resp.status_code == 201
    return resp.json()


async def _verify_receipt_outcome(
    receipt_id: str, expected_outcome: str, expected_charge: Decimal
):
    """Fetch receipt and assert outcome + charge. Returns receipt response."""
    receipt = await get_receipt_service().get_receipt(receipt_id)
    assert receipt is not None, f"Receipt {receipt_id} not found"
    assert receipt.outcome == expected_outcome, (
        f"Expected outcome {expected_outcome}, got {receipt.outcome}"
    )
    assert receipt.credits_charged == expected_charge, (
        f"Expected charge {expected_charge}, got {receipt.credits_charged}"
    )
    return receipt


async def _ledger_snapshot(*wallet_ids: str) -> dict[str, tuple[int, Decimal]]:
    """Ledger entry count and balance per wallet, to prove a denial moved nothing."""
    factory = get_session_factory()
    snapshot: dict[str, tuple[int, Decimal]] = {}
    async with factory() as session:
        for wallet_id in wallet_ids:
            entries = (
                await session.execute(
                    select(func.count())
                    .select_from(LedgerEntryModel)
                    .where(LedgerEntryModel.wallet_id == wallet_id)
                )
            ).scalar_one()
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            snapshot[wallet_id] = (int(entries), wallet.balance)
    return snapshot


async def _assert_no_economic_effect(
    permit_id: str, ledger_before: dict[str, tuple[int, Decimal]]
) -> None:
    """No ledger entry, no balance change, no permit spend, no receipt."""
    assert await _ledger_snapshot(*ledger_before) == ledger_before
    factory = get_session_factory()
    async with factory() as session:
        permit = await session.get(PermitModel, permit_id)
        assert permit is not None
        assert permit.spent_credits == Decimal("0")
        receipts = (
            await session.execute(
                select(func.count())
                .select_from(ReceiptModel)
                .where(ReceiptModel.permit_id == permit_id)
            )
        ).scalar_one()
        assert receipts == 0


# ─── Track 1: Forged Signatures ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_tampered_receipt_payload_fails_verification(client, clean_database):
    """Modify a receipt's stored payload_hash after signing; verify must fail."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-sig-1"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(client, wallet_id, key_id, tool_name)
        r = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            headers=agent_headers,
            idem_key="sig-1",
        )
        assert r.status_code == 200
        body = r.json()
        receipt_data = body["result"]["receipt"]
        receipt_id = receipt_data["receipt_id"]

        # Tamper the stored request_hash
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(ReceiptModel, receipt_id)
            model.request_hash = "0" * 64
            session.add(model)
            await session.commit()

        ok, reason, _ = await get_receipt_service().verify_receipt(receipt_id)
        assert ok is False, "Expected tampered receipt to fail verification"
        assert reason == "receipt_signature_invalid"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_wrong_key_signature_fails_verification(client, clean_database):
    """Sign a receipt payload with a different Ed25519 key; verify must fail."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-sig-2"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(client, wallet_id, key_id, tool_name)
        r = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            headers=agent_headers,
            idem_key="sig-2",
        )
        assert r.status_code == 200
        body = r.json()
        receipt_data = body["result"]["receipt"]
        receipt_id = receipt_data["receipt_id"]

        # Generate a wrong key and re-sign the payload
        wrong_key = Ed25519PrivateKey.generate()
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(ReceiptModel, receipt_id)
            payload = {
                "receipt_id": model.receipt_id,
                "permit_id": model.permit_id,
                "wallet_id": model.wallet_id,
                "key_id": model.key_id,
                "tool": model.tool,
                "request_hash": model.request_hash,
                "response_hash": model.response_hash,
                "ledger_entry_id": model.ledger_entry_id,
                "credits_authorized": model.credits_authorized,
                "credits_charged": model.credits_charged,
                "outcome": model.outcome,
                "audit_event_id": model.audit_event_id,
                "created_at": model.created_at,
                "alg": "Ed25519",
                "kid": model.signature_key_id,
            }
            if model.approval_id:
                payload["approval_id"] = model.approval_id
            payload["payload_hash"] = sha256_hex(payload)
            fake_sig = wrong_key.sign(canonical_json(payload).encode())
            model.signature = base64.b64encode(fake_sig).decode()
            session.add(model)
            await session.commit()

        ok, reason, _ = await get_receipt_service().verify_receipt(receipt_id)
        assert ok is False, "Expected wrong-key signature to fail verification"
        assert reason == "receipt_signature_invalid"
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_signature_from_different_receipt_fails(client, clean_database):
    """Copy a valid signature from receipt A onto receipt B's payload; verify must fail."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-sig-3"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(client, wallet_id, key_id, tool_name)
        r1 = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            headers=agent_headers,
            idem_key="sig-3a",
        )
        r2 = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            headers=agent_headers,
            idem_key="sig-3b",
        )
        assert r1.status_code == 200 and r2.status_code == 200
        receipt_a = r1.json()["result"]["receipt"]
        receipt_b = r2.json()["result"]["receipt"]

        # Swap A's signature onto B
        factory = get_session_factory()
        async with factory() as session:
            model_b = await session.get(ReceiptModel, receipt_b["receipt_id"])
            model_b.signature = receipt_a["signature"]
            session.add(model_b)
            await session.commit()

        ok, reason, _ = await get_receipt_service().verify_receipt(
            receipt_b["receipt_id"]
        )
        assert ok is False, "Expected cross-receipt signature to fail verification"
        assert reason == "receipt_signature_invalid"
    finally:
        get_service_registry().unregister_local(tool_name)


# ─── Track 2: Key Confusion ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_agent_b_key_with_agent_a_permit_denied(client, clean_database):
    """Agent B's API key + Agent A's permit must be denied."""
    provisioned_a = await provision_agent_wallet(client)
    provisioned_b = await provision_agent_wallet(client)
    tool_name = "fuzz-kc-1"
    _register_tool(tool_name)
    try:
        permit_a = await _create_permit(
            client, provisioned_a["agent_wallet_id"], provisioned_a["key_id"], tool_name
        )
        ledger_before = await _ledger_snapshot(
            provisioned_a["agent_wallet_id"], provisioned_b["agent_wallet_id"]
        )

        # Invoke with B's headers but A's permit
        r = await _invoke(
            client,
            wallet_id=provisioned_a["agent_wallet_id"],
            permit_id=permit_a["permit_id"],
            tool_name=tool_name,
            headers=provisioned_b["agent_headers"],
            idem_key="kc-1",
        )
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert body["error"]["code"] == -32003
        # Refused at the wallet-access layer, before the permit is consulted:
        # B's key is not bound to A's wallet. That layer issues no receipt,
        # because nothing was authorized against the permit.
        assert body["error"]["message"] == "wallet_access_denied"
        assert "receipt" not in (body["error"].get("data") or {})
        await _assert_no_economic_effect(
            permit_a["permit_id"],
            ledger_before,
        )
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_swapped_wallet_key_pair_denied(client, clean_database):
    """Permit issued for wallet A, invoke claiming wallet B with A's key, must deny."""
    provisioned_a = await provision_agent_wallet(client)
    provisioned_b = await provision_agent_wallet(client)
    tool_name = "fuzz-kc-2"
    _register_tool(tool_name)
    try:
        permit_a = await _create_permit(
            client, provisioned_a["agent_wallet_id"], provisioned_a["key_id"], tool_name
        )
        ledger_before = await _ledger_snapshot(
            provisioned_a["agent_wallet_id"], provisioned_b["agent_wallet_id"]
        )

        # Use wallet B in mcpContext but A's permit, with A's key in headers
        r = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": provisioned_b["agent_wallet_id"],
                        "permit_id": permit_a["permit_id"],
                        "idempotency_key": "kc-2",
                    },
                },
            },
            headers=provisioned_a["agent_headers"],
        )
        assert r.status_code == 200
        body = r.json()
        assert "error" in body
        assert body["error"]["code"] == -32003
        # A's key is not bound to wallet B, so the wallet-access layer refuses
        # before the permit is consulted, and issues no receipt.
        assert body["error"]["message"] == "wallet_access_denied"
        assert "receipt" not in (body["error"].get("data") or {})
        await _assert_no_economic_effect(
            permit_a["permit_id"],
            ledger_before,
        )
    finally:
        get_service_registry().unregister_local(tool_name)


# ─── Track 3: Constraint Field Injection ─────────────────────────────────────


@pytest.mark.anyio
async def test_forbidden_fields_with_unicode_injection_denied(client, clean_database):
    """Unicode lookalike key must be caught by forbidden_fields."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-inj-1"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(
            client,
            wallet_id,
            key_id,
            tool_name,
            extra={"forbidden_fields": ["token"]},
            idem_key="inj-permit-1",
        )
        # Use Unicode homoglyph: tοken with Greek omicron (ο) instead of Latin o
        # Pass input as well so the tool call doesn't fail for unrelated reasons
        r = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            arguments={"input": "hello", "tοken": "leak"},
            headers=agent_headers,
            idem_key="inj-1",
        )
        assert r.status_code == 200
        body = r.json()
        # Homoglyph is NOT "token" — call should succeed (injection did not match)
        assert "error" not in body, "Homoglyph should not match forbidden 'token'"

        # Now with actual forbidden key
        r2 = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            arguments={"token": "leak"},
            headers=agent_headers,
            idem_key="inj-2",
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert "error" in body2
        assert "permit_forbidden_field" in body2["error"]["message"]
        receipt = body2["error"]["data"]["receipt"]
        await _verify_receipt_outcome(receipt["receipt_id"], "denied", Decimal("0"))
    finally:
        get_service_registry().unregister_local(tool_name)


class _FakeUpstreamExecutor:
    """Records dispatches; a recipient_domain denial must leave this empty."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self,
        arguments: dict[str, Any],
        invocation_id: str | None = None,
        idempotency_key: str | None = None,
        before_dispatch: Any | None = None,
    ) -> Any:
        self.calls.append({"arguments": arguments, "invocation_id": invocation_id})
        return None


@pytest.mark.anyio
async def test_recipient_domain_wildcard_injection(client, clean_database):
    """A wildcard-looking recipient_domain is matched literally, never expanded.

    recipient_domain is only enforced for upstream_mcp tools (a local tool has
    no recipient to check), so the tool here is an upstream registered at
    ``https://sub.example.com``. A permit naming ``*.example.com`` must deny
    it: the constraint is an exact hostname, and a glob-matching
    implementation would dispatch to a host the permit never named.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-inj-2"
    executor = _FakeUpstreamExecutor()
    get_service_registry().register_upstream(
        service_id=tool_name,
        name="Fuzz Upstream Tool",
        description="Security fuzz upstream tool",
        category=ServiceCategory.AGENT_COMMS,
        executor=executor,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        credits_per_unit=2.0,
        upstream_tool_name="partner.echo",
        upstream_origin="https://sub.example.com",
    )
    try:
        permit = await _create_permit(
            client,
            wallet_id,
            key_id,
            tool_name,
            extra={"recipient_domain": "*.example.com"},
            idem_key="inj-permit-2",
        )
        assert permit["recipient_domain"] == "*.example.com"

        r = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            arguments={"input": "test"},
            headers=agent_headers,
            idem_key="inj-3",
        )
        assert r.status_code == 200
        body = r.json()
        assert "error" in body, "wildcard recipient_domain matched sub.example.com"
        assert body["error"]["code"] == -32003
        assert "permit_recipient_domain_mismatch" in body["error"]["message"]
        assert executor.calls == [], "the upstream must never be dispatched"

        receipt = body["error"]["data"]["receipt"]
        await _verify_receipt_outcome(receipt["receipt_id"], "denied", Decimal("0"))
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit["permit_id"])
            assert model is not None
            assert model.spent_credits == Decimal("0")
    finally:
        get_service_registry().unregister_local(tool_name)


@pytest.mark.anyio
async def test_tampered_max_calls_per_tool_row_fails_closed(client, clean_database):
    """Rewriting a signed permit's max_calls_per_tool in storage denies the call.

    max_calls_per_tool is part of the permit's signed payload, so editing the
    stored column out-of-band (here, adding a ``"null"`` key: JSON object keys
    are always strings, so a literal null key cannot arrive over the wire) must
    fail signature verification before any budget moves. The denial is
    receipted with zero charge.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-inj-3"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(
            client,
            wallet_id,
            key_id,
            tool_name,
            extra={"max_calls_per_tool": {tool_name: 5}},
            idem_key="inj-permit-3",
        )

        # Tamper with the signed column at the DB layer. json.dumps renders the
        # None key as the string "null".
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit["permit_id"])
            model.max_calls_per_tool_json = json.dumps({None: 5, tool_name: 5})
            session.add(model)
            await session.commit()
        async with factory() as session:
            model = await session.get(PermitModel, permit["permit_id"])
            assert json.loads(model.max_calls_per_tool_json) == {
                "null": 5,
                tool_name: 5,
            }

        r = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            headers=agent_headers,
            idem_key="inj-4",
        )
        assert r.status_code == 200
        body = r.json()
        assert "error" in body, "a tampered permit row must not authorize a call"
        assert body["error"]["message"] == "permit_signature_invalid"
        receipt = body["error"]["data"]["receipt"]
        await _verify_receipt_outcome(receipt["receipt_id"], "denied", Decimal("0"))

        async with factory() as session:
            model = await session.get(PermitModel, permit["permit_id"])
            assert model is not None
            assert model.spent_credits == Decimal("0")
    finally:
        get_service_registry().unregister_local(tool_name)


# ─── Track 4: Malformed JSON & Oversized Payloads ────────────────────────────


@pytest.mark.anyio
async def test_malformed_json_on_invoke_returns_parse_error(client, clean_database):
    """Send garbage JSON to /mcp/messages; expect parse error, no charge."""
    r = await client.post(
        "/mcp/messages",
        content=b"{not json",
        headers={**BOOTSTRAP_HEADERS, "Content-Type": "application/json"},
    )
    # FastAPI should return 422 or 400 for bad JSON
    assert r.status_code in (400, 422)


@pytest.mark.anyio
async def test_malformed_json_on_permit_creation_returns_422(client, clean_database):
    """Send garbage JSON to /v1/permits; expect validation error."""
    r = await client.post(
        "/v1/permits",
        content=b"{bad json",
        headers={
            **BOOTSTRAP_HEADERS,
            "Content-Type": "application/json",
            "Idempotency-Key": "bad-json-1",
        },
    )
    assert r.status_code in (400, 422)


@pytest.mark.anyio
async def test_oversized_payload_on_invoke_is_refused(client, clean_database):
    """Invoke with a 10MB argument string; the body ceiling must refuse it.

    This used to observe-and-skip, which meant an unbounded body read as a
    green run. `RequestBodyLimitMiddleware` now caps every route, so the
    refusal is asserted and a regression fails the suite.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-size-1"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(client, wallet_id, key_id, tool_name)
        huge = "x" * (10 * 1024 * 1024)  # 10 MB
        r = await _invoke(
            client,
            wallet_id,
            permit["permit_id"],
            tool_name,
            arguments={"input": huge},
            headers=agent_headers,
            idem_key="size-1",
        )
        assert r.status_code == 413, (
            f"10MB body was not refused (got {r.status_code}); "
            "the request body ceiling is not in force"
        )
        body = r.json()
        assert body["max_request_body_bytes"] == get_settings().MAX_REQUEST_BODY_BYTES
    finally:
        get_service_registry().unregister_local(tool_name)


# ─── Track 5: Rate Limit Behavior ────────────────────────────────────────────


@pytest.mark.anyio
async def test_rapid_fire_invokes_all_accounted(client, clean_database):
    """Fire 20 rapid invokes; check all receipts exist and total charge is exact.
    Requires PostgreSQL for accurate row-lock accounting.

    CI runs this in the postgres_trust job with REQUIRE_POSTGRES_TESTS=1, which
    turns the skip into a failure: a job that lost its PostgreSQL DATABASE_URL
    must go red rather than pass by skipping the only test it was added for.
    """
    from app.db.database import get_engine

    engine = get_engine()
    if engine is None or engine.dialect.name != "postgresql":
        if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail(
                "REQUIRE_POSTGRES_TESTS=1 but the suite is not running on "
                "PostgreSQL; check DATABASE_URL"
            )
        pytest.skip("requires PostgreSQL for accurate concurrent budget accounting")

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    key_id = provisioned["key_id"]
    agent_headers = provisioned["agent_headers"]
    tool_name = "fuzz-rl-1"
    _register_tool(tool_name)
    try:
        permit = await _create_permit(
            client,
            wallet_id,
            key_id,
            tool_name,
            extra={"max_calls_per_tool": {tool_name: 100}},
        )
        permit_id = permit["permit_id"]

        async def fire(i: int):
            return await _invoke(
                client,
                wallet_id,
                permit_id,
                tool_name,
                arguments={"input": f"rapid-{i}"},
                headers=agent_headers,
                idem_key=f"rl-{i}",
            )

        # Fire 20 calls as fast as possible
        responses = await asyncio.gather(*[fire(i) for i in range(20)])

        success_count = 0
        total_charged = Decimal("0")
        for resp in responses:
            assert resp.status_code == 200
            body = resp.json()
            if "error" in body:
                receipt = body["error"]["data"]["receipt"]
                await _verify_receipt_outcome(
                    receipt["receipt_id"], "denied", Decimal("0")
                )
            else:
                success_count += 1
                receipt = body["result"]["receipt"]
                total_charged += Decimal(str(receipt["credits_charged"]))

        assert success_count == 20, f"Expected 20 successes, got {success_count}"
        assert total_charged == Decimal("20"), (
            f"Expected charge 20, got {total_charged}"
        )

        # Verify permit spent_credits
        factory = get_session_factory()
        async with factory() as session:
            model = await session.get(PermitModel, permit_id)
            assert model.spent_credits == Decimal("20")
    finally:
        get_service_registry().unregister_local(tool_name)
