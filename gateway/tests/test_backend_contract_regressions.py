"""Regression coverage for QA findings BE-002 and BE-003 from PR #586."""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core import url_guard
from app.core.config import get_settings
from app.main import app
from app.services.upstream_mcp import (
    UpstreamMcpConfiguration,
    UpstreamMcpConfigurationError,
    validate_upstream_url,
)


@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/permits", "get"),
        ("/v1/permits", "post"),
        ("/v1/receipts", "get"),
        ("/v1/me/authority", "get"),
    ],
)
def test_openapi_declares_bearer_or_api_key(path, method):
    previous_schema = app.openapi_schema
    try:
        app.openapi_schema = None
        schema = app.openapi()
        schemes = schema["components"]["securitySchemes"]
        assert schemes["HTTPBearer"]["type"] == "http"
        assert schemes["HTTPBearer"]["scheme"] == "bearer"
        assert schemes["APIKeyHeader"]["type"] == "apiKey"
        assert schema["paths"][path][method]["security"] == [
            {"APIKeyHeader": []},
            {"HTTPBearer": []},
        ]
    finally:
        app.openapi_schema = previous_schema


@pytest.mark.parametrize(
    "path",
    ["/v1/permits", "/v1/receipts", "/v1/me/authority"],
)
@pytest.mark.parametrize("authorization", [None, "", "Basic opaque", "Bearer bad"])
async def test_optional_auth_schemes_never_allow_unauthenticated_requests(
    path, authorization
):
    headers = (
        {}
        if authorization is None
        else {"Authorization": authorization, "X-API-Key": "test-key"}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == 401
    assert "X-Credential-Rejected" not in response.headers


@pytest.mark.parametrize(
    "address", ["224.0.0.1", "239.255.255.250", "ff02::1", "ff05::1"]
)
@pytest.mark.parametrize("source", ["literal", "dns", "mixed_dns"])
async def test_outbound_guard_rejects_multicast(address, source, monkeypatch):
    monkeypatch.setattr(get_settings(), "ALLOW_PRIVATE_NETWORK_TARGETS", False)

    async def resolve(_host):
        addresses = ["93.184.216.34", address] if source == "mixed_dns" else [address]
        return [(None, None, None, None, (item, 0)) for item in addresses]

    monkeypatch.setattr(url_guard, "_resolve_host", resolve)
    host = f"[{address}]" if ":" in address else address
    if source != "literal":
        host = "partner.example"
    assert (
        await url_guard.check_outbound_url(f"https://{host}/")
        == "private_address_blocked"
    )


@pytest.mark.parametrize(
    "address", ["224.0.0.1", "239.255.255.250", "ff02::1", "ff05::1"]
)
@pytest.mark.parametrize("source", ["literal", "dns", "mixed_dns"])
async def test_upstream_guard_rejects_multicast(address, source):
    async def resolve(_host, _port):
        return ("93.184.216.34", address) if source == "mixed_dns" else (address,)

    host = f"[{address}]" if ":" in address else address
    if source != "literal":
        host = "partner.example"
    configuration = UpstreamMcpConfiguration(
        url=f"https://{host}/mcp",
        origin=f"https://{host}",
        tool_name="notes.write",
        public_tool_id="partner.notes.write",
        bearer_token=SecretStr(""),
        credits_per_call=Decimal("1"),
        connect_timeout_seconds=1.0,
        call_timeout_seconds=1.0,
        max_response_bytes=1024,
        environment="production",
    )
    with pytest.raises(UpstreamMcpConfigurationError):
        await validate_upstream_url(configuration, resolver=resolve)
