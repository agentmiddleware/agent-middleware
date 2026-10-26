"""A bad tool call must not mint a permit."""

from __future__ import annotations

import httpx
import pytest

from langchain_b2a import B2AClient
from langchain_b2a.tools import get_mcp_tools


@pytest.mark.asyncio
async def test_blank_tool_name_and_bad_arguments_do_not_mint_a_permit():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"detail": "should not be called"})

    client = B2AClient(
        api_key="test-key",
        base_url="http://127.0.0.1:9",
        transport=httpx.MockTransport(handler),
    )
    tool = get_mcp_tools(client, wallet_id="wallet-1")[0]
    try:
        with pytest.raises(ValueError, match="tool_name"):
            await tool.ainvoke(
                {
                    "tool_name": " ",
                    "idempotency_key": "invoke-1",
                    "permit_idempotency_key": "permit-1",
                    "arguments": {},
                }
            )
        coroutine = tool.coroutine
        assert coroutine is not None
        with pytest.raises(ValueError, match="arguments"):
            await coroutine(
                tool_name="partner.search",
                idempotency_key="invoke-1",
                permit_idempotency_key="permit-1",
                arguments=[],
            )
        assert seen == []
    finally:
        await client.close()
