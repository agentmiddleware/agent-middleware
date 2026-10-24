#!/usr/bin/env python3
"""Exercise a sandbox payout through the real gateway and offline verifier."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = EXAMPLE_DIR / "output"
TOOL = "sandbox.payout.send"
RETRY_KEY = "sandbox-invoice-4417"


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
            "PUBLIC_URL": "http://sandbox",
            "VALID_API_KEYS": admin_key,
            "TRUST_MODE_ENABLED": "true",
            "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
            "ENABLE_PROOF_SURFACES": "false",
            "ENABLE_DEMO_TENANT": "false",
            "TRUST_SIGNING_KEY_ID": "sandbox-payout-ed25519",
            "TRUST_SIGNING_PRIVATE_KEY_B64": base64.b64encode(signing_seed).decode(),
            "RATE_LIMIT_PER_MINUTE": "1000000",
        }
    )
    return admin_key


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "sdk" / "python" / "src"))

import httpx  # noqa: E402

from b2a_sdk.receipt_verifier import (  # noqa: E402
    key_set_from_document,
    verify_bundle,
)


class LoseFirstPayoutResponse(httpx.AsyncBaseTransport):
    """Let the gateway finish, then hide its first payout reply from the caller."""

    def __init__(self, app: Any) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.dropped_result: dict[str, Any] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.url.path == "/mcp/messages" and self.dropped_result is None:
            await response.aread()
            if response.status_code != 200:
                raise AssertionError(f"first payout status: {response.status_code}")
            self.dropped_result = response.json()["result"]
            await response.aclose()
            raise httpx.ReadError("simulated response loss", request=request)
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


async def run(admin_key: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    from app.db.database import close_db, init_db
    from app.main import app
    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry

    dispatches: list[dict[str, str]] = []

    def send_payout(vendor: str, invoice: str, amount_usd: str) -> dict[str, str]:
        dispatches.append(
            {"vendor": vendor, "invoice": invoice, "amount_usd": amount_usd}
        )
        return {"confirmation": "SANDBOX-0001", "status": "simulated"}

    await init_db()
    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Sandbox payout",
        description="Record a simulated vendor payout in local memory",
        category=ServiceCategory.AGENT_COMMS,
        func=send_payout,
        credits_per_unit=1.0,
        require_permit=True,
    )
    try:
        transport = LoseFirstPayoutResponse(app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://sandbox"
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
                    "agent_id": "sandbox-agent",
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
                    "key_name": "sandbox-agent",
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
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                },
                {**admin_headers, "Idempotency-Key": "sandbox-permit-4417"},
                201,
            )
            body = {
                "jsonrpc": "2.0",
                "id": "sandbox-payout",
                "method": "tools/call",
                "params": {
                    "name": TOOL,
                    "arguments": {
                        "vendor": "Example Supply",
                        "invoice": "INV-4417",
                        "amount_usd": "25.00",
                    },
                    "mcpContext": {
                        "wallet_id": wallet_id,
                        "permit_id": permit["permit_id"],
                        "idempotency_key": RETRY_KEY,
                    },
                },
            }
            try:
                await client.post("/mcp/messages", json=body, headers=agent_headers)
            except httpx.ReadError as exc:
                if str(exc) != "simulated response loss":
                    raise AssertionError("unexpected response loss") from exc
            else:
                raise AssertionError("the first response was not lost")

            original = transport.dropped_result
            if original is None or original["isError"] is not False:
                raise AssertionError("original payout did not succeed")
            if len(dispatches) != 1:
                raise AssertionError("original payout did not dispatch exactly once")

            replay = await post(client, "/mcp/messages", body, agent_headers, 200)
            result = replay["result"]
            if result["isError"] is not False:
                raise AssertionError("replayed payout failed")
            if result["receipt"] != original["receipt"]:
                raise AssertionError("replay returned a different receipt")
            if result["content"] != original["content"]:
                raise AssertionError("replay returned a different outcome")
            if len(dispatches) != 1:
                raise AssertionError("replay dispatched the payout again")

            changed = json.loads(json.dumps(body))
            changed["params"]["arguments"]["amount_usd"] = "50.00"
            conflict = await post(client, "/mcp/messages", changed, agent_headers, 200)
            if conflict["error"]["message"] != "idempotency_key_reused":
                raise AssertionError("changed payout was not rejected")
            if len(dispatches) != 1:
                raise AssertionError("changed payout dispatched again")

            receipt_id = result["receipt"]["receipt_id"]
            portable_response = await client.get(
                f"/v1/receipts/{receipt_id}/portable", headers=agent_headers
            )
            if portable_response.status_code != 200:
                raise AssertionError("portable receipt request failed")
            keys_response = await client.get("/.well-known/trust-keys.json")
            if keys_response.status_code != 200:
                raise AssertionError("trust keys request failed")
            portable = portable_response.json()
            keys = keys_response.json()
            if portable["receipt_id"] != receipt_id:
                raise AssertionError("portable receipt ID differs")
            if keys["alg"] != "Ed25519":
                raise AssertionError("unexpected trust key algorithm")

        if len(dispatches) != 1:
            raise AssertionError("payout dispatched more than once")
        return portable, keys, len(dispatches)
    finally:
        registry.unregister_local(TOOL)
        await close_db()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix=".sandbox-", dir=EXAMPLE_DIR) as state:
        admin_key = configure_sandbox(Path(state) / "gateway.db")
        portable, keys, dispatch_count = asyncio.run(run(admin_key))

    verification = verify_bundle(
        portable, key_set_from_document(keys), expected_issuer="http://sandbox"
    )
    if not verification.ok:
        raise AssertionError(verification.reason)
    if verification.claims["tool"] != TOOL:
        raise AssertionError("verified receipt claims the wrong tool")

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "receipt-bundle.json").write_text(
        json.dumps(portable, indent=2) + "\n"
    )
    (OUTPUT_DIR / "trust-keys.json").write_text(json.dumps(keys, indent=2) + "\n")
    print(
        "a retry with the same key returns the original receipt instead of dispatching again"
    )
    print(f"Provider dispatches: {dispatch_count}")
    print("Offline Ed25519 receipt verification: verified")
    print("Receipt: examples/retry-safe-refund/output/receipt-bundle.json")
    print("Public keys: examples/retry-safe-refund/output/trust-keys.json")


if __name__ == "__main__":
    main()
