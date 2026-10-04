"""framework_integrations.B2AClient.charge must match the server's charge contract.

The legacy method posted a JSON body ``{wallet_id, amount, description}``
with no ``Idempotency-Key``. ``POST /v1/billing/charge`` reads ``wallet_id``,
``service`` and ``units`` as query parameters (the server prices the units;
a caller cannot name an amount), so every call failed validation, and with
no key a retried charge could never be deduplicated. The method now sends
the server's request shape and requires a caller-owned idempotency key, so a
retry replays the original outcome instead of debiting twice.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from framework_integrations import B2AClient
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def http():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _b2a_against_app(api_key: str, wallet_id: str) -> B2AClient:
    client = B2AClient(api_url="http://test", api_key=api_key, wallet_id=wallet_id)
    client._client = AsyncClient(transport=ASGITransport(app=app))
    return client


async def _balance(http: AsyncClient, wallet_id: str) -> float:
    resp = await http.get(f"/v1/billing/wallets/{wallet_id}", headers=BOOTSTRAP_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["balance"]


@pytest.mark.anyio
async def test_charge_sends_query_params_and_idempotency_key():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"action": "debit", "entry_id": "led-1"})

    client = B2AClient(api_url="http://b2a.test", api_key="k", wallet_id="wal-1")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.charge(
            "iot_bridge",
            units=3,
            description="sensor batch",
            idempotency_key="charge-key-1",
            request_path="POST /v1/iot/devices",
        )
    finally:
        await client.close()

    assert result == {"action": "debit", "entry_id": "led-1"}
    assert len(sent) == 1
    request = sent[0]
    assert (request.method, request.url.path) == ("POST", "/v1/billing/charge")
    assert dict(request.url.params) == {
        "wallet_id": "wal-1",
        "service": "iot_bridge",
        "units": "3",
        "description": "sensor batch",
        "request_path": "POST /v1/iot/devices",
    }
    assert request.headers["Idempotency-Key"] == "charge-key-1"
    assert request.headers["X-API-Key"] == "k"
    assert request.content == b""


@pytest.mark.anyio
@pytest.mark.parametrize("bad_key", ["", "   "])
async def test_charge_refuses_blank_idempotency_key_without_sending(bad_key):
    sent: list[httpx.Request] = []
    client = B2AClient(api_url="http://b2a.test", api_key="k", wallet_id="wal-1")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    )
    try:
        with pytest.raises(ValueError, match="idempotency_key"):
            await client.charge("iot_bridge", idempotency_key=bad_key)
    finally:
        await client.close()
    assert sent == []


@pytest.mark.anyio
async def test_charge_requires_an_idempotency_key_without_sending():
    """The old ``charge(amount, description)`` call shape fails loudly."""
    sent: list[httpx.Request] = []
    client = B2AClient(api_url="http://b2a.test", api_key="k", wallet_id="wal-1")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: sent.append(r) or httpx.Response(200, json={})
        )
    )
    try:
        with pytest.raises(TypeError, match="idempotency_key"):
            await client.charge(5.0, "legacy amount call")  # type: ignore[call-arg]
    finally:
        await client.close()
    assert sent == []


@pytest.mark.anyio
async def test_charge_against_app_debits_once_and_replays(http, clean_database):
    wallets = await provision_agent_wallet(http)
    agent = wallets["agent_wallet_id"]
    before = await _balance(http, agent)
    client = _b2a_against_app(wallets["agent_headers"]["X-API-Key"], agent)
    try:
        first = await client.charge(
            "iot_bridge", units=2, idempotency_key="legacy-client-charge-1"
        )
        replay = await client.charge(
            "iot_bridge", units=2, idempotency_key="legacy-client-charge-1"
        )
    finally:
        await client.close()

    assert first["action"] == "debit"
    assert first["service_category"] == "iot_bridge"
    assert replay == first
    # 2 units x 2 credits, debited exactly once despite the retry.
    assert await _balance(http, agent) == before - 4


@pytest.mark.anyio
async def test_charge_key_reuse_with_different_payload_is_refused(http, clean_database):
    wallets = await provision_agent_wallet(http)
    agent = wallets["agent_wallet_id"]
    before = await _balance(http, agent)
    client = _b2a_against_app(wallets["agent_headers"]["X-API-Key"], agent)
    try:
        await client.charge("iot_bridge", units=1, idempotency_key="legacy-reuse")
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.charge("iot_bridge", units=5, idempotency_key="legacy-reuse")
    finally:
        await client.close()

    assert excinfo.value.response.status_code == 409
    assert excinfo.value.response.json()["detail"]["error"] == "idempotency_key_reused"
    assert await _balance(http, agent) == before - 2


@pytest.mark.anyio
async def test_charge_cannot_debit_another_tenants_wallet(http, clean_database):
    attacker = await provision_agent_wallet(http)
    victim = await provision_agent_wallet(http)
    victim_wallet = victim["agent_wallet_id"]
    before = await _balance(http, victim_wallet)
    client = _b2a_against_app(attacker["agent_headers"]["X-API-Key"], victim_wallet)
    try:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.charge("iot_bridge", idempotency_key="cross-tenant-charge")
    finally:
        await client.close()

    assert excinfo.value.response.status_code == 403
    assert excinfo.value.response.json()["detail"]["error"] == "wallet_access_denied"
    assert await _balance(http, victim_wallet) == before


@pytest.mark.anyio
async def test_charge_with_unknown_api_key_is_refused(http, clean_database):
    wallets = await provision_agent_wallet(http)
    agent = wallets["agent_wallet_id"]
    before = await _balance(http, agent)
    client = _b2a_against_app("not-a-real-key", agent)
    try:
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.charge("iot_bridge", idempotency_key="unauth-charge")
    finally:
        await client.close()

    # An unknown-but-well-formed key is refused with 403 (app/core/auth.py).
    assert excinfo.value.response.status_code == 403
    assert excinfo.value.response.json()["detail"]["error"] == "invalid_api_key"
    assert await _balance(http, agent) == before


@pytest.mark.anyio
async def test_discover_and_awi_execute_forward_required_headers_verbatim():
    sent = []
    client = B2AClient(
        api_url="http://b2a.test", api_key="synthetic", wallet_id="wallet-local"
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json={})
        )
    )
    try:
        await client.discover()
        await client.execute_awi_action(
            "session-local",
            "navigate_to",
            {},
            permit_id="permit-local",
            idempotency_key=" logical-action ",
        )
    finally:
        await client.close()
    assert sent[0].url.path == "/v1/discover"
    assert sent[0].headers["X-API-Key"] == "synthetic"
    assert sent[1].url.path == "/v1/awi/execute"
    assert sent[1].headers["X-API-Key"] == "synthetic"
    assert sent[1].headers["X-Permit-Id"] == "permit-local"
    assert sent[1].headers["Idempotency-Key"] == " logical-action "


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("permit_id", "key"),
    [
        ("", "key"),
        (" ", "key"),
        (None, "key"),
        ("permit", ""),
        ("permit", " "),
        ("permit", None),
        ("permit", "x" * 129),
        ("permit", " " + "x" * 128),
    ],
)
async def test_awi_execute_refuses_invalid_headers_before_transport(permit_id, key):
    sent = []
    client = B2AClient(api_url="http://b2a.test", api_key="synthetic")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json={})
        )
    )
    try:
        with pytest.raises(ValueError):
            await client.execute_awi_action(
                "session", "navigate_to", {}, permit_id=permit_id, idempotency_key=key
            )
        with pytest.raises(TypeError):
            await client.execute_awi_action("session", "navigate_to", {})
    finally:
        await client.close()
    assert sent == []
