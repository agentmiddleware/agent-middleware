"""Run a LangGraph tool through permits, replay and offline receipt verification."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from b2a_sdk import AgentMiddlewareClient
from b2a_sdk.edge_client import GovernedEdgeSession
from b2a_sdk.errors import PermitDeniedError
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from framework_integrations.langgraph_middleware import LangGraphGovernedTools
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from mock_tool import append_note, append_note_stub, delete_note_stub, notes

EXAMPLE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = EXAMPLE_DIR / "output"
TOOL = "demo.notes.append"
RETRY_KEY = "demo-note-0001"


def configure_sandbox(database: Path) -> str:
    signing_seed = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    admin_key = secrets.token_urlsafe(32)
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "STATE_BACKEND": "memory",
            "REDIS_URL": "",
            "ENVIRONMENT": "local",
            "PUBLIC_URL": "http://127.0.0.1",
            "VALID_API_KEYS": admin_key,
            "TRUST_MODE_ENABLED": "true",
            "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
            "ENABLE_PROOF_SURFACES": "false",
            "ENABLE_DEMO_TENANT": "false",
            "TRUST_SIGNING_KEY_ID": "local-notes-ed25519",
            "TRUST_SIGNING_PRIVATE_KEY_B64": base64.b64encode(signing_seed).decode(),
            "RATE_LIMIT_PER_MINUTE": "1000000",
        }
    )
    return admin_key


class GatewayTransport(httpx.AsyncBaseTransport):
    """Count real gateway requests and retain their receipt summaries."""

    def __init__(self, app: Any) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []
        self.receipts: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "127.0.0.1":
            raise AssertionError("only the local gateway is allowed")
        self.requests.append(request)
        response = await self.inner.handle_async_request(request)
        await response.aread()
        if request.url.path == "/mcp/messages":
            receipt = response.json().get("result", {}).get("receipt")
            if receipt:
                self.receipts.append(receipt)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


async def post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    headers: dict[str, str],
    expected_status: int,
) -> dict[str, Any]:
    response = await client.post(path, json=body, headers=headers)
    if response.status_code != expected_status:
        raise AssertionError(f"{path}: {response.status_code}")
    return response.json()


async def run(admin_key: str) -> dict[str, Any]:
    from app.db.database import close_db, init_db
    from app.main import app
    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry

    notes.clear()
    await init_db()
    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Local notes",
        description="Append a note to memory",
        category=ServiceCategory.AGENT_COMMS,
        func=append_note,
        credits_per_unit=1.0,
        require_permit=True,
    )
    transport = GatewayTransport(app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            admin_headers = {"X-API-Key": admin_key}
            sponsor = await post(
                client,
                "/v1/billing/wallets/sponsor",
                {
                    "sponsor_name": "Sandbox",
                    "email": "sandbox@example.com",
                    "initial_credits": 100,
                    "require_kyc": False,
                },
                admin_headers,
                201,
            )
            agent = await post(
                client,
                "/v1/billing/wallets/agent",
                {
                    "sponsor_wallet_id": sponsor["wallet_id"],
                    "agent_id": "local-notes-agent",
                    "budget_credits": 10,
                    "daily_limit": 10,
                },
                admin_headers,
                201,
            )
            wallet_id = agent["wallet_id"]
            key = await post(
                client,
                "/v1/api-keys",
                {
                    "wallet_id": wallet_id,
                    "key_name": "local-notes-agent",
                    "expires_in_days": 1,
                },
                admin_headers,
                201,
            )
            agent_headers = {"X-API-Key": key["api_key"]}
            permit = await post(
                client,
                "/v1/permits",
                {
                    "issuer_wallet_id": wallet_id,
                    "subject_wallet_id": wallet_id,
                    "subject_key_id": key["key_id"],
                    "allowed_tools": [TOOL],
                    "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
                    "max_credits": 1,
                    "expires_at": (
                        datetime.now(UTC) + timedelta(minutes=5)
                    ).isoformat(),
                },
                {**admin_headers, "Idempotency-Key": "demo-permit-0001"},
                201,
            )
            async with AgentMiddlewareClient(
                key["api_key"], base_url="http://127.0.0.1", transport=transport
            ) as sdk:
                session = await GovernedEdgeSession.open(
                    sdk, permit_id=permit["permit_id"], wallet_id=wallet_id
                )
                factory = LangGraphGovernedTools(session)
                tool = factory.as_tool(append_note_stub, tool_name=TOOL)
                builder = StateGraph(MessagesState)
                builder.add_node("tools", ToolNode([tool], handle_tool_errors=False))
                builder.add_edge(START, "tools")
                builder.add_edge("tools", END)
                node = builder.compile()
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": TOOL,
                            "args": {
                                "note": "This note stays in memory",
                                "idempotency_key": RETRY_KEY,
                            },
                            "id": "demo-call-0001",
                            "type": "tool_call",
                        }
                    ],
                )
                first = await node.ainvoke({"messages": [message]})
                if first["messages"][-1].status != "success":
                    raise AssertionError("LangGraph call failed")
                original = transport.receipts[-1]
                if original["outcome"] != "success" or not original["receipt_id"]:
                    raise AssertionError("governed call failed")
                if len(notes) != 1:
                    raise AssertionError("tool did not run once")
                print(
                    f"First receipt: {original['receipt_id']} outcome: {original['outcome']} credits_charged: {original['credits_charged']}"
                )
                await node.ainvoke({"messages": [message]})
                replay = transport.receipts[-1]
                calls = [
                    json.loads(request.content)
                    for request in transport.requests
                    if request.url.path == "/mcp/messages"
                ]
                if len(calls) != 2 or len(transport.receipts) != 2:
                    raise AssertionError("expected two governed HTTP calls")
                for call in calls:
                    params = call["params"]
                    if params["mcpContext"]["idempotency_key"] != RETRY_KEY:
                        raise AssertionError("LangGraph discarded the explicit key")
                    if params["arguments"] != {"note": "This note stays in memory"}:
                        raise AssertionError("unexpected gateway tool arguments")
                if replay != original or len(notes) != 1:
                    raise AssertionError("replay changed the receipt or ran the tool")
                print(f"Replay receipt: {replay['receipt_id']}")

                denied_tool = factory.as_tool(
                    delete_note_stub, tool_name="demo.notes.delete"
                )
                before = len(transport.requests)
                try:
                    await denied_tool.ainvoke(
                        {
                            "note": "This note stays in memory",
                            "idempotency_key": "demo-delete-0001",
                        }
                    )
                except PermitDeniedError as exc:
                    if exc.reason != "permit_tool_not_allowed":
                        raise AssertionError(exc.reason) from exc
                    local_reason = exc.reason
                else:
                    raise AssertionError("outside permit call succeeded")
                local_requests = len(transport.requests) - before
                if local_requests != 0:
                    raise AssertionError("local denial made an HTTP request")
                print(f"Local denial: {local_reason}; HTTP requests: {local_requests}")

            denied = await post(
                client,
                "/mcp/messages",
                {
                    "jsonrpc": "2.0",
                    "id": "demo-no-permit",
                    "method": "tools/call",
                    "params": {
                        "name": TOOL,
                        "arguments": {"note": "Not authorized"},
                        "mcpContext": {
                            "wallet_id": wallet_id,
                            "idempotency_key": "demo-no-permit-0001",
                        },
                    },
                },
                agent_headers,
                200,
            )
            error = denied["error"]
            if error["code"] != -32003 or error["message"] != "permit_required":
                raise AssertionError(f"unexpected permit denial: {error}")
            print(f"No permit denial: {error['code']} {error['message']}")
            if len(notes) != 1:
                raise AssertionError("denied call ran the tool")
            receipt_id = original["receipt_id"]
            portable_response = await client.get(
                f"/v1/receipts/{receipt_id}/portable", headers=agent_headers
            )
            portable_response.raise_for_status()
            keys_response = await client.get("/.well-known/trust-keys.json")
            keys_response.raise_for_status()
            portable, keys = portable_response.json(), keys_response.json()
            verification = verify_bundle(
                portable,
                key_set_from_document(keys),
                expected_issuer="http://127.0.0.1",
            )
            if not verification.ok or verification.claims["tool"] != TOOL:
                raise AssertionError("offline receipt verification failed")
            print("Offline Ed25519 receipt verification: verified")
            print(f"Tool executions: {len(notes)}")
            return {
                "portable": portable,
                "keys": keys,
                "original": original,
                "replay": replay,
                "executions": len(notes),
                "denial": error,
                "local_requests": local_requests,
                "local_reason": local_reason,
            }
    finally:
        registry.unregister_local(TOOL)
        await close_db()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix=".sandbox-", dir=EXAMPLE_DIR) as state:
        admin_key = configure_sandbox(Path(state) / "gateway.db")
        result = asyncio.run(run(admin_key))
    OUTPUT_DIR.mkdir(exist_ok=True)
    for name, data in (
        ("receipt-bundle.json", result["portable"]),
        ("trust-keys.json", result["keys"]),
    ):
        (OUTPUT_DIR / name).write_text(json.dumps(data, indent=2) + "\n")
    print("Receipt: examples/langgraph-permit/output/receipt-bundle.json")
    print("Public keys: examples/langgraph-permit/output/trust-keys.json")


if __name__ == "__main__":
    main()
