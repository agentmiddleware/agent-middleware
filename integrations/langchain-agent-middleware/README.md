# LangChain + Agent Middleware API

LangChain integration for the Agent Middleware API via governed **permit → invoke → receipt** flow.

All tool invocations go through the trust plane: scoped permits, signed receipts, replay protection, and metered billing.

**Status:** source-only integration example, not a published package. Start
with the [documentation guide](../../gateway/docs/README.md) to evaluate the supported
one-tool MCP path before adopting a framework wrapper.

## Installation

This package is not published to PyPI. Install it from a checkout of
this repository:

```bash
git clone https://github.com/agentmiddleware/agent-middleware.git
cd agent-middleware
python -m pip install -e ./sdk/python
python -m pip install -e integrations/langchain-agent-middleware
```

`b2a_sdk` remains an import package. The SDK distribution will be published
as `amw-sdk` (`pip install amw-sdk`). Until then, install it from the local
path first because this wrapper depends on `amw-sdk>=0.4.0` and cannot
resolve that dependency from PyPI. Installing the wrapper provides the
`langchain_b2a` module used below.

## Quick Start (Governed Flow)

```python
from langchain_b2a import B2AClient, get_langgraph_tools
from langgraph.prebuilt import create_react_agent

# Initialize client
client = B2AClient(api_key="your-api-key")

# Get LangGraph-compatible tools with wallet_id
tools = get_langgraph_tools(client, wallet_id="agent-001")

# Create agent
model = ChatOpenAI(model="gpt-4o")
agent = create_react_agent(model, tools)
```

## MCP Tools via Governed Flow

```python
from langchain_b2a import B2AClient, get_mcp_tools

client = B2AClient(api_key="...")
mcp_tools = get_mcp_tools(client, wallet_id="agent-001")
tool = mcp_tools[0]

# Call an MCP tool with caller-supplied idempotency keys
# The wrapper creates a permit, invokes the tool, and returns a signed receipt
result = await tool.ainvoke(
    {
        "tool_name": "data-indexer",
        "idempotency_key": "unique-invoke-123",  # REQUIRED: caller must supply
        "permit_idempotency_key": "permit-invoke-123",  # REQUIRED: stable for replay
        "arguments": {"documents": ["..."]},
    }
)

# Result includes signed receipt
# {'content': [...], 'receipt_id': '...', 'credits_charged': '2', 'signature': '...'}
```

## Idempotency and Replay Protection

Both `idempotency_key` and `permit_idempotency_key` are **required** and must be supplied by the caller. Do not auto-generate keys.

An identical replay with the same invocation key returns the original receipt
without recharging. `idempotency_key` identifies one governed invocation, and
the gateway rejects that key reused with changed invocation input with an
idempotency conflict (`IdempotencyConflictError`; on `/mcp/messages` the gateway
reports it as a JSON-RPC `-32603` `idempotency_key_reused` error rather than an
HTTP 409). `permit_idempotency_key` makes permit creation
repeatable; it does not make a changed invocation an idempotent replay.

```python
# First call: charges credits
result1 = await tool.ainvoke(
    {
        "tool_name": "partner.search",
        "idempotency_key": "search-abc-123",
        "permit_idempotency_key": "permit-abc-123",
        "arguments": {"query": "test"},
    }
)

# Valid replay: same request returns the cached receipt, no additional charge
result2 = await tool.ainvoke(
    {
        "tool_name": "partner.search",
        "idempotency_key": "search-abc-123",  # same invoke key
        "permit_idempotency_key": "permit-abc-123",  # same permit key
        "arguments": {"query": "test"},  # same arguments
    }
)
```

## Permit Configuration

Control permit budget and TTL:

```python
from decimal import Decimal

tools = get_mcp_tools(
    client,
    wallet_id="agent-001",
    permit_budget=Decimal("50"),  # max 50 credits per permit
    permit_ttl_minutes=15,  # permit expires in 15 minutes
)
```

## Requirements

- Python 3.11+
- LangChain 0.1.0+
- httpx 0.25.0+
