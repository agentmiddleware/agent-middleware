"""The REST invoke body accepts the JSON-RPC spelling of its billing context.

``POST /mcp/tools/{name}/invoke`` reads its billing context from
``mcp_context``. JSON-RPC ``params`` and most of the docs spell it
``mcpContext``, and the request model used to ignore unknown keys, so a client
that sent ``mcpContext`` had its wallet, permit and idempotency key dropped
without an error (issue #389). Both spellings now reach the same code, and a
body that sends both is refused.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import provision_agent_wallet

TOOL = "rest-context-spelling-tool"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def registered_tool():
    registry = get_service_registry()

    def tool_func(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id=TOOL,
        name="REST context spelling tool",
        description="Tool for the mcpContext spelling tests",
        category=ServiceCategory.AGENT_COMMS,
        func=tool_func,
        credits_per_unit=2.0,
        unit_name="call",
    )
    yield registry
    registry.unregister_local(TOOL)


async def _permit_for(client: AsyncClient, agent: dict, idem_key: str) -> str:
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": agent["agent_wallet_id"],
            "subject_wallet_id": agent["agent_wallet_id"],
            "subject_key_id": agent["key_id"],
            "allowed_tools": [TOOL],
            "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": "2030-01-01T00:00:00Z",
        },
        headers={**agent["agent_headers"], "Idempotency-Key": idem_key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["permit_id"]


async def _spent(client: AsyncClient, agent: dict, permit_id: str) -> Decimal:
    resp = await client.get(f"/v1/permits/{permit_id}", headers=agent["agent_headers"])
    assert resp.status_code == 200, resp.text
    return Decimal(str(resp.json()["spent_credits"]))


@pytest.mark.asyncio
async def test_json_rpc_spelling_applies_the_permit_and_the_idempotency_key(
    client, clean_database, registered_tool
):
    """Sent as ``mcpContext``, the permit is charged once and the replay is free."""
    agent = await provision_agent_wallet(client)
    permit_id = await _permit_for(client, agent, "spelling-permit-1")
    body = {
        "name": TOOL,
        "arguments": {"message": "camel case context"},
        "mcpContext": {
            "wallet_id": agent["agent_wallet_id"],
            "permit_id": permit_id,
            "idempotency_key": "spelling-invoke-1",
        },
    }

    first = await client.post(
        f"/mcp/tools/{TOOL}/invoke", json=body, headers=agent["agent_headers"]
    )
    assert first.status_code == 200, first.text
    charged = Decimal(str(first.json()["receipt"]["credits_charged"]))
    assert charged > 0
    assert await _spent(client, agent, permit_id) == charged

    replay = await client.post(
        f"/mcp/tools/{TOOL}/invoke", json=body, headers=agent["agent_headers"]
    )
    assert replay.status_code == 200, replay.text
    assert await _spent(client, agent, permit_id) == charged


@pytest.mark.asyncio
async def test_json_rpc_spelling_is_judged_like_the_snake_case_one(
    client, clean_database, registered_tool
):
    """A permit id that does not exist is refused the same way under both spellings."""
    agent = await provision_agent_wallet(client)
    context = {
        "wallet_id": agent["agent_wallet_id"],
        "permit_id": "permit_does_not_exist",
        "idempotency_key": "spelling-missing-permit",
    }

    answers = []
    for key in ("mcp_context", "mcpContext"):
        resp = await client.post(
            f"/mcp/tools/{TOOL}/invoke",
            json={"name": TOOL, "arguments": {}, key: context},
            headers=agent["agent_headers"],
        )
        answers.append((resp.status_code, resp.json()))

    assert answers[0][0] >= 400, answers[0]
    assert answers[0] == answers[1]


@pytest.mark.asyncio
async def test_both_spellings_in_one_body_are_refused(
    client, clean_database, registered_tool
):
    agent = await provision_agent_wallet(client)
    context = {"wallet_id": agent["agent_wallet_id"]}

    resp = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={
            "name": TOOL,
            "arguments": {},
            "mcp_context": context,
            "mcpContext": context,
        },
        headers=agent["agent_headers"],
    )

    assert resp.status_code == 422, resp.text
    assert "not both" in resp.text


@pytest.mark.asyncio
async def test_a_null_json_rpc_context_behaves_like_an_absent_one(
    client, clean_database, registered_tool
):
    """Pin: ``mcpContext: null`` is the legacy no-context shape, never a 500."""
    agent = await provision_agent_wallet(client)
    absent = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={"name": TOOL, "arguments": {}},
        headers=agent["agent_headers"],
    )
    null = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={"name": TOOL, "arguments": {}, "mcpContext": None},
        headers=agent["agent_headers"],
    )

    assert absent.status_code < 500, absent.text
    assert null.status_code == absent.status_code
