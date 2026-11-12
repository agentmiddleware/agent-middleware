"""A permit key replays the original permit after a process restart (#363).

The fake trust plane below keeps the first body it saw under each permit key
and answers 409 ``idempotency_key_reused`` to a different body, as the real
``POST /v1/permits`` does. A "restart" is a fresh tool instance.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from b2a_sdk import IdempotencyConflictError

from crewai_b2a import B2AClient, CrewAIB2ATool, JsonFilePermitStore


class FakeTrustPlane:
    def __init__(self) -> None:
        self.permit_bodies: dict[str, bytes] = {}
        self.permit_posts: list[bytes] = []
        self.fail_permits = False

    def permit_payload(self) -> dict:
        return {
            "permit_id": "permit-1",
            "issuer_wallet_id": "wallet-1",
            "subject_wallet_id": "wallet-1",
            "subject_key_id": "key-1",
            "scopes": ["tool:partner.search:invoke", "billing:charge"],
            "allowed_tools": ["partner.search"],
            "max_credits": "100",
            "spent_credits": "0",
            "expires_at": datetime.now(UTC).isoformat(),
            "nonce": "nonce-1",
            "status": "active",
            "signature": "sig-permit-1",
            "key_id": "key-1",
            "issued_at": datetime.now(UTC).isoformat(),
            "revoked_at": None,
        }

    def receipt_payload(self) -> dict:
        return {
            "receipt_id": "receipt-success",
            "permit_id": "permit-1",
            "wallet_id": "wallet-1",
            "key_id": "key-1",
            "tool": "partner.search",
            "request_hash": "request-hash",
            "response_hash": "response-hash",
            "ledger_entry_id": "ledger-1",
            "dispatch_attempt_id": "dispatch-1",
            "credits_authorized": "2",
            "credits_charged": "2",
            "outcome": "success",
            "audit_event_id": "audit-1",
            "created_at": datetime.now(UTC).isoformat(),
            "signature": "sig-success",
            "signature_key_id": "signing-key-1",
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            self.permit_posts.append(request.content)
            if self.fail_permits:
                raise httpx.ConnectError("connection dropped", request=request)
            key = request.headers["idempotency-key"]
            first = self.permit_bodies.setdefault(key, request.content)
            if json.loads(first) != json.loads(request.content):
                return httpx.Response(
                    409,
                    json={
                        "error": "idempotency_key_reused",
                        "message": "idempotency_key_reused",
                    },
                )
            return httpx.Response(201, json=self.permit_payload())
        if request.url.path == "/mcp/messages":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "{}"}],
                        "structuredContent": {},
                        "isError": False,
                        "receipt": self.receipt_payload(),
                    },
                },
            )
        return httpx.Response(404)


def _tool(server: FakeTrustPlane, **kwargs) -> CrewAIB2ATool:
    tool = CrewAIB2ATool(api_key="test-key", wallet_id="wallet-1", **kwargs)
    tool.client = B2AClient(
        api_key="test-key", transport=httpx.MockTransport(server.handler)
    )
    return tool


async def _call(tool: CrewAIB2ATool) -> str:
    try:
        return await tool._arun(
            operation="call_tool",
            tool_name="partner.search",
            idempotency_key="call-key-1",
            permit_idempotency_key="permit-key-1",
            arguments={"q": "x"},
        )
    finally:
        await tool.client.close()


@pytest.mark.asyncio
async def test_restart_with_store_replays_the_same_permit_body(tmp_path):
    server = FakeTrustPlane()
    path = tmp_path / "permits.json"

    first = await _call(_tool(server, permit_store=JsonFilePermitStore(path)))
    second = await _call(_tool(server, permit_store=JsonFilePermitStore(path)))

    assert "receipt-success" in first
    assert first == second
    assert len(server.permit_posts) == 2
    assert json.loads(server.permit_posts[0]) == json.loads(server.permit_posts[1])


@pytest.mark.asyncio
async def test_restart_without_store_still_conflicts():
    server = FakeTrustPlane()
    await _call(_tool(server))
    with pytest.raises(IdempotencyConflictError):
        await _call(_tool(server))


@pytest.mark.asyncio
async def test_body_is_stored_before_a_lost_permit_response(tmp_path):
    server = FakeTrustPlane()
    path = tmp_path / "permits.json"
    server.fail_permits = True
    lost = await _call(_tool(server, permit_store=JsonFilePermitStore(path)))
    assert "receipt-success" not in lost
    assert path.exists()

    server.fail_permits = False
    result = await _call(_tool(server, permit_store=JsonFilePermitStore(path)))

    assert "receipt-success" in result
    assert {json.dumps(json.loads(body)) for body in server.permit_posts} == {
        json.dumps(json.loads(server.permit_posts[0]))
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [{"permit_budget": Decimal(50)}, {"permit_ttl_minutes": 5}],
)
async def test_restart_with_changed_terms_is_refused_before_the_network(
    tmp_path, change
):
    server = FakeTrustPlane()
    path = tmp_path / "permits.json"
    await _call(_tool(server, permit_store=JsonFilePermitStore(path)))

    changed = _tool(server, permit_store=JsonFilePermitStore(path), **change)
    result = await _call(changed)
    assert "different permit terms" in result
    assert len(server.permit_posts) == 1


def test_permit_store_is_not_serialized(tmp_path):
    tool = CrewAIB2ATool(
        api_key="test-key",
        wallet_id="wallet-1",
        permit_store=JsonFilePermitStore(tmp_path / "permits.json"),
    )
    assert "permit_store" not in tool.model_dump()
