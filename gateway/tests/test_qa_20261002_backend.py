"""QA sweep: public auth contract and replay-key encoding boundaries.

Optional generative/schema tooling is installed by docs/qa/2026-10-02/
requirements-qa.txt. Product behavior is deliberately unchanged.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.idempotency import (
    InvalidIdempotencyKeyError,
    decode_idempotency_key_header,
    resolve_client_idempotency_key,
)
from tests.test_mcp_idempotency_key_validation import CountedTool, _snapshot
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


@pytest.fixture
async def qa_client():
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/permits"),
        ("POST", "/v1/permits"),
        ("GET", "/v1/receipts"),
        ("GET", "/v1/receipts/missing"),
        ("GET", "/v1/billing/wallets"),
        ("POST", "/v1/billing/charge"),
        ("GET", "/v1/audit/events"),
        ("POST", "/v1/audit/verify-chain"),
        ("GET", "/v1/me/authority"),
        ("GET", "/v1/api-keys/missing"),
        ("POST", "/mcp/messages"),
        ("POST", "/mcp/tools/missing/invoke"),
    ],
)
@pytest.mark.parametrize("credential", ["absent", "invalid-bearer"])
async def test_sensitive_route_rejects_unauthenticated_caller(
    qa_client, method, path, credential
):
    # A malformed Authorization header must not fall back to a valid bootstrap key.
    headers = (
        {}
        if credential == "absent"
        else {
            "Authorization": "Bearer malformed",
            "X-API-Key": "test-key",
        }
    )
    response = await qa_client.request(method, path, headers=headers, json={})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert "traceback" not in response.text.lower()
    assert "X-Credential-Rejected" not in response.headers


def test_openapi_schema_is_valid_with_unique_operation_ids():
    validator = pytest.importorskip("openapi_spec_validator")
    # Force a fresh schema: other tests mount dormant routers dynamically.
    old_schema = app.openapi_schema
    try:
        app.openapi_schema = None
        schema = app.openapi()
        validator.validate(schema)
        operations = [
            operation
            for path in schema["paths"].values()
            for method, operation in path.items()
            if method in {"get", "post", "put", "patch", "delete"}
        ]
        counts = Counter(operation["operationId"] for operation in operations)
        assert all(count == 1 for count in counts.values())
    finally:
        app.openapi_schema = old_schema


def test_openapi_declares_supported_bearer_authentication():
    old_schema = app.openapi_schema
    try:
        app.openapi_schema = None
        schema = app.openapi()
        schemes = schema["components"]["securitySchemes"]
        bearer = [
            name
            for name, definition in schemes.items()
            if definition.get("type") == "http" and definition.get("scheme") == "bearer"
        ]
        assert len(bearer) == 1
        security = schema["paths"]["/v1/permits"]["get"]["security"]
        assert {bearer[0]: []} in security
        assert {"APIKeyHeader": []} in security
        assert all(len(alternative) == 1 for alternative in security)
        for path in ("/health", "/.well-known/agent.json"):
            assert not schema["paths"][path]["get"].get("security")
    finally:
        app.openapi_schema = old_schema


def test_scalar_unicode_replay_keys_roundtrip_without_normalization():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import strategies as st

    alphabet = st.characters(
        exclude_categories=("Cs",),
        exclude_characters="".join(chr(i) for i in [*range(32), 127]),
    )

    @hypothesis.settings(max_examples=200, derandomize=True, deadline=None)
    @hypothesis.given(
        st.text(alphabet=alphabet, min_size=1, max_size=128).filter(str.strip)
    )
    def check(key):
        decoded = decode_idempotency_key_header(key.encode("utf-8").decode("latin-1"))
        assert decoded == key
        assert (
            resolve_client_idempotency_key([("header", decoded), ("body", key)]) == key
        )
        with pytest.raises(InvalidIdempotencyKeyError):
            resolve_client_idempotency_key([("header", key), ("body", key + " ")])

    check()


async def test_lone_surrogate_replay_key_is_rejected_before_effect_or_debit(
    qa_client, clean_database
):
    tool = CountedTool("qa.encoding.effect")
    try:
        caller = await provision_agent_wallet(qa_client)
        permit = await create_tool_permit(
            qa_client,
            wallet_id=caller["agent_wallet_id"],
            key_id=caller["key_id"],
            tool_name=tool.name,
        )
        before = await _snapshot(qa_client, caller, tool)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool.name,
                "arguments": {"text": "one"},
                "mcpContext": {
                    "wallet_id": caller["agent_wallet_id"],
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "\ud800",
                },
            },
        }
        response = await qa_client.post(
            "/mcp/messages",
            content=json.dumps(body).encode("ascii"),
            headers={**caller["agent_headers"], "Content-Type": "application/json"},
        )
        assert await _snapshot(qa_client, caller, tool) == before
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid JSON"
    finally:
        tool.close()


@pytest.mark.parametrize(
    "address", ["224.0.0.1", "239.255.255.250", "ff02::1", "ff05::1"]
)
@pytest.mark.parametrize("resolved", [False, True], ids=["literal", "mixed-dns"])
async def test_outbound_url_guard_rejects_multicast(address, resolved, monkeypatch):
    from app.core.config import get_settings
    from app.core.url_guard import check_outbound_url

    monkeypatch.setattr(get_settings(), "ALLOW_PRIVATE_NETWORK_TARGETS", False)
    host = f"[{address}]" if ":" in address else address
    if resolved:

        async def resolve(_host):
            return [
                (None, None, None, None, (value, 0)) for value in ("8.8.8.8", address)
            ]

        monkeypatch.setattr("app.core.url_guard._resolve_host", resolve)
        host = "multicast.example"
    assert await check_outbound_url(f"http://{host}/") == "private_address_blocked"


@pytest.mark.parametrize("address", ["224.0.0.1", "ff02::1"])
@pytest.mark.parametrize("resolved", [False, True], ids=["literal", "mixed-dns"])
async def test_upstream_url_guard_rejects_multicast(address, resolved):
    from decimal import Decimal
    from app.services.upstream_mcp import (
        UpstreamMcpConfiguration,
        UpstreamMcpConfigurationError,
        validate_upstream_url,
    )

    host = f"[{address}]" if ":" in address else address
    if resolved:
        host = "multicast.example"

    async def resolve(_host, _port):
        return ("8.8.8.8", address)

    configuration = UpstreamMcpConfiguration(
        url=f"https://{host}/mcp",
        origin=f"https://{host}",
        tool_name="notes.write",
        public_tool_id="partner.notes.write",
        bearer_token="",
        credits_per_call=Decimal("1"),
        connect_timeout_seconds=1.0,
        call_timeout_seconds=1.0,
        max_response_bytes=1024,
        environment="production",
    )
    with pytest.raises(
        UpstreamMcpConfigurationError, match="upstream_mcp_configuration_invalid"
    ):
        await validate_upstream_url(configuration, resolver=resolve)
