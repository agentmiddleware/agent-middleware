"""x402 settlement-facilitation surface (dormant).

Covers strict HTTP 402 parsing (per-header failure reasons, network
allowlist, address shapes), the permit-governed settle loop (budget caps,
atomic compensation on mid-flight failure, idempotent replay), the
transfer-authorization shapes (EIP-712 TransferWithAuthorization for EVM,
structured Ed25519-signable message for Solana), and the SDK helpers.

The module stem is in conftest's DORMANT_SURFACE_TEST_MODULES, so the x402
router is mounted on the shared app by the dormant-marked fixture. Nothing
here touches real ledger entries: settlements land in the shadow ledger and
signed receipts only (docs/settlement-rails.md settlement freeze).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.audit_log import list_audit_events
from app.services.receipts import (
    ReceiptService,
    ReceiptWriteContendedError,
    get_receipt_service,
)
from app.services.shadow_ledger import get_shadow_ledger
from app.services.x402_engine import X402Error, get_x402_handler
from b2a_sdk.errors import PermitDeniedError
from b2a_sdk.x402 import X402Client, parse_402_response
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

EVM_PAY_TO = "0x1111111111111111111111111111111111111111"
EVM_PAYER = "0x2222222222222222222222222222222222222222"
SOLANA_PAY_TO = "A" * 40  # valid base58 alphabet, length within 32-44


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _x402_headers(
    amount: str = "1.50",
    pay_to: str = EVM_PAY_TO,
    network: str = "base",
) -> dict[str, str]:
    return {
        "X-402-Amount": amount,
        "X-402-PayTo": pay_to,
        "X-402-Network": network,
    }


def _settle_body(
    *,
    permit_id: str,
    wallet_id: str,
    amount: str = "0.03",
    pay_to: str = EVM_PAY_TO,
    network: str = "base",
    payer: str | None = EVM_PAYER,
) -> dict:
    body = {
        "permit_id": permit_id,
        "wallet_id": wallet_id,
        "amount": amount,
        "pay_to": pay_to,
        "network": network,
    }
    if payer is not None:
        body["payer"] = payer
    return body


async def _permit_spent(client: AsyncClient, permit_id: str) -> Decimal:
    resp = await client.get(f"/v1/permits/{permit_id}", headers=BOOTSTRAP_HEADERS)
    assert resp.status_code == 200
    return Decimal(str(resp.json()["spent_credits"]))


class _StubResponse:
    """Duck-typed stand-in for httpx.Response in SDK parse tests."""

    def __init__(self, status_code: int, headers: dict[str, str]):
        self.status_code = status_code
        self.headers = headers


# ---------------------------------------------------------------------------
# Acceptance tests (exact names required by the work package)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_x402_permit_budget_cap_exceeded(client, clean_database):
    """A settle whose credits exceed the permit cap is denied atomically:
    reason surfaces untouched, no budget is consumed, no receipt exists."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=10,
        idem_key="x402-cap-permit",
    )
    permit_id = permit["permit_id"]

    # $0.02 at the default 1000 credits/USD rate = 20 credits > 10 cap.
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit_id,
            wallet_id=provisioned["agent_wallet_id"],
            amount="0.02",
        ),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-cap-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "permit_budget_exceeded"

    assert await _permit_spent(client, permit_id) == Decimal("0")
    receipts, total = await get_receipt_service().list_receipts(permit_id=permit_id)
    assert total == 0
    assert receipts == []


