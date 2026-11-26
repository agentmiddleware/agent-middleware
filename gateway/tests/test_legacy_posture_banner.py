"""The legacy posture banner says what the posture actually accepts (#352).

With ``ALLOW_LEGACY_UNPERMITTED_MCP=true`` the banner said ungoverned MCP
calls were accepted, but a tool registered with ``require_permit`` (the
dogfood tool, every upstream tool) still refuses an unpermitted call with
``permit_required``. Both halves of the corrected banner are pinned here
over HTTP, in the suite's default legacy posture.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.trust_mode import (
    describe_permissive_trust_mode,
    warn_if_trust_mode_permissive,
)
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def legacy_posture(monkeypatch):
    monkeypatch.setenv("TRUST_MODE_ENABLED", "false")
    monkeypatch.setenv("ALLOW_LEGACY_UNPERMITTED_MCP", "true")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def tools():
    registry = get_service_registry()
    runs = {"plain": 0, "permit": 0}

    def plain(message: str = "ok") -> dict[str, Any]:
        runs["plain"] += 1
        return {"message": message}

    def permitted(message: str = "ok") -> dict[str, Any]:
        runs["permit"] += 1
        return {"message": message}

    registry.register_local(
        service_id="legacy-banner-plain",
        name="Legacy banner plain tool",
        description="No require_permit",
        category=ServiceCategory.AGENT_COMMS,
        func=plain,
        credits_per_unit=1.0,
    )
    registry.register_local(
        service_id="legacy-banner-permit",
        name="Legacy banner permit tool",
        description="require_permit like the dogfood and upstream tools",
        category=ServiceCategory.AGENT_COMMS,
        func=permitted,
        credits_per_unit=1.0,
        require_permit=True,
    )
    try:
        yield runs
    finally:
        registry.unregister_local("legacy-banner-plain")
        registry.unregister_local("legacy-banner-permit")


def test_banner_names_the_require_permit_exception(legacy_posture, caplog) -> None:
    description = describe_permissive_trust_mode(
        trust_mode_enabled=False, allow_legacy_unpermitted_mcp=True
    )
    assert description is not None
    assert "ungoverned MCP calls accepted" not in description
    assert "only for tools registered without require_permit" in description
    assert "permit_required" in description

    with caplog.at_level(logging.WARNING, logger="app.core.trust_mode"):
        warn_if_trust_mode_permissive(legacy_posture)
    assert "only for tools registered without require_permit" in caplog.text


async def _call(client: AsyncClient, ctx: dict[str, Any], tool: str) -> Any:
    return await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": tool,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {"message": "hi"},
                "mcpContext": {"wallet_id": ctx["agent_wallet_id"]},
            },
        },
        headers=ctx["agent_headers"],
    )


@pytest.mark.anyio
async def test_legacy_posture_behaves_as_the_banner_says(
    client: AsyncClient, clean_database: None, legacy_posture, tools
) -> None:
    ctx = await provision_agent_wallet(client)

    plain = await _call(client, ctx, "legacy-banner-plain")
    assert plain.status_code == 200, plain.text
    assert "result" in plain.json(), plain.text
    assert tools["plain"] == 1

    refused = await _call(client, ctx, "legacy-banner-permit")
    assert refused.status_code == 200, refused.text
    assert refused.json()["error"]["message"] == "permit_required", refused.text
    assert tools["permit"] == 0
