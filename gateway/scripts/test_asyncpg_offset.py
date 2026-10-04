"""Direct asyncpg integration test for the offset regression.

Runs outside pytest to avoid the event-loop-per-fixture issue that
breaks asyncpg connection pools across test boundaries.
"""

import asyncio
import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Generate a fresh key
key = Ed25519PrivateKey.generate()
raw = key.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)

os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:pg@localhost:5433/postgres"
os.environ["STATE_BACKEND"] = "postgres"
os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = base64.b64encode(raw).decode()
os.environ["TRUST_MODE_ENABLED"] = "true"
os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
os.environ["ENABLE_DOGFOOD_TOOL"] = "true"
os.environ["VALID_API_KEYS"] = "pg-test-key"

import httpx

from app.core import config as config_module
from app.db import database as database_module

config_module.get_settings.cache_clear()
database_module._engine = None
database_module._session_factory = None

import importlib
import app.main as main_module

importlib.reload(main_module)


async def run():
    app = main_module.app
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://pg-test"
    ) as client:
        headers = {"X-API-Key": "pg-test-key"}

        # 1. Create sponsor wallet
        sponsor = await client.post(
            "/v1/billing/wallets/sponsor",
            headers=headers,
            json={
                "sponsor_name": "PG Regression",
                "email": "pg@example.com",
                "initial_credits": 1000,
            },
        )
        assert sponsor.status_code == 201, f"sponsor: {sponsor.text}"
        sponsor_id = sponsor.json()["wallet_id"]
        print(f"✅ sponsor: {sponsor_id}")

        # 2. Create agent wallet
        agent = await client.post(
            "/v1/billing/wallets/agent",
            headers=headers,
            json={
                "sponsor_wallet_id": sponsor_id,
                "agent_id": "agt-pg-regression",
                "budget_credits": 100,
            },
        )
        assert agent.status_code == 201, f"agent: {agent.text}"
        agent_id = agent.json()["wallet_id"]
        print(f"✅ agent: {agent_id}")

        # 3. Create permit with +05:00 offset
        permit = await client.post(
            "/v1/permits",
            headers={**headers, "Idempotency-Key": "pg-permit-offset"},
            json={
                "issuer_wallet_id": sponsor_id,
                "subject_wallet_id": agent_id,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": "10",
                "expires_at": "2099-01-01T00:00:00+05:00",
            },
        )
        assert permit.status_code == 201, f"permit: {permit.text}"
        body = permit.json()
        permit_id = body["permit_id"]
        print(f"✅ permit: {permit_id}")

        # 4. Assert normalized UTC
        assert body["expires_at"] == "2098-12-31T19:00:00", f"got {body['expires_at']}"
        assert "+" not in body["expires_at"]
        print(f"✅ expires_at normalized: {body['expires_at']}")

        # 5. Assert signature present
        assert body["signature"]
        print("✅ signature present")

        # 6. Governed invocation
        invoke = await client.post(
            "/mcp/messages",
            headers={**headers, "Idempotency-Key": "pg-invoke-offset"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "offset regression"},
                    "mcpContext": {"wallet_id": agent_id, "permit_id": permit_id},
                },
            },
        )
        assert invoke.status_code == 200, f"invoke: {invoke.text}"
        result = invoke.json()
        assert "error" not in result, result
        assert result["result"]["receipt"]["receipt_id"]
        print(
            f"✅ governed invoke succeeded, receipt: {result['result']['receipt']['receipt_id']}"
        )

        # 7. Idempotency replay (same key, same payload → same receipt)
        replay = await client.post(
            "/mcp/messages",
            headers={**headers, "Idempotency-Key": "pg-invoke-offset"},
            json={
                "jsonrpc": "2.0",
                "id": 3,  # same id as original for identical hash
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "offset regression"},
                    "mcpContext": {"wallet_id": agent_id, "permit_id": permit_id},
                },
            },
        )
        assert replay.status_code == 200, f"replay: {replay.text}"
        replay_body = replay.json()
        assert (
            replay_body["result"]["receipt"]["receipt_id"]
            == result["result"]["receipt"]["receipt_id"]
        )
        print("✅ idempotency replay returned same receipt")

        # 8. Fresh idempotency key with different payload → new receipt
        replay2 = await client.post(
            "/mcp/messages",
            headers={**headers, "Idempotency-Key": "pg-invoke-offset"},
            json={
                "jsonrpc": "2.0",
                "id": 99,  # different id → different hash → idempotency_key_reused
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "different text"},
                    "mcpContext": {"wallet_id": agent_id, "permit_id": permit_id},
                },
            },
        )
        assert replay2.status_code == 200, f"replay2: {replay2.text}"
        replay2_body = replay2.json()
        assert replay2_body["error"]["message"] == "idempotency_key_reused"
        print("✅ different payload with same idempotency key correctly rejected")
        print("✅ different payload with same idempotency key correctly rejected")

        # 8. Denial without permit
        denial = await client.post(
            "/mcp/messages",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "denied"},
                    "mcpContext": {"wallet_id": agent_id},
                },
            },
        )
        assert denial.status_code == 200, f"denial: {denial.text}"
        error = denial.json()["error"]
        assert error["code"] == -32003, f"got {error['code']}"
        assert error["message"] == "permit_required"
        print("✅ unpermitted call denied with permit_required")

    print("\n🎉 All asyncpg integration checks passed!")


if __name__ == "__main__":
    asyncio.run(run())