@pytest.mark.anyio
async def test_x402_atomic_settlement(client, clean_database, monkeypatch):
    """A failure at the receipt step compensates fully (budget released, no
    receipt, no live shadow session); the retried key then settles exactly
    once, the receipt verifies, and replay returns the same settlement."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-atomic-permit",
    )
    permit_id = permit["permit_id"]
    body = _settle_body(permit_id=permit_id, wallet_id=wallet_id, amount="0.03")
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "x402-atomic-1"}

    assert await _permit_spent(client, permit_id) == Decimal("0")

    async def _induced_receipt_failure(self, **kwargs):
        raise ReceiptWriteContendedError("induced rolled-back receipt failure")

    monkeypatch.setattr(ReceiptService, "create_receipt", _induced_receipt_failure)
    failed = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert failed.status_code == 400
    assert failed.json()["detail"] == "x402_settlement_failed"

    # Full compensation: reservation released, no receipt, no live shadow
    # session left holding the simulated charge.
    assert await _permit_spent(client, permit_id) == Decimal("0")
    _, total = await get_receipt_service().list_receipts(permit_id=permit_id)
    assert total == 0
    assert await get_shadow_ledger().list_sessions(wallet_id) == []

    # Un-patch and retry the SAME idempotency key: the failed attempt was
    # side-effect free, so the key must be reusable.
    monkeypatch.undo()
    ok = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert ok.status_code == 200
    settlement = ok.json()
    assert settlement["receipt_id"].startswith("rcpt-")
    assert Decimal(settlement["credits"]) == Decimal("30")
    assert await _permit_spent(client, permit_id) == Decimal("30")

    receipts, total = await get_receipt_service().list_receipts(permit_id=permit_id)
    assert total == 1
    assert receipts[0].receipt_id == settlement["receipt_id"]
    assert receipts[0].outcome == "success"
    assert Decimal(str(receipts[0].credits_charged)) == Decimal("30")

    verify = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": settlement["receipt_id"]},
        headers=provisioned["agent_headers"],
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True

    # Replay with the SAME key and body: same settlement, no double-reserve.
    replay = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["receipt_id"] == settlement["receipt_id"]
    assert replayed["authorization"] == settlement["authorization"]
    assert await _permit_spent(client, permit_id) == Decimal("30")
    _, total = await get_receipt_service().list_receipts(permit_id=permit_id)
    assert total == 1


@pytest.mark.anyio
async def test_x402_settle_solana_end_to_end(client, clean_database):
    """The full Solana settle path: permit reservation, attestation over the
    Solana structured message (payer optional on Solana), shadow metering,
    a verifiable receipt, and exact budget consumption."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-solana-permit",
    )
    permit_id = permit["permit_id"]

    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit_id,
            wallet_id=wallet_id,
            amount="0.03",
            pay_to=SOLANA_PAY_TO,
            network="solana",
            payer=None,  # optional on Solana; the wallet signs natively
        ),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-solana-1"},
    )
    assert resp.status_code == 200
    settlement = resp.json()
    assert settlement["network"] == "solana"
    assert settlement["receipt_id"].startswith("rcpt-")

    authorization = settlement["authorization"]
    assert authorization["scheme"] == "x402-solana-transfer/1"
    assert authorization["pay_to"] == SOLANA_PAY_TO
    assert authorization["amount"] == "30000"  # $0.03 in 6-decimal base units
    assert "payer" not in authorization
    # settle threads the permit's issued_at..expires_at window (unix seconds)
    # into the signed Solana message, mirroring EVM validAfter/validBefore.
    valid_after = int(authorization["valid_after"])
    valid_before = int(authorization["valid_before"])
    assert 0 < valid_after < valid_before

    verify = await client.post(
        "/v1/receipts/verify",
        json={"receipt_id": settlement["receipt_id"]},
        headers=provisioned["agent_headers"],
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True

    # $0.03 at the default 1000 credits/USD rate = 30 credits, all reserved.
    assert Decimal(settlement["credits"]) == Decimal("30")
    assert await _permit_spent(client, permit_id) == Decimal(settlement["credits"])


@pytest.mark.anyio
async def test_x402_settle_solana_rejects_malformed_payer(client, clean_database):
    """Payer is optional on Solana, but a PRESENT payer must be a valid
    base58 address: a malformed one is refused before any budget moves."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-solana-payer-permit",
    )

    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"],
            wallet_id=wallet_id,
            amount="0.03",
            pay_to=SOLANA_PAY_TO,
            network="solana",
            payer="0Ol-invalid",  # excluded base58 characters, wrong length
        ),
        headers={
            **provisioned["agent_headers"],
            "Idempotency-Key": "x402-solana-payer-1",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "x402_payer_invalid"
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Parsing and transfer-authorization shapes
# ---------------------------------------------------------------------------


def test_x402_parse_evm_happy_path_and_eip712_shape():
    handler = get_x402_handler()
    # Case-insensitive header lookup is part of the contract.
    requirement = handler.parse_402(
        402,
        {
            "x-402-amount": "1.50",
            "X-402-PAYTO": EVM_PAY_TO,
            "X-402-Network": "base",
        },
    )
    assert requirement.amount_usd == Decimal("1.50")
    assert requirement.pay_to == EVM_PAY_TO
    assert requirement.network == "base"
    assert requirement.asset == "USDC"

    authorization = handler.build_transfer_authorization(
        requirement,
        permit_id="permit-abc",
        wallet_id="wallet-abc",
        idempotency_key="idem-abc",
        payer=EVM_PAYER,
        valid_after=100,
        valid_before=200,
    )
    assert authorization["primaryType"] == "TransferWithAuthorization"
    assert authorization["domain"] == {
        "name": "USD Coin",
        "version": "2",
        "chainId": 8453,
        "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
    field_names = [
        item["name"] for item in authorization["types"]["TransferWithAuthorization"]
    ]
    assert field_names == ["from", "to", "value", "validAfter", "validBefore", "nonce"]
    message = authorization["message"]
    assert message["to"] == EVM_PAY_TO
    assert message["value"] == "1500000"  # $1.50 in 6-decimal USDC base units
    assert message["validAfter"] == "100"
    assert message["validBefore"] == "200"
    # The attested message binds the real payer address: the facilitator
    # signature covers this exact payload, so a blank `from` would leave the
    # actually-signed transfer unbound from the attestation.
    assert message["from"] == EVM_PAYER
    nonce = message["nonce"]
    assert nonce.startswith("0x") and len(nonce) == 66  # 32 bytes of hex
    int(nonce, 16)  # must be hex

    # Deterministic per (permit_id, idempotency_key): idempotent replays
    # rebuild the identical authorization; a new key gets a new nonce.
    again = handler.build_transfer_authorization(
        requirement,
        permit_id="permit-abc",
        wallet_id="wallet-abc",
        idempotency_key="idem-abc",
        payer=EVM_PAYER,
        valid_after=100,
        valid_before=200,
    )
    assert again == authorization
    other = handler.build_transfer_authorization(
        requirement,
        permit_id="permit-abc",
        wallet_id="wallet-abc",
        idempotency_key="idem-other",
        payer=EVM_PAYER,
        valid_after=100,
        valid_before=200,
    )
    assert other["message"]["nonce"] != nonce

    # EVM authorizations without a (valid) payer are refused outright.
    with pytest.raises(X402Error) as missing:
        handler.build_transfer_authorization(
            requirement,
            permit_id="permit-abc",
            wallet_id="wallet-abc",
            idempotency_key="idem-abc",
            valid_after=100,
            valid_before=200,
        )
    assert missing.value.reason == "x402_payer_required"
    with pytest.raises(X402Error) as malformed:
        handler.build_transfer_authorization(
            requirement,
            permit_id="permit-abc",
            wallet_id="wallet-abc",
            idempotency_key="idem-abc",
            payer="0xnothex",
            valid_after=100,
            valid_before=200,
        )
    assert malformed.value.reason == "x402_payer_invalid"


@pytest.mark.parametrize(
    ("network", "chain_id", "contract", "domain_name"),
    [
        # Base Sepolia's FiatToken test deployment initializes name() as
        # "USDC" (mainnet deployments use "USD Coin"); the name feeds the
        # contract's DOMAIN_SEPARATOR, so signing under the wrong one yields
        # signatures that can never verify on that chain.
        (
            "base-sepolia",
            84532,
            "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "USDC",
        ),
        ("ethereum", 1, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USD Coin"),
    ],
)
def test_x402_evm_usdc_domain_per_network(network, chain_id, contract, domain_name):
    handler = get_x402_handler()
    requirement = handler.parse_402(402, _x402_headers(amount="0.25", network=network))
    authorization = handler.build_transfer_authorization(
        requirement,
        permit_id="permit-net",
        wallet_id="wallet-net",
        idempotency_key="idem-net",
        payer=EVM_PAYER,
        valid_after=0,
        valid_before=10,
    )
    assert authorization["domain"]["chainId"] == chain_id
    assert authorization["domain"]["verifyingContract"] == contract
    assert authorization["domain"]["name"] == domain_name
    assert authorization["message"]["value"] == "250000"


def test_x402_parse_solana_happy_path_and_message_shape():
    handler = get_x402_handler()
    requirement = handler.parse_402(
        402,
        _x402_headers(amount="0.25", pay_to=SOLANA_PAY_TO, network="solana"),
    )
    assert requirement.network == "solana"

    authorization = handler.build_transfer_authorization(
        requirement,
        permit_id="permit-sol",
        wallet_id="wallet-sol",
        idempotency_key="idem-sol",
    )
    assert "types" not in authorization  # not EIP-712; structured message
    assert authorization["network"] == "solana"
    assert authorization["pay_to"] == SOLANA_PAY_TO
    assert authorization["amount"] == "250000"
    assert authorization["decimals"] == 6
    assert "permit-sol" in authorization["memo"]
    assert len(authorization["nonce"]) == 64
    # The validity window is part of the signed Solana message (defaults
    # when no window is supplied): a late signer must be able to see it.
    assert authorization["valid_after"] == "0"
    assert authorization["valid_before"] is None

    windowed = handler.build_transfer_authorization(
        requirement,
        permit_id="permit-sol",
        wallet_id="wallet-sol",
        idempotency_key="idem-sol",
        valid_after=100,
        valid_before=200,
    )
    assert windowed["valid_after"] == "100"
    assert windowed["valid_before"] == "200"
    # The window fields never perturb the deterministic nonce.
    assert windowed["nonce"] == authorization["nonce"]


def test_x402_parse_rejects_non_402_status():
    handler = get_x402_handler()
    with pytest.raises(X402Error) as exc:
        handler.parse_402(200, _x402_headers())
    assert exc.value.reason == "x402_not_payment_required"


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("X-402-Amount", "x402_amount_missing"),
        ("X-402-PayTo", "x402_pay_to_missing"),
        ("X-402-Network", "x402_network_missing"),
    ],
)
def test_x402_parse_rejects_missing_headers(missing, reason):
    handler = get_x402_handler()
    headers = _x402_headers()
    del headers[missing]
    with pytest.raises(X402Error) as exc:
        handler.parse_402(402, headers)
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("amount", "reason"),
    [
        ("-1", "x402_amount_not_positive"),
        ("0", "x402_amount_not_positive"),
        ("not-a-decimal", "x402_amount_invalid"),
        ("NaN", "x402_amount_invalid"),
        ("Infinity", "x402_amount_invalid"),
        ("0.1234567", "x402_amount_precision_exceeded"),  # > 6 dp (USDC)
        ("10000.01", "x402_amount_too_large"),
    ],
)
def test_x402_parse_rejects_malformed_amounts(amount, reason):
    handler = get_x402_handler()
    with pytest.raises(X402Error) as exc:
        handler.parse_402(402, _x402_headers(amount=amount))
    assert exc.value.reason == reason


def test_x402_parse_rejects_unknown_network():
    handler = get_x402_handler()
    with pytest.raises(X402Error) as exc:
        handler.parse_402(402, _x402_headers(network="polygon"))
    assert exc.value.reason == "x402_network_unsupported"


@pytest.mark.parametrize(
    ("pay_to", "network"),
    [
        (SOLANA_PAY_TO, "base"),  # base58 address on an EVM network
        ("0x1234", "base"),  # too short
        ("0x" + "g" * 40, "ethereum"),  # non-hex characters
        (EVM_PAY_TO, "solana"),  # 0x address on Solana
        ("A" * 31, "solana"),  # below base58 length floor
        ("A" * 45, "solana-devnet"),  # above base58 length ceiling
        ("O0Il" + "A" * 30, "solana"),  # excluded base58 characters
    ],
)
def test_x402_parse_rejects_wrong_shape_pay_to(pay_to, network):
    handler = get_x402_handler()
    with pytest.raises(X402Error) as exc:
        handler.parse_402(402, _x402_headers(pay_to=pay_to, network=network))
    assert exc.value.reason == "x402_pay_to_invalid"


@pytest.mark.anyio
async def test_x402_parse_endpoint_maps_reasons_and_requires_auth(client):
    ok = await client.post(
        "/v1/x402/parse",
        json={"status_code": 402, "headers": _x402_headers()},
        headers=BOOTSTRAP_HEADERS,
    )
    assert ok.status_code == 200
    assert ok.json() == {
        "amount_usd": "1.50",
        "pay_to": EVM_PAY_TO,
        "network": "base",
        "asset": "USDC",
    }

    bad = await client.post(
        "/v1/x402/parse",
        json={"status_code": 402, "headers": {"X-402-PayTo": EVM_PAY_TO}},
        headers=BOOTSTRAP_HEADERS,
    )
    assert bad.status_code == 400
    assert bad.json()["detail"] == "x402_amount_missing"

    unauthenticated = await client.post(
        "/v1/x402/parse",
        json={"status_code": 402, "headers": _x402_headers()},
    )
    assert unauthenticated.status_code == 401


# ---------------------------------------------------------------------------
# Settle endpoint negative paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_x402_settle_requires_idempotency_key(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id="permit-any",
            wallet_id=provisioned["agent_wallet_id"],
        ),
        headers=provisioned["agent_headers"],
    )
    assert resp.status_code == 422  # missing required Idempotency-Key header


@pytest.mark.anyio
async def test_x402_settle_foreign_wallet_denied(client, clean_database):
    """Tenant isolation: a key for wallet B cannot settle against wallet A."""
    victim = await provision_agent_wallet(client)
    attacker = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=victim["agent_wallet_id"],
        key_id=victim["key_id"],
        tool_name="x402.payment",
        max_credits=50,
        idem_key="x402-foreign-permit",
    )
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"],
            wallet_id=victim["agent_wallet_id"],
            amount="0.01",
        ),
        headers={**attacker["agent_headers"], "Idempotency-Key": "x402-foreign-1"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "wallet_access_denied"
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")


@pytest.mark.anyio
async def test_x402_settle_permit_wallet_mismatch(client, clean_database):
    """A caller settling its own wallet under another wallet's permit is
    denied on the permit's subject-wallet binding."""
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=owner["agent_wallet_id"],
        key_id=owner["key_id"],
        tool_name="x402.payment",
        max_credits=50,
        idem_key="x402-mismatch-permit",
    )
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"],
            wallet_id=other["agent_wallet_id"],
            amount="0.01",
        ),
        headers={**other["agent_headers"], "Idempotency-Key": "x402-mismatch-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "permit_wallet_mismatch"
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")
    _, total = await get_receipt_service().list_receipts(permit_id=permit["permit_id"])
    assert total == 0


@pytest.mark.anyio
async def test_x402_settle_unknown_permit_is_404(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id="permit-does-not-exist",
            wallet_id=provisioned["agent_wallet_id"],
            amount="0.01",
        ),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-missing-1"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "permit_not_found"


@pytest.mark.anyio
async def test_x402_settle_rejects_malformed_requirement(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    base = _settle_body(
        permit_id="permit-any",
        wallet_id=provisioned["agent_wallet_id"],
    )
    for override, reason in (
        ({"amount": "-5"}, "x402_amount_not_positive"),
        ({"amount": "0.1234567"}, "x402_amount_precision_exceeded"),
        ({"network": "dogecoin"}, "x402_network_unsupported"),
        ({"pay_to": "0xdeadbeef"}, "x402_pay_to_invalid"),
    ):
        resp = await client.post(
            "/v1/x402/settle",
            json={**base, **override},
            headers={
                **provisioned["agent_headers"],
                "Idempotency-Key": "x402-malformed-1",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == reason


@pytest.mark.anyio
async def test_x402_settle_conflicting_replay_is_409(client, clean_database):
    """Reusing an idempotency key with a different payload is refused, not
    silently settled twice (matching the POST /v1/permits semantics)."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-conflict-permit",
    )
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "x402-conflict-1"}
    first = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"], wallet_id=wallet_id, amount="0.01"
        ),
        headers=headers,
    )
    assert first.status_code == 200

    conflicting = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"], wallet_id=wallet_id, amount="0.02"
        ),
        headers=headers,
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"] == "idempotency_key_reused"
    # The conflicting attempt reserved nothing on top of the first settle.
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("10")


# ---------------------------------------------------------------------------
# SDK helpers
# ---------------------------------------------------------------------------


def test_sdk_parse_402_response_stub():
    requirement = parse_402_response(_StubResponse(402, _x402_headers()))
    assert requirement == {
        # "amount" is the legacy alias; "amount_usd" matches the server's
        # parse endpoint. Both carry the same value.
        "amount": "1.50",
        "amount_usd": "1.50",
        "pay_to": EVM_PAY_TO,
        "network": "base",
    }
    # Non-402 responses and 402s without the x402 header set are not demands.
    assert parse_402_response(_StubResponse(200, _x402_headers())) is None
    assert parse_402_response(_StubResponse(402, {"X-402-Amount": "1.00"})) is None


@pytest.mark.anyio
async def test_sdk_handle_402_settles_against_app(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=50,
        idem_key="x402-sdk-permit",
    )
    api_key = provisioned["agent_headers"]["X-API-Key"]
    sdk = X402Client(
        api_key=api_key,
        base_url="http://test",
        transport=ASGITransport(app=app),
    )
    try:
        assert (
            await sdk.handle_402(
                _StubResponse(200, {}),
                permit_id=permit["permit_id"],
                wallet_id=wallet_id,
                idempotency_key="x402-sdk-1",
            )
            is None
        )
        settlement = await sdk.handle_402(
            _StubResponse(402, _x402_headers(amount="0.01")),
            permit_id=permit["permit_id"],
            wallet_id=wallet_id,
            idempotency_key="x402-sdk-1",
            payer=EVM_PAYER,
        )
        assert settlement is not None
        assert settlement["receipt_id"].startswith("rcpt-")
        assert Decimal(settlement["credits"]) == Decimal("10")
        assert settlement["authorization"]["domain"]["chainId"] == 8453
    finally:
        await sdk.close()
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("10")


@pytest.mark.anyio
async def test_sdk_settle_surfaces_permit_denial(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=5,
        idem_key="x402-sdk-deny-permit",
    )
    sdk = X402Client(
        api_key=provisioned["agent_headers"]["X-API-Key"],
        base_url="http://test",
        transport=ASGITransport(app=app),
    )
    try:
        with pytest.raises(PermitDeniedError) as exc:
            await sdk.settle_402(
                permit_id=permit["permit_id"],
                wallet_id=wallet_id,
                requirement={
                    "amount": "0.02",  # 20 credits > 5-credit cap
                    "pay_to": EVM_PAY_TO,
                    "network": "base",
                },
                idempotency_key="x402-sdk-deny-1",
                payer=EVM_PAYER,
            )
        assert exc.value.reason == "permit_budget_exceeded"
    finally:
        await sdk.close()
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Settlement-integrity regressions (PR #363 review findings)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_x402_settle_rejects_unsupported_asset(client, clean_database):
    """The engine only knows USDC: any other asset string must be refused
    before it can produce evidence claiming one token while the typed data
    moves another."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-asset-permit",
    )
    body = _settle_body(permit_id=permit["permit_id"], wallet_id=wallet_id)
    body["asset"] = "DAI"
    resp = await client.post(
        "/v1/x402/settle",
        json=body,
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-asset-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "x402_asset_unsupported"
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")

    with pytest.raises(X402Error) as exc:
        get_x402_handler().build_requirement(
            amount="1.00", pay_to=EVM_PAY_TO, network="base", asset="DAI"
        )
    assert exc.value.reason == "x402_asset_unsupported"


@pytest.mark.anyio
async def test_x402_settle_requires_payer_for_evm(client, clean_database):
    """EVM settlements without a (valid) payer address are refused before any
    budget is reserved: the attestation must bind the real EIP-712 `from`."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-payer-permit",
    )
    for payer, reason in (
        (None, "x402_payer_required"),
        ("0xzz", "x402_payer_invalid"),
    ):
        resp = await client.post(
            "/v1/x402/settle",
            json=_settle_body(
                permit_id=permit["permit_id"], wallet_id=wallet_id, payer=payer
            ),
            headers={
                **provisioned["agent_headers"],
                "Idempotency-Key": f"x402-payer-{reason}",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == reason
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")


@pytest.mark.anyio
async def test_x402_failed_settlement_appends_compensating_audit_event(
    client, clean_database, monkeypatch
):
    """A settlement that fails after its success audit event was written must
    not let that event stand as the last word: a compensating failure event
    joins the chain, and the chain still verifies."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-comp-permit",
    )

    async def _induced_receipt_failure(self, **kwargs):
        raise ReceiptWriteContendedError("induced rolled-back receipt failure")

    monkeypatch.setattr(ReceiptService, "create_receipt", _induced_receipt_failure)
    failed = await client.post(
        "/v1/x402/settle",
        json=_settle_body(permit_id=permit["permit_id"], wallet_id=wallet_id),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-comp-1"},
    )
    assert failed.status_code == 400
    monkeypatch.undo()

    failures = await list_audit_events(
        event="x402.settlement_failed", wallet_id=wallet_id
    )
    assert len(failures) == 1
    assert failures[0].ok is False
    assert failures[0].error == "x402_settlement_failed"
    assert failures[0].metadata["permit_id"] == permit["permit_id"]

    chain = await client.post(
        "/v1/audit/verify-chain",
        json={"wallet_id": wallet_id},
        headers=BOOTSTRAP_HEADERS,
    )
    assert chain.status_code == 200
    assert chain.json()["valid"] is True


@pytest.mark.anyio
async def test_x402_max_calls_released_on_failed_settlement(
    client, clean_database, monkeypatch
):
    """A compensated failure gives back the max_calls_per_tool use it
    consumed: a one-call permit's retried key settles instead of dying on
    permit_max_calls_exceeded, and the cap still bites after the success."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": ["x402.payment"],
            "scopes": ["tool:x402.payment:invoke", "billing:charge"],
            "max_credits": 100,
            "max_calls_per_tool": {"x402.payment": 1},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "x402-maxcall-permit"},
    )
    assert resp.status_code == 201
    permit_id = resp.json()["permit_id"]
    body = _settle_body(permit_id=permit_id, wallet_id=wallet_id, amount="0.01")
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "x402-maxcall-1"}

    async def _induced_receipt_failure(self, **kwargs):
        raise ReceiptWriteContendedError("induced rolled-back receipt failure")

    monkeypatch.setattr(ReceiptService, "create_receipt", _induced_receipt_failure)
    failed = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert failed.status_code == 400
    monkeypatch.undo()

    # The retry must not be told the single allowed call was already used by
    # the compensated failure.
    ok = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert ok.status_code == 200
    assert await _permit_spent(client, permit_id) == Decimal("10")

    # The successful settlement legitimately consumed the one allowed call.
    second = await client.post(
        "/v1/x402/settle",
        json=_settle_body(permit_id=permit_id, wallet_id=wallet_id, amount="0.01"),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-maxcall-2"},
    )
    assert second.status_code == 400
    assert second.json()["detail"] == "permit_max_calls_exceeded"


@pytest.mark.anyio
async def test_release_tool_call_compensation_semantics(client, clean_database):
    """Direct coverage of PermitService.release_tool_call: decrements exactly
    one reserved use, clamps at zero, no-ops on missing permits, absent or
    malformed counters, and never loses a concurrent reservation's increment."""
    from sqlalchemy import select

    from app.db.database import get_session_factory
    from app.db.models import PermitModel
    from app.services.permits import get_permit_service

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": provisioned["key_id"],
            "allowed_tools": ["x402.payment"],
            "scopes": ["tool:x402.payment:invoke", "billing:charge"],
            "max_credits": 100,
            "max_calls_per_tool": {"x402.payment": 3},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "x402-release-permit"},
    )
    assert resp.status_code == 201
    permit_id = resp.json()["permit_id"]
    permits = get_permit_service()

    async def counter() -> object:
        factory = get_session_factory()
        async with factory() as session:
            model = (
                await session.execute(
                    select(PermitModel).where(PermitModel.permit_id == permit_id)
                )
            ).scalar_one()
            import json as _json

            counts = _json.loads(model.tool_call_counts_json or "{}")
            return counts.get("x402.payment")

    # Missing permit and absent counter are silent no-ops.
    await permits.release_tool_call("permit-does-not-exist", "x402.payment")
    await permits.release_tool_call(permit_id, "x402.payment")
    assert await counter() is None

    # Two concurrent-style reservations, one release: the surviving count is
    # exactly one — the release never clobbers the other reservation.
    for idx in range(2):
        validation = await permits.authorize_and_reserve(
            permit_id=permit_id,
            wallet_id=wallet_id,
            tool_name="x402.payment",
            estimated_credits=Decimal("1"),
            key_id=provisioned["key_id"],
        )
        assert validation.allowed, validation.reason
    assert await counter() == 2
    await permits.release_tool_call(permit_id, "x402.payment")
    assert await counter() == 1

    # Clamp at zero: releasing past the floor stops at 0, never negative.
    await permits.release_tool_call(permit_id, "x402.payment")
    assert await counter() == 0
    await permits.release_tool_call(permit_id, "x402.payment")
    assert await counter() == 0

    # A malformed boolean counter is rejected, not coerced and decremented.
    factory = get_session_factory()
    async with factory() as session:
        model = (
            await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit_id)
            )
        ).scalar_one()
        import json as _json

        model.tool_call_counts_json = _json.dumps({"x402.payment": True})
        session.add(model)
        await session.commit()
    await permits.release_tool_call(permit_id, "x402.payment")
    assert await counter() is True


