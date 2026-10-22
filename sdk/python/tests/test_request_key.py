"""Request key derivation and SDK forwarding contract."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import httpx
import pytest

from b2a_sdk import AgentMiddlewareClient, IdempotencyConflictError, request_key


def test_request_key_is_stable_and_explains_dropped_fields() -> None:
    first = {"order_id": "A1", "amount": "12.00", "requested_at": "one", "note": "first"}
    second = {"note": "rewritten", "requested_at": "two", "amount": "12.00", "order_id": "A1"}
    with pytest.warns(UserWarning, match="unstable_field"):
        info = request_key.explain("partner.refund", first, business_id="A1")
    with pytest.warns(UserWarning, match="unstable_field"):
        retry_key = request_key("partner.refund", second, business_id="A1")
    assert info.key == retry_key
    assert info.key.startswith("amwk1_")
    assert len(info.key) == 58
    assert info.used_fields == ("amount", "order_id")
    assert info.dropped_fields == ("note", "requested_at")
    assert request_key("partner.refund", {"order_id": "A1", "amount": "13.00"}) != request_key(
        "partner.refund", {"order_id": "A1", "amount": "12.00"}
    )


def test_request_key_nested_paths_and_include_override() -> None:
    args = {"payment": {"amount": "12.00", "timestamp": "today"}, "trace_id": "trace"}
    with pytest.warns(UserWarning, match="unstable_field"):
        info = request_key.explain("partner.refund", args)
    assert info.used_fields == ("payment.amount",)
    assert info.dropped_fields == ("payment.timestamp", "trace_id")
    with pytest.warns(UserWarning, match="unstable_field"):
        included = request_key.explain("partner.refund", args, include=["payment.timestamp"])
    assert included.used_fields == ("payment.timestamp",)
    assert included.dropped_fields == ("payment.amount", "trace_id")


def test_request_key_array_paths_and_escaped_field_names() -> None:
    args = {"items": [{"amount": "1", "note": "a"}, {"amount": "2", "note": "b"}], "a.b": 3}
    with pytest.warns(UserWarning, match="unstable_field"):
        info = request_key.explain("batch.refund", args, exclude=["a\\.b"])
    assert info.used_fields == ("items.amount",)
    assert info.dropped_fields == ("a\\.b", "items.note")
    with pytest.raises(ValueError, match="unknown field path"):
        request_key("batch.refund", args, exclude=["a.b"])
    with pytest.warns(UserWarning, match="unstable_field"):
        included = request_key.explain("batch.refund", args, include=["items.amount"])
    assert included.used_fields == ("items.amount",)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_request_key_rejects_non_finite_numbers(bad: float) -> None:
    with pytest.raises(ValueError, match="finite JSON"):
        request_key("partner.refund", {"details": [{"amount": bad}]})


def test_request_key_warns_about_floats_and_rejects_unknown_paths() -> None:
    with pytest.warns(UserWarning, match="float_argument"):
        request_key("partner.refund", {"amount": 12.1})
    with pytest.raises(ValueError, match="unknown field path"):
        request_key("partner.refund", {"amount": "12.00"}, exclude=["amout"])
    with pytest.raises(ValueError, match="cannot be used together"):
        request_key("partner.refund", {"amount": "12.00"}, include=["amount"], exclude=[])


def test_published_request_key_vectors() -> None:
    path = Path(__file__).resolve().parents[3] / "spec" / "request-key-v1" / "vectors.json"
    vectors = json.loads(path.read_text())["vectors"]
    assert vectors
    for case in vectors:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            info = request_key.explain(
                case["tool"],
                case["args"],
                business_id=case["business_id"],
                scope=case["scope"],
                include=case["include"],
                exclude=case["exclude"],
            )
        assert info.key == case["key"], case["name"]
        assert list(info.used_fields) == case["used_fields"], case["name"]
        assert list(info.dropped_fields) == case["dropped_fields"], case["name"]


@pytest.mark.asyncio
async def test_auto_key_is_forwarded_in_both_mcp_places() -> None:
    args = {"order_id": "A1", "amount": "12.00"}
    expected = request_key("partner.refund", args, business_id="A1")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["idempotency-key"] == expected
        assert body["id"] == expected
        assert body["params"]["mcpContext"]["idempotency_key"] == expected
        assert body["params"]["arguments"] == args
        return httpx.Response(409, json={"detail": "idempotency_key_reused"})

    async with AgentMiddlewareClient(
        api_key="test-key", base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(IdempotencyConflictError, match="idempotency_key_reused"):
            await client.invoke_tool(
                "partner.refund",
                args,
                wallet_id="wallet-1",
                permit_id="permit-1",
                idempotency_key="auto",
                business_id="A1",
            )
