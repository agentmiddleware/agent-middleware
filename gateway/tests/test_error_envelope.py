"""HTTP error responses expose a stable code and actionable copy."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.middleware.error_envelope import ERROR_COPY, error_envelope
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
    assert payload["message"] == "No route matches this path."
    assert "/docs" in payload["fix"] and "/openapi.json" in payload["fix"]
    assert (
        payload["docs_url"] == "https://www.agentmiddleware.dev/docs/errors#not_found"
    )


@pytest.mark.anyio
async def test_short_api_key_points_to_try(clean_database):
    """BUG-AMA-003: a too short key must name the demo key page."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/me/authority", headers={"X-API-Key": "short"})

    assert response.status_code == 401
    payload = response.json()
    assert payload["detail"]["error"] == "invalid_api_key"
    assert payload["code"] == "invalid_api_key"
    assert "https://www.agentmiddleware.dev/try" in payload["fix"]


@pytest.mark.anyio
async def test_bogus_bearer_has_plain_next_step(clean_database):
    """BUG-AMA-003: a junk bearer must get plain copy, not only JWT jargon."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/me/authority", headers={"Authorization": "Bearer not-a-jwt"}
        )

    assert response.status_code == 401
    payload = response.json()
    assert payload["detail"]["error"] == "invalid_token"
    assert payload["code"] == "invalid_token"
    assert payload["message"] == "That bearer token was not accepted."
    assert "X-API-Key" in payload["fix"] or "Bearer" in payload["fix"]


@pytest.mark.anyio
async def test_malformed_json_has_plain_message(clean_database):
    """BUG-AMA-003: a json_invalid 422 must carry a plain next step."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/mcp/tools/anything/invoke",
            content=b"{not-json",
            headers={
                "Content-Type": "application/json",
                **BOOTSTRAP_HEADERS,
            },
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "invalid_request"
    assert payload["message"] == "This request was not valid."
    assert "/docs" in payload["fix"] or "/openapi.json" in payload["fix"]


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
    for message, fix in ERROR_COPY.values():
        assert not any(
            mark in text.replace("X-API-Key", "")
            for text in (message, fix)
            for mark in "-–—"
        )


@pytest.mark.parametrize(
    ("code", "message", "fix"),
    [
        (
            "missing_wallet_id",
            "This call needs a wallet. Pass wallet_id from your demo key response, or use a key with one wallet.",
            "Pass wallet_id from your demo key response, or use a key with one wallet.",
        ),
        (
            "invalid_api_key",
            "Your key was not accepted.",
            "Check the X-API-Key header, or get a demo key at https://www.agentmiddleware.dev/try.",
        ),
        (
            "invalid_token",
            "That bearer token was not accepted.",
            "Send a valid Authorization Bearer token, or use an X-API-Key instead.",
        ),
        (
            "invalid_request",
            "This request was not valid.",
            "Check the body against /docs or /openapi.json, then send valid JSON.",
        ),
        (
            "not_found",
            "No route matches this path.",
            "Open /docs or /openapi.json for the published API.",
        ),
        (
            "permit_not_found",
            "We could not find that permit.",
            "Create a permit first and pass its permit_id.",
        ),
        (
            "permit_expired",
            "Your permit has expired.",
            "Create a new permit with a later expires_at.",
        ),
        (
            "permit_tool_not_allowed",
            "Your permit does not include this tool.",
            "Create a permit that lists this tool in allowed_tools.",
        ),
        (
            "permit_budget_exceeded",
            "This call costs more credits than the permit has left.",
            "Create a permit with a larger max_credits.",
        ),
        (
            "insufficient_funds",
            "Your wallet does not have enough credits.",
            "Get a new demo key, or add credits to the wallet.",
        ),
        (
            "idempotency_key_reused",
            "This request key was already used with different arguments.",
            "Use a new request key for a new action.",
        ),
        (
            "idempotency_key_too_long",
            "This request key is too long.",
            "Use a shorter request key, such as a UUID.",
        ),
        (
            "delivery_uncertain",
            "We charged this call but do not know if the tool finished.",
            "Check the tool directly before you try again. We never resend it on our own.",
        ),
    ],
)
def test_top_ten_error_copy_is_exact(code, message, fix):
    assert error_envelope(code) == {
        "code": code,
        "message": message,
        "fix": fix,
        "docs_url": f"https://www.agentmiddleware.dev/docs/errors#{code}",
    }


def test_header_body_key_conflict_copy_is_exact():
    assert error_envelope("idempotency_key_conflict") == {
        "code": "idempotency_key_conflict",
        "message": "The request key in the header and the body do not match. Send the same key in both.",
        "fix": "Send one request key, or use the same key in the header and the body.",
        "docs_url": "https://www.agentmiddleware.dev/docs/errors#idempotency_key_conflict",
    }
