"""Call a governed tool through the gateway's standard MCP endpoint, then retry.

Uses the official MCP Python SDK (`pip install "mcp>=1.29,<2"`). Run a local
gateway first (`cd gateway && make quickstart`) and mint a key as shown in
QUICKSTART.md, then:

    AGENT_API_KEY=... python examples/mcp-client/call_with_retry.py

The second call reuses the idempotency key, so the gateway returns the original
receipt instead of dispatching or charging again.

Licensed under Apache-2.0.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

try:  # mcp 2.x ships its own httpx fork
    import httpx2 as httpx
except ImportError:  # mcp 1.x
    import httpx

URL = os.environ.get("AMW_MCP_URL", "http://127.0.0.1:8000/mcp")
TOOL = os.environ.get("AMW_TOOL", "partner.notes.write")


async def call_once(api_key: str, idempotency_key: str) -> dict:
    headers = {"X-API-Key": api_key, "Idempotency-Key": idempotency_key}
    async with (
        httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(30, read=300)
        ) as http_client,
        streamable_http_client(URL, http_client=http_client) as (read, write, *_),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(TOOL, {"text": "hello from an MCP client"})
        return result.model_dump(mode="json")


RECEIPT_META_KEY = "io.agentmiddleware/receipt"


def receipt_id(result: dict) -> str | None:
    """The gateway returns the signed receipt in the tool result's _meta."""
    receipt = (result.get("meta") or result.get("_meta") or {}).get(RECEIPT_META_KEY) or {}
    return receipt.get("receipt_id")


async def main() -> int:
    api_key = os.environ["AGENT_API_KEY"]
    key = f"mcp-example-{uuid.uuid4()}"
    first = await call_once(api_key, key)
    retry = await call_once(api_key, key)  # same key: same receipt, no second charge
    first_id = receipt_id(first)
    retry_id = receipt_id(retry)
    print("first receipt:", first_id, "outcome:", _outcome(first))
    print("retry receipt:", retry_id, "outcome:", _outcome(retry))
    if not first_id or first_id != retry_id:
        print("retry did not return the same receipt")
        return 1
    return 0


def _outcome(result: dict) -> str | None:
    receipt = (result.get("meta") or result.get("_meta") or {}).get(RECEIPT_META_KEY) or {}
    return receipt.get("outcome")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