# ---------------------------------------------------------------------------
# Stale idempotency-record recovery and the settlement freeze
# ---------------------------------------------------------------------------


async def _backdate_settle_record(idempotency_key: str, *, wallet_id: str) -> None:
    """Age one wallet's settle record past the router's staleness threshold.

    Constrained by the full idempotency identity (wallet, endpoint, key), the
    same discipline as the ACP bridge's test helper."""
    from sqlalchemy import select

    from app.core.time import utc_now
    from app.db.database import get_session_factory
    from app.db.models import IdempotencyRecordModel
    from app.routers.x402 import _SETTLE_ENDPOINT, _SETTLE_STALE_SECONDS

    factory = get_session_factory()
    async with factory() as session:
        record = (
            await session.execute(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.wallet_id == wallet_id,
                    IdempotencyRecordModel.endpoint == _SETTLE_ENDPOINT,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()
        assert record.response_json is None  # crashed before completion
        record.created_at = utc_now() - timedelta(seconds=_SETTLE_STALE_SECONDS + 1)
        session.add(record)
        await session.commit()


@pytest.mark.anyio
async def test_x402_stale_receiptless_record_retains_owner_for_review(
    client, clean_database
):
    """No receipt cannot prove whether the attempt reserved permit authority.

    Retain even an empty stale owner: it has no durable reservation checkpoint
    distinguishing it from a crash after reservation. Fresh owners still get
    the ordinary in-progress conflict.
    """
    from app.routers.x402 import _SETTLE_ENDPOINT, X402SettleRequest
    from app.services.idempotency import get_idempotency_service

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-recover-permit",
    )
    body = _settle_body(
        permit_id=permit["permit_id"], wallet_id=wallet_id, amount="0.03"
    )
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "x402-recover-1"}

    # Simulate the crashed attempt: its record was begun with exactly the
    # payload the route hashes, and the process died before completing it.
    idem = get_idempotency_service()
    begun = await idem.begin_with_record(
        wallet_id=wallet_id,
        endpoint=_SETTLE_ENDPOINT,
        idempotency_key="x402-recover-1",
        request_payload=X402SettleRequest(**body).model_dump(mode="json"),
    )
    assert begun.replay is None

    # Fresh in-progress record: still refused — it could be a live attempt.
    blocked = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "idempotency_in_progress"

    await _backdate_settle_record("x402-recover-1", wallet_id=wallet_id)

    for _ in range(2):
        blocked = await client.post("/v1/x402/settle", json=body, headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "x402_settlement_needs_review"
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("0")
    _, total = await get_receipt_service().list_receipts(permit_id=permit["permit_id"])
    assert total == 0
    retained = await idem.get_record(
        wallet_id=wallet_id,
        endpoint=_SETTLE_ENDPOINT,
        idempotency_key="x402-recover-1",
    )
    assert retained.record_id == begun.record_id
    assert retained.response_json is None


@pytest.mark.anyio
async def test_x402_crash_after_reservation_refuses_overlapping_stale_retries(
    client, clean_database, monkeypatch
):
    from app.routers.x402 import _SETTLE_ENDPOINT
    from app.services.idempotency import get_idempotency_service

    class SimulatedProcessDeath(BaseException):
        pass

    owner = await provision_agent_wallet(client)
    wallet_id = owner["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=owner["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="crash-permit",
    )
    body = _settle_body(permit_id=permit["permit_id"], wallet_id=wallet_id)
    headers = {**owner["agent_headers"], "Idempotency-Key": "crash-settle"}
    handler = get_x402_handler()

    def die_after_committed_reservation(*args, **kwargs):
        raise SimulatedProcessDeath()

    with monkeypatch.context() as context:
        context.setattr(
            handler, "build_transfer_authorization", die_after_committed_reservation
        )
        with pytest.raises(BaseExceptionGroup) as crash:
            await client.post("/v1/x402/settle", json=body, headers=headers)
        assert crash.value.subgroup(SimulatedProcessDeath) is not None
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    idem = get_idempotency_service()
    original = await idem.get_record(
        wallet_id=wallet_id, endpoint=_SETTLE_ENDPOINT, idempotency_key="crash-settle"
    )
    await _backdate_settle_record("crash-settle", wallet_id=wallet_id)

    retries = await asyncio.gather(
        *(client.post("/v1/x402/settle", json=body, headers=headers) for _ in range(2))
    )
    for retry in retries:
        assert retry.status_code == 409
        assert retry.json()["detail"] == "x402_settlement_needs_review"
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    _, total = await get_receipt_service().list_receipts(permit_id=permit["permit_id"])
    assert total == 0
    retained = await idem.get_record(
        wallet_id=wallet_id, endpoint=_SETTLE_ENDPOINT, idempotency_key="crash-settle"
    )
    assert retained.record_id == original.record_id
    assert retained.response_json is None


@pytest.mark.anyio
async def test_x402_live_worker_crossing_stale_threshold_keeps_its_owner(
    client, clean_database, monkeypatch
):
    from app.services.signing_keys import get_signing_key_service

    owner = await provision_agent_wallet(client)
    wallet_id = owner["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=owner["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="live-permit",
    )
    body = _settle_body(permit_id=permit["permit_id"], wallet_id=wallet_id)
    headers = {**owner["agent_headers"], "Idempotency-Key": "live-settle"}
    reserved, finish = asyncio.Event(), asyncio.Event()
    signing = get_signing_key_service()
    original_sign = signing.sign_payload

    async def pause_after_reservation(payload):
        if "authorization" in payload and not reserved.is_set():
            reserved.set()
            await finish.wait()
        return await original_sign(payload)

    monkeypatch.setattr(signing, "sign_payload", pause_after_reservation)
    live = asyncio.create_task(
        client.post("/v1/x402/settle", json=body, headers=headers)
    )
    try:
        await asyncio.wait_for(reserved.wait(), timeout=5)
        await _backdate_settle_record("live-settle", wallet_id=wallet_id)
        retry = await client.post("/v1/x402/settle", json=body, headers=headers)
        assert retry.status_code == 409
        assert retry.json()["detail"] == "x402_settlement_needs_review"
        assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    finally:
        finish.set()
        result = await asyncio.wait_for(live, timeout=5)
    assert result.status_code == 200, result.text
    replay = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == result.json()
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    _, total = await get_receipt_service().list_receipts(permit_id=permit["permit_id"])
    assert total == 1


@pytest.mark.anyio
async def test_x402_stale_receipted_record_is_settled_unrecoverable(
    client, clean_database, monkeypatch
):
    """Crash shape 2: death between the receipt write and idem.complete. The
    settlement is durable (budget consumed, verifiable receipt) but the
    verbatim X402SettleResponse is NOT reconstructable from persisted state:
    receipts keep only request/response payload hashes, and the response's
    attestation signature and shadow-ledger ids live nowhere that can be
    proven byte-identical. The recovery path must not guess — once stale,
    replays of the key get the DISTINCT typed conflict
    x402_settled_unrecoverable_replay (never a fabricated 200, never an
    eternal idempotency_in_progress), and nothing re-executes."""
    from app.services.idempotency import IdempotencyService

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-wedge-permit",
    )
    body = _settle_body(
        permit_id=permit["permit_id"], wallet_id=wallet_id, amount="0.03"
    )
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "x402-wedge-1"}

    original_complete = IdempotencyService.complete

    async def _crash_complete(self, **complete_kwargs):
        raise RuntimeError("induced crash before idem.complete")

    monkeypatch.setattr(IdempotencyService, "complete", _crash_complete)
    with pytest.raises(RuntimeError):
        await client.post("/v1/x402/settle", json=body, headers=headers)
    monkeypatch.setattr(IdempotencyService, "complete", original_complete)

    # The settlement itself is durable: budget reserved, one receipt exists.
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    receipts, total = await get_receipt_service().list_receipts(
        permit_id=permit["permit_id"]
    )
    assert total == 1

    # Fresh record: the generic in-progress conflict, as ever.
    blocked = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "idempotency_in_progress"

    await _backdate_settle_record("x402-wedge-1", wallet_id=wallet_id)

    # Stale + receipted: the distinct typed contract. The caller learns the
    # settlement DID happen and can fetch the verifiable receipt; the exact
    # original response body is the one thing that cannot be replayed.
    conflicted = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert conflicted.status_code == 409
    assert conflicted.json()["detail"] == "x402_settled_unrecoverable_replay"

    # Nothing re-ran: no double reservation, no second receipt.
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    receipts_after, total_after = await get_receipt_service().list_receipts(
        permit_id=permit["permit_id"]
    )
    assert total_after == 1
    assert receipts_after[0].receipt_id == receipts[0].receipt_id


