"""Independent review probes (2026-09, against c6b0534): reject malformed inputs before side effects.

Kept verbatim as the reproduction the review supplied; the broader contract lives in
``tests/test_mcp_idempotency_key_validation.py``.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import provision_agent_wallet, BOOTSTRAP_HEADERS


@pytest.fixture
async def review_client(monkeypatch):
    import base64

    monkeypatch.setenv(
        "TRUST_SIGNING_PRIVATE_KEY_B64", base64.b64encode(b"r" * 32).decode()
    )
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    monkeypatch.setenv("TRUST_MODE_ENABLED", "true")
    monkeypatch.setenv("ALLOW_LEGACY_UNPERMITTED_MCP", "false")
    get_settings.cache_clear()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        trust_env=False,
    ) as client:
        yield client
    get_settings.cache_clear()


@pytest.mark.parametrize("bad_key", ["", 123, [], {"key": "retry"}, "x" * 257])
async def test_explicit_invalid_retry_key_never_executes(
    review_client, clean_database, bad_key
):
    p = await provision_agent_wallet(review_client)
    calls = []

    def effect(text: str = "one"):
        calls.append(text)
        return {"text": text}

    registry = get_service_registry()
    registry.register_local(
        service_id="review.effect",
        name="Review effect",
        description="Local counted effect",
        category=ServiceCategory.AGENT_COMMS,
        func=effect,
        credits_per_unit=2.0,
        unit_name="call",
    )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "review.effect",
            "arguments": {"text": "one"},
            "_meta": {"io.agentmiddleware/idempotency_key": bad_key},
        },
    }
    headers = {**p["agent_headers"], "Accept": "application/json, text/event-stream"}
    try:
        import copy

        control = copy.deepcopy(body)
        control["params"]["_meta"]["io.agentmiddleware/idempotency_key"] = (
            "valid-control"
        )
        accepted = await review_client.post("/mcp", json=control, headers=headers)
        assert "result" in accepted.json(), accepted.text
        assert calls == ["one"]
        calls.clear()
        responses = [
            await review_client.post("/mcp", json=body, headers=headers)
            for _ in range(2)
        ]
        receipts = [
            r.json().get("result", {}).get("receipt", {}).get("receipt_id")
            for r in responses
        ]
        assert calls == [], (
            f"Invalid retry key executed {len(calls)} effects; statuses={[r.status_code for r in responses]}; receipts={receipts}"
        )
    finally:
        registry.unregister_local("review.effect")


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        "bad",
        3,
        {"method": "tools/call", "params": []},
        {"method": "tools/call", "params": {"mcpContext": [1]}},
    ],
)
async def test_malformed_legacy_envelope_never_returns_500(review_client, body):
    import json

    response = await review_client.post(
        "/mcp/messages",
        content=json.dumps(body),
        headers={**BOOTSTRAP_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code < 500, (
        f"Malformed envelope caused HTTP {response.status_code}: {response.text[:160]}"
    )
