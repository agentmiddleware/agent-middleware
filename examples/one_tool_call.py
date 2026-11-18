"""Minimal permit for one tool call with offline receipt verification.

This example shows the smallest real permit-governed MCP tool invocation:
start a local gateway, create a permit for one tool with a budget, call the
tool once, then verify the receipt offline with Receipt.verify().

Run from the repository root:

    cd agent-middleware/gateway
    PYTHONPATH=../sdk/python/src uv run --no-project --isolated --python 3.12 \\
      --with-requirements requirements.txt \\
      --with-editable '../sdk/python[verify]' \\
      python ../examples/one_tool_call.py

Expected output (receipt IDs differ on each run):

    Receipt ID: rcpt-0d4e8c5a1b2f3967
    Tool: partner.notes.write
    Outcome: success
    Credits charged: 2
    Verification: valid

The example runs the gateway in-process (SQLite, memory state) with one tool
(partner.notes.write) and verifies the receipt with no server call.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Insert the SDK source and gateway on sys.path for local runs.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (_REPO_ROOT / "sdk" / "python" / "src", _REPO_ROOT / "gateway"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


async def main() -> int:
    """Start gateway, issue permit, call one tool, verify receipt."""
    # Configure environment before any gateway imports.
    with tempfile.TemporaryDirectory(prefix=".one-tool-") as state:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        database = Path(state) / "gateway.db"
        signing_seed = Ed25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        admin_key = secrets.token_urlsafe(32)
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
                "STATE_BACKEND": "memory",
                "ENVIRONMENT": "local",
                "PUBLIC_URL": "http://sandbox",
                "VALID_API_KEYS": admin_key,
                "TRUST_MODE_ENABLED": "true",
                "TRUST_SIGNING_KEY_ID": "one-tool-ed25519",
                "TRUST_SIGNING_PRIVATE_KEY_B64": base64.b64encode(signing_seed).decode(),
                "ENABLE_DOGFOOD_TOOL": "true",  # Registers partner.notes.write
                "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
                "MCP_UPSTREAM_ENABLED": "false",
            }
        )

        # Import gateway modules after environment is configured.
        from app.db.database import close_db, init_db
        from app.main import app
        from amw import Receipt, key_set_from_document
        import httpx

        await init_db()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://sandbox"
            ) as client:
                # Create wallets and mint an API key.
                admin = {"X-API-Key": admin_key}
                run_id = secrets.token_hex(6)
                sponsor_resp = await client.post(
                    "/v1/billing/wallets/sponsor",
                    json={
                        "sponsor_name": f"one-tool-{run_id}",
                        "email": f"one-tool-{run_id}@example.com",
                        "initial_credits": 10,
                        "require_kyc": False,
                    },
                    headers=admin,
                )
                agent_resp = await client.post(
                    "/v1/billing/wallets/agent",
                    json={
                        "sponsor_wallet_id": sponsor_resp.json()["wallet_id"],
                        "agent_id": f"one-tool-agent-{run_id}",
                        "budget_credits": 5,
                        "daily_limit": 5,
                    },
                    headers=admin,
                )
                wallet_id = agent_resp.json()["wallet_id"]
                key_resp = await client.post(
                    "/v1/api-keys",
                    json={
                        "wallet_id": wallet_id,
                        "key_name": f"one-tool-{run_id}",
                        "expires_in_days": 1,
                    },
                    headers=admin,
                )
                agent_key = key_resp.json()["api_key"]

                # Issue a permit for one tool with a 3-credit budget.
                tool_name = "partner.notes.write"
                permit_resp = await client.post(
                    "/v1/permits",
                    json={
                        "issuer_wallet_id": wallet_id,
                        "subject_wallet_id": wallet_id,
                        "subject_key_id": key_resp.json()["key_id"],
                        "allowed_tools": [tool_name],
                        "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                        "max_credits": 3,
                        "expires_at": (
                            datetime.now(UTC) + timedelta(minutes=5)
                        ).isoformat(),
                    },
                    headers={**admin, "Idempotency-Key": f"one-tool-permit-{run_id}"},
                )
                permit_id = permit_resp.json()["permit_id"]

                # Call the tool once through the governed MCP endpoint.
                # partner.notes.write argument is "text", not "note", and charges 2 credits.
                call_resp = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": "call-1",
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": {"text": "one tool call example"},
                            "mcpContext": {
                                "wallet_id": wallet_id,
                                "permit_id": permit_id,
                                "idempotency_key": f"one-tool-{run_id}",
                            },
                        },
                    },
                    headers={"X-API-Key": agent_key},
                )
                result = call_resp.json().get("result", {})
                if result.get("isError"):
                    print(f"Tool call failed: {call_resp.json()}", file=sys.stderr)
                    return 1

                # Extract the receipt from the result.
                receipt_dict = result.get("receipt", {})
                receipt_id = receipt_dict.get("receipt_id")

                # Fetch the portable bundle and trust keys for offline verification.
                bundle_resp = await client.get(
                    f"/v1/receipts/{receipt_id}/portable",
                    headers={"X-API-Key": agent_key},
                )
                keys_resp = await client.get("/.well-known/trust-keys.json")

                # Verify the receipt offline with Receipt.verify().
                receipt = Receipt.from_dict(bundle_resp.json())
                verification = receipt.verify(
                    bundle=bundle_resp.json(), keys=keys_resp.json()
                )

                # Print the result.
                print(f"Receipt ID: {receipt_id}")
                print(f"Tool: {receipt_dict.get('tool')}")
                print(f"Outcome: {receipt_dict.get('outcome')}")
                print(f"Credits charged: {receipt_dict.get('credits_charged')}")
                print(f"Verification: {'valid' if verification.ok else 'INVALID'}")

                return 0 if verification.ok else 1
        finally:
            await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