@pytest.mark.anyio
async def test_x402_settle_never_touches_real_ledger_entries(client, clean_database):
    """The module docstring's settlement-freeze claim, asserted: a successful
    settle writes NO real ledger entries — the ledger_entries row count is
    unchanged across the settle and the receipt's ledger_entry_id is None
    (shadow-ledger metering plus a signed receipt only)."""
    from sqlalchemy import func, select

    from app.db.database import get_session_factory
    from app.db.models import LedgerEntryModel

    async def _ledger_rows() -> int:
        factory = get_session_factory()
        async with factory() as session:
            return (
                await session.execute(
                    select(func.count()).select_from(LedgerEntryModel)
                )
            ).scalar_one()

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="x402.payment",
        max_credits=100,
        idem_key="x402-freeze-permit",
    )
    rows_before = await _ledger_rows()
    # Provisioning itself wrote real entries (sponsor mint, agent funding),
    # so the counter is provably live before the claim is tested.
    assert rows_before > 0

    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"], wallet_id=wallet_id, amount="0.03"
        ),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "x402-freeze-1"},
    )
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["credits"]) == Decimal("30")

    assert await _ledger_rows() == rows_before
    receipts, total = await get_receipt_service().list_receipts(
        permit_id=permit["permit_id"]
    )
    assert total == 1
    assert receipts[0].ledger_entry_id is None


