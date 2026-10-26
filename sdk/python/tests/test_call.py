"""The blocking call surface and portable receipt verification."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import httpx
import pytest

from b2a_sdk import Client, Receipt, VerificationStatus

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "test-vectors"
CASES = {
    case["name"]: case["bundle"]
    for case in json.loads((VECTORS / "receipts.json").read_text())["cases"]
}
KEYS = json.loads((VECTORS / "keys.json").read_text())
VALID_BUNDLE = CASES["success_action_v1.valid"]
SIGNED_CLAIMS = json.loads(VALID_BUNDLE["signing_input"])


def _receipt_payload() -> dict:
    return {
        **SIGNED_CLAIMS,
        "signature": VALID_BUNDLE["signature"],
        "signature_key_id": VALID_BUNDLE["kid"],
    }


def _mcp_response(key: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": key,
            "result": {
                "content": [{"type": "text", "text": '{"ok": true}'}],
                "isError": False,
                "receipt": _receipt_payload(),
            },
        },
    )


def _evidence_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == f"/v1/receipts/{SIGNED_CLAIMS['receipt_id']}/portable":
        return httpx.Response(200, json=VALID_BUNDLE)
    if request.url.path == "/.well-known/trust-keys.json":
        return httpx.Response(200, json=KEYS)
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


@pytest.mark.parametrize("sdk_name", ["b2a_sdk", "amw"])
def test_explicit_key_replay_returns_the_same_receipt(sdk_name: str) -> None:
    sdk = import_module(sdk_name)
    calls: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/mcp/messages":
            return _evidence_response(request)
        body = json.loads(request.content)
        calls.append((request.headers["idempotency-key"], body))
        return _mcp_response(request.headers["idempotency-key"])

    with sdk.Client(
        "YOUR_DEMO_KEY", base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        first = client.call(
            SIGNED_CLAIMS["tool"],
            {"message": "hi"},
            key="order-123",
            wallet_id=SIGNED_CLAIMS["wallet_id"],
            permit_id=SIGNED_CLAIMS["permit_id"],
        )
        second = client.call(
            SIGNED_CLAIMS["tool"],
            {"message": "hi"},
            key="order-123",
            wallet_id=SIGNED_CLAIMS["wallet_id"],
            permit_id=SIGNED_CLAIMS["permit_id"],
        )
        assert first.verify().status is sdk.VerificationStatus.VERIFIED

    assert isinstance(first, sdk.Receipt)
    assert first.id == second.id == SIGNED_CLAIMS["receipt_id"]
    assert first.request_key == second.request_key == client.last_request_key == "order-123"
    assert first.raw["receipt_id"] == first.id
    assert [key for key, _ in calls] == ["order-123", "order-123"]
    for _, body in calls:
        assert body["method"] == "tools/call"
        assert body["params"]["mcpContext"] == {
            "wallet_id": SIGNED_CLAIMS["wallet_id"],
            "permit_id": SIGNED_CLAIMS["permit_id"],
            "idempotency_key": "order-123",
        }


@pytest.mark.parametrize("sdk_name", ["b2a_sdk", "amw"])
def test_omitted_key_uses_request_key_and_exposes_it(sdk_name: str) -> None:
    sdk = import_module(sdk_name)
    args = {"message": "hi"}
    expected_key = sdk.request_key(SIGNED_CLAIMS["tool"], args)
    seen_keys: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/mcp/messages":
            return _evidence_response(request)
        key = request.headers["idempotency-key"]
        seen_keys.append(key)
        assert json.loads(request.content)["params"]["mcpContext"]["idempotency_key"] == key
        return _mcp_response(key)

    with sdk.Client(
        "YOUR_DEMO_KEY",
        base_url="http://test",
        wallet_id=SIGNED_CLAIMS["wallet_id"],
        permit_id=SIGNED_CLAIMS["permit_id"],
        transport=httpx.MockTransport(handler),
    ) as client:
        receipt = client.call(
            SIGNED_CLAIMS["tool"],
            args,
        )
        assert client.last_request_key == receipt.request_key == expected_key

    assert seen_keys == [expected_key]


@pytest.mark.parametrize(
    ("wallet_id", "permit_id"),
    [(None, None), (SIGNED_CLAIMS["wallet_id"], None), (None, SIGNED_CLAIMS["permit_id"])],
)
def test_missing_call_context_fails_before_transport(
    wallet_id: str | None, permit_id: str | None
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("the gateway should not be called")

    with Client(
        "YOUR_DEMO_KEY", base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError):
            client.call(
                SIGNED_CLAIMS["tool"],
                {"message": "hi"},
                key="order-123",
                wallet_id=wallet_id,
                permit_id=permit_id,
            )

    assert requests == []


@pytest.mark.parametrize(
    ("case_name", "status"),
    [
        ("success_action_v1.valid", VerificationStatus.VERIFIED),
        ("success_action_v1.tampered_payload", VerificationStatus.INVALID),
    ],
)
def test_verify_uses_published_receipt_vectors(case_name: str, status: VerificationStatus) -> None:
    pytest.importorskip("cryptography")
    receipt = Receipt.from_dict(_receipt_payload())

    result = receipt.verify(bundle=CASES[case_name], keys=KEYS)

    assert result.status is status
    assert result.ok is (status is VerificationStatus.VERIFIED)


def test_verify_rejects_valid_evidence_for_a_different_receipt() -> None:
    pytest.importorskip("cryptography")
    receipt = Receipt.from_dict(_receipt_payload())

    result = receipt.verify(bundle=CASES["success_approval.valid"], keys=KEYS)

    assert result.status is VerificationStatus.MISMATCH


def test_verify_rejects_immediate_receipt_outcome_that_disagrees_with_signature() -> None:
    pytest.importorskip("cryptography")
    raw = _receipt_payload()
    raw["outcome"] = "denied"
    receipt = Receipt.from_dict(raw)

    result = receipt.verify(bundle=VALID_BUNDLE, keys=KEYS)

    assert result.status is VerificationStatus.MISMATCH


def test_verify_accepts_legacy_signature_with_unsigned_backfilled_linkage() -> None:
    pytest.importorskip("cryptography")
    bundle = CASES["success_legacy.valid"]
    raw = {
        **json.loads(bundle["signing_input"]),
        "signature": bundle["signature"],
        "signature_key_id": bundle["kid"],
        "idempotency_record_id": "idem-backfilled",
    }
    receipt = Receipt.from_dict(raw)

    result = receipt.verify(bundle=bundle, keys=KEYS)

    assert result.status is VerificationStatus.VERIFIED
    assert "idempotency_record_id" not in result.claims


def test_verify_returns_result_for_malformed_key_document() -> None:
    receipt = Receipt.from_dict(_receipt_payload())

    result = receipt.verify(bundle=VALID_BUNDLE, keys={})

    assert result.status is VerificationStatus.MALFORMED


@pytest.mark.parametrize(
    "failing_path",
    [
        f"/v1/receipts/{SIGNED_CLAIMS['receipt_id']}/portable",
        "/.well-known/trust-keys.json",
    ],
)
def test_evidence_fetch_failure_preserves_call_receipt(failing_path: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp/messages":
            return _mcp_response(request.headers["idempotency-key"])
        if request.url.path == failing_path:
            return httpx.Response(503, json={"detail": "unavailable"})
        return _evidence_response(request)

    with Client(
        "YOUR_DEMO_KEY", base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        receipt = client.call(
            SIGNED_CLAIMS["tool"],
            {"message": "hi"},
            key="order-123",
            wallet_id=SIGNED_CLAIMS["wallet_id"],
            permit_id=SIGNED_CLAIMS["permit_id"],
        )

    assert receipt.id == SIGNED_CLAIMS["receipt_id"]
    assert receipt.request_key == client.last_request_key == "order-123"
    assert receipt.raw["signature"] == VALID_BUNDLE["signature"]
    expected_status = (
        VerificationStatus.MALFORMED
        if failing_path.endswith("/portable")
        else VerificationStatus.UNKNOWN_KEY
    )
    assert receipt.verify().status is expected_status
