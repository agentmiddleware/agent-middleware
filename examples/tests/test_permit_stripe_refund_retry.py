"""Offline test for examples/permit_stripe_refund_retry.py.

Stripe is replaced by an ``httpx.MockTransport``; no network request leaves the
process. The gateway and both upstream MCP servers run in process over ASGI,
so receipts, ledger debits and idempotency records all come from the product.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import permit_stripe_refund_retry as example
import pytest

# Tests supply their own inputs: this is not a real key, it only has the
# test mode prefix the example requires.
TEST_KEY = "sk_test_" + "offline0fixture0value0not0a0real0key"


class FakeStripe:
    """Answer the three Stripe calls the example makes and count them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refunds: list[dict[str, Any]] = []
        self.refunds_by_idempotency_key: dict[str, dict[str, Any]] = {}

    @property
    def refund_posts(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == "POST" and r.url.path == "/v1/refunds"
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.stripe.com"
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.method == "POST" and request.url.path == "/v1/payment_intents":
            assert form["amount"] == "100" and form["currency"] == "usd"
            return httpx.Response(
                200,
                json={
                    "id": "pi_offline_test",
                    "livemode": False,
                    "status": "succeeded",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/refunds":
            assert form == {"payment_intent": "pi_offline_test", "amount": "100"}
            idem = request.headers["Idempotency-Key"]
            if idem not in self.refunds_by_idempotency_key:
                refund = {
                    "id": f"re_offline_{len(self.refunds) + 1}",
                    "amount": 100,
                    "currency": "usd",
                    "status": "succeeded",
                    "payment_intent": form["payment_intent"],
                }
                self.refunds.append(refund)
                self.refunds_by_idempotency_key[idem] = refund
            return httpx.Response(200, json=self.refunds_by_idempotency_key[idem])
        if request.method == "GET" and request.url.path == "/v1/refunds":
            return self.list_refunds(request)
        return httpx.Response(404, json={"error": {"type": "invalid_request_error"}})

    def list_refunds(self, request: httpx.Request) -> httpx.Response:
        """Page like Stripe: newest first, default limit 10, ``starting_after``."""
        params = request.url.params
        matching = [
            r
            for r in reversed(self.refunds)
            if r["payment_intent"] == params.get("payment_intent")
        ]
        limit = int(params.get("limit", "10"))
        assert 1 <= limit <= 100
        start = 0
        if "starting_after" in params:
            ids = [r["id"] for r in matching]
            start = ids.index(params["starting_after"]) + 1
        page = matching[start : start + limit]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": page,
                "has_more": start + limit < len(matching),
            },
        )


@pytest.fixture
def restore_environ() -> Iterator[None]:
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_retry_returns_same_receipt_one_debit_and_skips_stripe(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    restore_environ: None,
) -> None:
    fake = FakeStripe()
    monkeypatch.setattr(example, "stripe_transport", httpx.MockTransport(fake.handler))
    original_run = example.run
    captured: dict[str, example.RetryEvidence] = {}

    async def recording_run(*args: Any, **kwargs: Any) -> example.RetryEvidence:
        captured["evidence"] = await original_run(*args, **kwargs)
        return captured["evidence"]

    monkeypatch.setattr(example, "run", recording_run)
    caplog.set_level(logging.DEBUG)

    exit_code = example.main({"STRIPE_SECRET_KEY": TEST_KEY})

    out, err = capfd.readouterr()
    assert exit_code == 0, err
    evidence = captured["evidence"]

    # 1. Two dispatches with the same idempotency key return the same receipt.
    assert evidence.first_receipt_id
    assert evidence.first_receipt_id == evidence.second_receipt_id
    # 2. Exactly one debit for that key (the other wallet debit is tool 1).
    assert evidence.debits_for_key == 1
    assert evidence.wallet_debits == 2
    # 3. The second dispatch never calls Stripe.
    assert evidence.stripe_calls_after_first == 1
    assert evidence.stripe_calls_after_second == 1
    assert len(fake.refund_posts) == 1
    assert len(fake.refunds) == 1
    assert evidence.stripe_refunds_before == 0
    assert evidence.stripe_refunds_after == 1
    assert fake.refund_posts[0].headers["Idempotency-Key"] == (
        f"amw-refund-{evidence.idempotency_key}"
    )
    # Tool 1 (partner.echo) still routes through the gateway.
    assert evidence.echo_receipt_id
    assert evidence.echo_receipt_id != evidence.first_receipt_id
    assert example.check(evidence) == []

    assert f"dispatch 1 receipt_id:    {evidence.first_receipt_id}" in out
    assert f"dispatch 2 receipt_id:    {evidence.second_receipt_id}" in out
    assert "debits for this key:      1" in out
    assert "dispatch 2 called Stripe: no" in out
    assert "PASS:" in out

    # The Stripe key never reaches stdout, stderr, or the log.
    assert TEST_KEY not in out
    assert TEST_KEY not in err
    assert TEST_KEY not in caplog.text


@pytest.mark.parametrize("existing", [10, 11, 100, 101, 250])
def test_list_refunds_follows_pagination(
    existing: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tests supply their own inputs: these stand in for Stripe's response to a
    # payment that already has many refunds.
    fake = FakeStripe()
    fake.refunds = [
        {
            "id": f"re_offline_{i}",
            "amount": 1,
            "currency": "usd",
            "status": "succeeded",
            "payment_intent": "pi_offline_test",
        }
        for i in range(1, existing + 1)
    ]
    monkeypatch.setattr(example, "stripe_transport", httpx.MockTransport(fake.handler))

    refunds = asyncio.run(example.list_refunds(TEST_KEY, "pi_offline_test"))

    assert len(refunds) == existing
    assert len({r["id"] for r in refunds}) == existing
    gets = [r for r in fake.requests if r.method == "GET"]
    assert len(gets) == -(-existing // 100)
    assert all(r.url.params["limit"] == "100" for r in gets)
    assert "starting_after" not in gets[0].url.params
    # Each later page starts after the last refund of the page before it.
    assert [r.url.params["starting_after"] for r in gets[1:]] == [
        refunds[i]["id"] for i in range(99, existing - 1, 100)
    ]


def test_list_refunds_stops_when_stripe_sends_an_empty_page_with_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": True})

    monkeypatch.setattr(example, "stripe_transport", httpx.MockTransport(broken))
    with pytest.raises(RuntimeError, match="more refunds"):
        asyncio.run(example.list_refunds(TEST_KEY, "pi_offline_test"))


def test_check_flags_each_broken_property() -> None:
    good = example.RetryEvidence(
        idempotency_key="k",
        first_receipt_id="a",
        second_receipt_id="a",
        debits_for_key=1,
        wallet_debits=2,
        stripe_calls_after_first=1,
        stripe_calls_after_second=1,
        stripe_refunds_before=0,
        stripe_refunds_after=1,
        refund_id="r",
        echo_receipt_id="e",
    )
    assert example.check(good) == []
    fields = good.__dict__
    assert example.check(example.RetryEvidence(**{**fields, "second_receipt_id": "b"}))
    assert example.check(example.RetryEvidence(**{**fields, "debits_for_key": 2}))
    assert example.check(
        example.RetryEvidence(**{**fields, "stripe_calls_after_second": 2})
    )
    assert example.check(example.RetryEvidence(**{**fields, "stripe_refunds_after": 2}))
    # A payment that already had refunds still passes with one new refund.
    assert (
        example.check(
            example.RetryEvidence(
                **{**fields, "stripe_refunds_before": 2, "stripe_refunds_after": 3}
            )
        )
        == []
    )


def test_stripe_refund_server_repr_hides_the_key() -> None:
    server = example.StripeRefundServer(
        key=TEST_KEY, tool_name="stripe.refunds.create", allowed_host="localhost:1"
    )
    assert TEST_KEY not in repr(server)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("sk_live_" + "x" * 24, id="secret-live"),
        pytest.param("rk_live_" + "x" * 24, id="restricted-live"),
        pytest.param("pk_test_" + "x" * 24, id="publishable-test"),
        pytest.param("sk_" + "x" * 24, id="no-mode"),
        pytest.param("", id="empty"),
        pytest.param(None, id="unset"),
    ],
)
def test_refuses_anything_but_a_test_mode_key(
    key: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def no_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("refusal must happen before any Stripe request")

    monkeypatch.setattr(example, "stripe_transport", httpx.MockTransport(no_network))
    monkeypatch.setattr(
        example,
        "configure_gateway",
        lambda *_a: pytest.fail("refusal must happen before gateway setup"),
    )
    env = {} if key is None else {"STRIPE_SECRET_KEY": key}
    with pytest.raises(SystemExit) as excinfo:
        example.main(env)
    assert excinfo.value.code == example.SAFETY_REFUSAL_EXIT
    out, err = capfd.readouterr()
    assert "refusing to run" in err
    if key:
        assert key not in out
        assert key not in err


@pytest.mark.parametrize("prefix", ["sk_test_", "rk_test_"])
def test_accepts_test_mode_secret_and_restricted_keys(prefix: str) -> None:
    key = prefix + "x" * 24
    assert example.require_test_key({"STRIPE_SECRET_KEY": key}) == key


def test_refuses_non_loopback_upstream_origin(
    capfd: pytest.CaptureFixture[str],
) -> None:
    env = {
        "STRIPE_SECRET_KEY": TEST_KEY,
        "UPSTREAM_MCP_ORIGIN_2": "https://mcp.example.com",
    }
    with pytest.raises(SystemExit) as excinfo:
        example.main(env)
    assert excinfo.value.code == example.SAFETY_REFUSAL_EXIT
    assert TEST_KEY not in capfd.readouterr().err


def test_upstream_env_maps_to_tool_two() -> None:
    echo, refund = example.read_upstreams(
        {
            "UPSTREAM_MCP_ORIGIN_2": "http://localhost:9200",
            "UPSTREAM_TOOL_2": "stripe.refund.test",
        }
    )
    assert echo.tool == "partner.echo"
    assert echo.url == "http://localhost:9101/mcp"
    assert refund.tool == "stripe.refund.test"
    assert refund.url == "http://localhost:9200/mcp"
    assert refund.host == "localhost:9200"