async def _one_call_x402_permit(client, owner, idem_key):
    wallet_id = owner["agent_wallet_id"]
    response = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": owner["key_id"],
            "allowed_tools": ["x402.payment"],
            "max_credits": 100,
            "max_calls_per_tool": {"x402.payment": 1},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert response.status_code == 201
    return response.json()


async def _x402_call_count(permit_id):
    import json
    from app.db.database import get_session_factory
    from app.db.models import PermitModel

    async with get_session_factory()() as session:
        row = await session.get(PermitModel, permit_id)
        return json.loads(row.tool_call_counts_json)["x402.payment"]


@pytest.mark.anyio
@pytest.mark.parametrize("release", ["release_budget", "release_tool_call"])
async def test_x402_incomplete_compensation_preserves_original_owner(
    client, clean_database, monkeypatch, release
):
    from app.services.idempotency import get_idempotency_service
    from app.services.permits import get_permit_service

    owner = await provision_agent_wallet(client)
    wallet_id = owner["agent_wallet_id"]
    permit = await _one_call_x402_permit(client, owner, "release-failure-permit")
    body = _settle_body(permit_id=permit["permit_id"], wallet_id=wallet_id)
    headers = {**owner["agent_headers"], "Idempotency-Key": "release-failure"}

    async def receipt_failure(*args, **kwargs):
        raise ReceiptWriteContendedError("known rolled-back receipt write")

    async def release_failure(*args, **kwargs):
        raise RuntimeError("synthetic compensation failure")

    with monkeypatch.context() as patch:
        patch.setattr(ReceiptService, "create_receipt", receipt_failure)
        patch.setattr(get_permit_service(), release, release_failure)
        first = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert first.status_code == 409
    assert first.json()["detail"] == "x402_settlement_needs_review"
    expected_spent = Decimal("30") if release == "release_budget" else Decimal("0")
    expected_calls = 1 if release == "release_tool_call" else 0
    assert await _permit_spent(client, permit["permit_id"]) == expected_spent
    assert await _x402_call_count(permit["permit_id"]) == expected_calls
    idem = get_idempotency_service()
    original = await idem.get_record(
        wallet_id=wallet_id,
        endpoint="/v1/x402/settle",
        idempotency_key="release-failure",
    )
    assert original is not None and original.response_json is None
    for stale in (False, True):
        if stale:
            await _backdate_settle_record("release-failure", wallet_id=wallet_id)
        retry = await client.post("/v1/x402/settle", json=body, headers=headers)
        assert retry.status_code == 409
        assert await _permit_spent(client, permit["permit_id"]) == expected_spent
        assert await _x402_call_count(permit["permit_id"]) == expected_calls
    retained = await idem.get_record(
        wallet_id=wallet_id,
        endpoint="/v1/x402/settle",
        idempotency_key="release-failure",
    )
    assert retained.record_id == original.record_id
    _, total = await get_receipt_service().list_receipts(permit_id=permit["permit_id"])
    assert total == 0
    events = await list_audit_events(
        event="x402.settlement_failed", wallet_id=wallet_id
    )
    assert len(events) == 1
    assert events[0].metadata["compensation_complete"] is False
    assert events[0].metadata["credits_released"] == (
        None if release == "release_budget" else "30.000"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "receipt_committed,lookup_fails", [(False, False), (True, False), (True, True)]
)
async def test_x402_uncertain_receipt_write_never_releases_authority_or_owner(
    client, clean_database, monkeypatch, receipt_committed, lookup_fails
):
    from app.services.idempotency import get_idempotency_service
    from app.services.permits import get_permit_service

    owner = await provision_agent_wallet(client)
    wallet_id = owner["agent_wallet_id"]
    permit = await _one_call_x402_permit(client, owner, "uncertain-receipt-permit")
    body = _settle_body(permit_id=permit["permit_id"], wallet_id=wallet_id)
    headers = {**owner["agent_headers"], "Idempotency-Key": "uncertain-receipt"}
    original_create = ReceiptService.create_receipt

    async def uncertain_receipt(self, **kwargs):
        if receipt_committed:
            await original_create(self, **kwargs)
        raise RuntimeError("synthetic receipt acknowledgement lost")

    async def no_release(*args, **kwargs):
        pytest.fail("uncertain receipt writes must retain budget and call authority")

    async def failed_lookup(*args, **kwargs):
        raise RuntimeError("synthetic receipt lookup outage")

    with monkeypatch.context() as patch:
        patch.setattr(ReceiptService, "create_receipt", uncertain_receipt)
        patch.setattr(get_permit_service(), "release_budget", no_release)
        patch.setattr(get_permit_service(), "release_tool_call", no_release)
        if lookup_fails:
            patch.setattr(
                ReceiptService, "get_receipt_by_idempotency_record_id", failed_lookup
            )
        first = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert first.status_code == 409
    assert first.json()["detail"] == (
        "x402_settled_unrecoverable_replay"
        if receipt_committed and not lookup_fails
        else "x402_settlement_needs_review"
    )
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    idem = get_idempotency_service()
    original = await idem.get_record(
        wallet_id=wallet_id,
        endpoint="/v1/x402/settle",
        idempotency_key="uncertain-receipt",
    )
    assert original is not None and original.response_json is None
    await _backdate_settle_record("uncertain-receipt", wallet_id=wallet_id)
    retry = await client.post("/v1/x402/settle", json=body, headers=headers)
    assert retry.status_code == 409
    assert retry.json()["detail"] == (
        "x402_settled_unrecoverable_replay"
        if receipt_committed
        else "x402_settlement_needs_review"
    )
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")
    assert await _x402_call_count(permit["permit_id"]) == 1
    receipts, total = await get_receipt_service().list_receipts(
        permit_id=permit["permit_id"]
    )
    assert total == int(receipt_committed)
    if receipts:
        valid, reason, _ = await get_receipt_service().verify_receipt(
            receipts[0].receipt_id
        )
        assert valid, reason
    # No false compensation event claims released authority for a durable receipt.
    assert (
        await list_audit_events(event="x402.settlement_failed", wallet_id=wallet_id)
        == []
    )
    retained = await idem.get_record(
        wallet_id=wallet_id,
        endpoint="/v1/x402/settle",
        idempotency_key="uncertain-receipt",
    )
    assert retained.record_id == original.record_id


