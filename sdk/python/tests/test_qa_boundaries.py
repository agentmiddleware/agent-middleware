"""Focused pre-I/O and billing boundaries for the Python SDK."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from b2a_sdk.client import AgentMiddlewareClient
from b2a_sdk.errors import APIError, InsufficientFundsError
from b2a_sdk.models import ACPCheckoutRequest, ACPLineItem


def _client(handler) -> AgentMiddlewareClient:
    return AgentMiddlewareClient(
        api_key="test-key",
        base_url="https://gateway.example",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("method", ["get_receipt", "verify_receipt", "get_evidence", "get_wallet"])
@pytest.mark.parametrize("unsafe_id", ["../admin", "wallet/../admin", "a%2Fb", "a?b", " "])
@pytest.mark.asyncio
async def test_unsafe_identifier_is_rejected_before_io(method: str, unsafe_id: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    async with _client(handler) as client:
        with pytest.raises(ValueError, match="safe path segment"):
            await getattr(client, method)(unsafe_id)
    assert seen == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", " "), ("wallet_id", " "), ("permit_id", " ")],
)
@pytest.mark.asyncio
async def test_blank_invoke_binding_is_rejected_before_io(field: str, value: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    arguments = {
        "name": "partner.search",
        "wallet_id": "wallet-1",
        "permit_id": "permit-1",
        "idempotency_key": "invoke-1",
    }
    arguments[field] = value
    async with _client(handler) as client:
        with pytest.raises(ValueError, match="must not be blank"):
            await client.invoke_tool(arguments.pop("name"), {}, **arguments)
    assert seen == []


@pytest.mark.asyncio
async def test_unsafe_dry_run_id_sends_no_delete_and_preserves_body_error() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/billing/dry-run/session":
            return httpx.Response(200, json={"session_id": "../admin"})
        raise AssertionError("unsafe cleanup sent a request")

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="original failure"):
            async with client.simulate_session("wallet-1"):
                raise RuntimeError("original failure")
    assert seen == [("POST", "/v1/billing/dry-run/session")]


@pytest.mark.parametrize(
    "body,detail",
    [(b"not json", "invalid_json_response"), (b"[]", "invalid_object_response")],
)
@pytest.mark.asyncio
async def test_malformed_charge_402_keeps_api_error(body: bytes, detail: str) -> None:
    async with _client(lambda _: httpx.Response(402, content=body)) as client:
        with pytest.raises(APIError, match=detail) as exc_info:
            await client.charge("wallet-1", "iot_bridge", idempotency_key="charge-1")
    assert type(exc_info.value) is APIError
    assert exc_info.value.status_code == 402


@pytest.mark.asyncio
async def test_exact_shortfall_and_relative_top_up_survive_verbatim() -> None:
    body = {
        "detail": {
            "shortfall": 0.30000000000000004,
            "shortfall_exact": "0.30",
            "top_up_url": "/v1/billing/top-up/prepare",
        }
    }
    async with _client(lambda _: httpx.Response(402, json=body)) as client:
        with pytest.raises(InsufficientFundsError) as exc_info:
            await client.charge("wallet-1", "iot_bridge", idempotency_key="charge-1")
    error = exc_info.value
    assert error.shortfall == 0.3
    assert error.shortfall_exact == "0.30"
    assert error.top_up_url == "/v1/billing/top-up/prepare"
    assert "Shortfall: 0.30 credits" in str(error)


@pytest.mark.parametrize("link", ["https://[bad/top", "https://example.com:bad/top"])
@pytest.mark.asyncio
async def test_malformed_top_up_link_does_not_mask_funds_error(link: str) -> None:
    body = {"detail": {"shortfall": "2", "top_up_url": link}}
    async with _client(lambda _: httpx.Response(402, json=body)) as client:
        with pytest.raises(InsufficientFundsError) as exc_info:
            await client.charge("wallet-1", "iot_bridge", idempotency_key="charge-1")
    assert exc_info.value.shortfall == 2.0
    assert exc_info.value.top_up_url is None


def test_huge_finite_exact_shortfall_never_exposes_infinite_float() -> None:
    error = InsufficientFundsError("wallet-1", shortfall="0.5", shortfall_exact="1e9999")
    assert error.shortfall is None
    assert error.shortfall_exact == "1e9999"
    assert "Shortfall: 1e9999 credits" in str(error)


def _checkout() -> ACPCheckoutRequest:
    return ACPCheckoutRequest(
        intent_id="intent-1",
        line_items=[ACPLineItem("Widget", 2, 2125, "usd", sku="")],
        spt_token="spt_secret",
        merchant_domain="example.com",
        client_total=4250,
    )


def test_checkout_validates_exact_total_and_retains_empty_sku() -> None:
    request = _checkout()
    assert request.to_payload()["line_items"][0]["sku"] == ""
    assert "spt_secret" not in repr(request)
    with pytest.raises(ValueError, match="acp_total_mismatch"):
        ACPCheckoutRequest(
            intent_id="intent-1",
            line_items=request.line_items,
            spt_token="spt_secret",
            merchant_domain="example.com",
            client_total=4249,
        )


@pytest.mark.parametrize("mutation", ["total", "quantity", "currency"])
def test_checkout_revalidates_nested_mutations_before_payload(mutation: str) -> None:
    request = _checkout()
    if mutation == "total":
        request.client_total = 1
    elif mutation == "quantity":
        request.line_items[0].quantity = 3
    else:
        request.line_items[0].currency = "eur"
    with pytest.raises(ValueError):
        request.to_payload()


def test_non_finite_shortfall_is_unknown() -> None:
    error = InsufficientFundsError("wallet-1", shortfall=Decimal("NaN"))
    assert error.shortfall is None
    assert error.shortfall_exact is None
