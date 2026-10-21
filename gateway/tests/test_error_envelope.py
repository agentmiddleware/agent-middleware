"""HTTP error responses expose a stable code and actionable copy."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.middleware.error_envelope import ERROR_COPY
from tests.test_trust_helpers import BOOTSTRAP_HEADERS


@pytest.mark.anyio
async def test_known_http_error_keeps_status_and_existing_detail(clean_database):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/me/authority", headers={"X-API-Key": "bad"})

    assert response.status_code == 401
    payload = response.json()
    assert payload["detail"]["error"] == "invalid_api_key"
    assert payload["code"] == "invalid_api_key"
    assert payload["message"]
    assert payload["fix"]
    assert payload["docs_url"] == (
        "https://www.agentmiddleware.dev/docs/errors#invalid_api_key"
    )


@pytest.mark.anyio
async def test_unmatched_http_error_has_envelope(clean_database):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/route-that-does-not-exist")

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == "not_found"
    assert payload["message"]
    assert payload["fix"]
    assert (
        payload["docs_url"] == "https://www.agentmiddleware.dev/docs/errors#not_found"
    )


@pytest.mark.anyio
async def test_body_limit_error_uses_envelope(clean_database):
    body = b"x" * (get_settings().MAX_REQUEST_BODY_BYTES + 1)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/mcp/messages", content=body)

    assert response.status_code == 413
    payload = response.json()
    assert payload["code"] == "request_too_large"
    assert payload["max_request_body_bytes"] == get_settings().MAX_REQUEST_BODY_BYTES
    assert payload["docs_url"].endswith("#request_too_large")


@pytest.mark.anyio
async def test_rest_invoke_missing_wallet_has_specific_code(clean_database):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/mcp/tools/anything/invoke",
            json={"name": "anything", "arguments": {}},
            headers=BOOTSTRAP_HEADERS,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Missing wallet_id"
    assert response.json()["code"] == "missing_wallet_id"


def test_error_copy_has_no_dashes():
    assert len(ERROR_COPY) == 10
    for message, fix in ERROR_COPY.values():
        assert not any(mark in text for text in (message, fix) for mark in "-–—")