# ---------------------------------------------------------------------------
# Permit recipient binding on the x402 path
# ---------------------------------------------------------------------------

OTHER_EVM_PAY_TO = "0x3333333333333333333333333333333333333333"


async def _create_recipient_permit(
    client,
    *,
    wallet_id: str,
    key_id: str | None,
    recipient: str,
    idem_key: str,
) -> dict:
    """Mint an x402.payment permit bound to one recipient address."""
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": ["x402.payment"],
            "scopes": ["tool:x402.payment:invoke", "billing:charge"],
            "max_credits": 100,
            "recipient_domain": recipient,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.anyio
async def test_x402_settle_recipient_match_allowed(client, clean_database):
    """A demand paying the permit's bound address settles normally."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await _create_recipient_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        recipient=EVM_PAY_TO,
        idem_key="x402-recipient-allow-permit",
    )
    resp = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit["permit_id"],
            wallet_id=wallet_id,
            amount="0.03",
            pay_to=EVM_PAY_TO,
        ),
        headers={
            **provisioned["agent_headers"],
            "Idempotency-Key": "x402-recipient-allow-1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["receipt_id"].startswith("rcpt-")
    assert await _permit_spent(client, permit["permit_id"]) == Decimal("30")


@pytest.mark.anyio
async def test_x402_settle_recipient_mismatch_denied_no_reserve(client, clean_database):
    """A demand paying any other address is denied before budget moves.

    No reservation is held, no receipt exists, and the refused idempotency
    key stays reusable for a corrected demand.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    permit = await _create_recipient_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        recipient=EVM_PAY_TO,
        idem_key="x402-recipient-deny-permit",
    )
    permit_id = permit["permit_id"]
    headers = {
        **provisioned["agent_headers"],
        "Idempotency-Key": "x402-recipient-deny-1",
    }

    denied = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit_id,
            wallet_id=wallet_id,
            amount="0.03",
            pay_to=OTHER_EVM_PAY_TO,
        ),
        headers=headers,
    )
    assert denied.status_code == 400
    assert denied.json()["detail"] == "permit_recipient_domain_mismatch"
    assert await _permit_spent(client, permit_id) == Decimal("0")
    _, total = await get_receipt_service().list_receipts(permit_id=permit_id)
    assert total == 0

    # The refused key was abandoned, so the corrected demand reuses it.
    ok = await client.post(
        "/v1/x402/settle",
        json=_settle_body(
            permit_id=permit_id,
            wallet_id=wallet_id,
            amount="0.03",
            pay_to=EVM_PAY_TO,
        ),
        headers=headers,
    )
    assert ok.status_code == 200
    assert await _permit_spent(client, permit_id) == Decimal("30")


