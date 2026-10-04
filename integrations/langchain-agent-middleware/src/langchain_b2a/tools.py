"""LangChain tools from Agent Middleware API."""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from .client import B2AClient


class MCPToolInput(BaseModel):
    """Input for MCP tool."""

    tool_name: str
    idempotency_key: str
    permit_idempotency_key: str
    arguments: dict[str, Any]


def get_mcp_tools(
    client: B2AClient,
    *,
    wallet_id: str,
    permit_budget: Decimal = Decimal(100),
    permit_ttl_minutes: int = 30,
) -> list[BaseTool]:
    """Get LangChain tools from MCP registry via governed permit→invoke→receipt flow.

    Args:
        client: B2AClient instance connected to Agent Middleware API
        wallet_id: Wallet ID for billing
        permit_budget: Maximum credits per permit (default 100)
        permit_ttl_minutes: Permit lifetime in minutes (default 30)

    Returns:
        List of LangChain BaseTool instances

    Example:
        client = B2AClient(api_key="...")
        tools = get_mcp_tools(client, wallet_id="agent-001")
        for t in tools:
            result = await t.ainvoke({
                "tool_name": "my_tool",
                "idempotency_key": "unique-key-123",
                "arguments": {...}
            })
    """
    from ._tools import create_mcp_tool

    return [
        create_mcp_tool(
            client,
            wallet_id=wallet_id,
            permit_budget=permit_budget,
            permit_ttl_minutes=permit_ttl_minutes,
        )
    ]


def get_langgraph_tools(
    client: B2AClient,
    *,
    wallet_id: str,
    permit_budget: Decimal = Decimal(100),
    permit_ttl_minutes: int = 30,
) -> list[Callable]:
    """Get tools formatted for LangGraph via governed permit→invoke→receipt flow.

    Args:
        client: B2AClient instance connected to Agent Middleware API
        wallet_id: Wallet ID for billing
        permit_budget: Maximum credits per permit (default 100)
        permit_ttl_minutes: Permit lifetime in minutes (default 30)

    Returns:
        List of callable tools compatible with LangGraph

    Example:
        from langgraph.prebuilt import create_react_agent
        client = B2AClient(api_key="...")
        tools = get_langgraph_tools(client, wallet_id="agent-001")
        agent = create_react_agent(model, tools)
    """
    from ._tools import create_langgraph_tools

    return create_langgraph_tools(
        client,
        wallet_id=wallet_id,
        permit_budget=permit_budget,
        permit_ttl_minutes=permit_ttl_minutes,
    )