def test_x402_settle_requires_idempotency_record_id():
    """Settle without a record id is a type error: no caller can skip dedupe.

    The permit id does not exist, so without the required argument the old
    code would fall through to permit_not_found instead of raising here.
    """
    handler = get_x402_handler()
    requirement = handler.build_requirement(
        amount="0.01", pay_to=EVM_PAY_TO, network="base"
    )
    with pytest.raises(TypeError):
        handler.settle(
            permit_id="permit-does-not-exist",
            wallet_id="wallet-does-not-exist",
            key_id=None,
            requirement=requirement,
            idempotency_key="x402-no-record-1",
            payer=EVM_PAYER,
        )


def test_recipient_binding_matches_addresses_and_hosts():
    """The shared comparison the MCP and x402 paths both enforce."""
    from app.services.permits import recipient_binding_matches

    assert recipient_binding_matches(None, EVM_PAY_TO) is True
    assert recipient_binding_matches(EVM_PAY_TO, EVM_PAY_TO) is True
    assert recipient_binding_matches(EVM_PAY_TO, OTHER_EVM_PAY_TO) is False
    assert (
        recipient_binding_matches(
            "partner.example", "https://partner.example/tools/call"
        )
        is True
    )
    assert (
        recipient_binding_matches("allowed.example.com", "https://other.example")
        is False
    )
