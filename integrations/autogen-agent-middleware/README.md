# AutoGen + Agent Middleware API

AutoGen integration for the Agent Middleware API via governed **permit → invoke → receipt** flow.

All tool invocations go through the trust plane: scoped permits, signed receipts, replay protection, and metered billing.

**Status:** source-only integration example, not a published package. Start
with the [documentation guide](../../gateway/docs/README.md) to evaluate the supported
one-tool MCP path before adopting a framework wrapper.

## Installation

This package is not published to PyPI. Install it from a checkout of
this repository:

```bash
git clone https://github.com/PetrefiedThunder/agent-middleware.git
cd agent-middleware
python -m pip install -e ./sdk/python
python -m pip install -e integrations/autogen-agent-middleware
```

`b2a_sdk` must be installed from the local path first: this package
depends on `b2a-sdk>=0.4.0`, which is not on PyPI, so installing the
wrapper on its own fails to resolve. That installs the `autogen_b2a`
module used below.

The framework dependency is the `autogen` distribution on PyPI
([AG2 Classic](https://github.com/ag2ai/ag2classic)), which provides the
`import autogen` namespace and `ConversableAgent` that this wrapper targets.
It is installed automatically. Do not substitute `autogen-agentchat` (the
AutoGen 0.4+ rewrite) or `ag2>=1.0` (`import ag2`): both are different APIs
and neither ships the `autogen` module. To run an LLM-backed agent, add the
provider extra for your model, for example:

```bash
python -m pip install "autogen[openai]"
```

## Quick Start (Governed Flow)

```python
import asyncio
from autogen import ConversableAgent, UserProxyAgent
from autogen_b2a import B2AFunctionTool, register_b2a_tools

# Initialize tool with required wallet_id and api_key
b2a_tool = B2AFunctionTool(
    api_key="your-api-key",
    wallet_id="agent-001",
)

# The assistant decides when to call a tool. Its llm_config carries the
# OpenAI-style function schemas so the model can see them.
assistant = ConversableAgent(
    name="assistant",
    system_message="You are a helpful assistant with access to MCP tools.",
    llm_config={
        "config_list": [{"model": "gpt-4o", "api_key": "your-openai-key"}],
        "tools": b2a_tool.get_function_schemas(),
    },
)

# The user proxy executes the tool calls the assistant proposes.
user_proxy = UserProxyAgent(
    name="user",
    human_input_mode="NEVER",
    code_execution_config=False,
)
register_b2a_tools(user_proxy, b2a_tool)


# Run the two-agent chat
async def main():
    result = await user_proxy.a_initiate_chat(
        assistant,
        message="Discover available MCP tools and check the wallet balance",
    )
    print(result.summary)


asyncio.run(main())
```

## Direct Tool Usage (Governed Flow)

```python
import asyncio
from autogen_b2a import B2AFunctionTool

tool = B2AFunctionTool(
    api_key="...",
    wallet_id="agent-001",
)


async def main():
    # Discover tools
    tools = await tool.discover_tools()
    print(f"Available tools: {len(tools)}")

    # Call a tool with caller-supplied idempotency keys
    # The wrapper creates a permit, invokes the tool, and returns a signed receipt
    result = await tool.call_mcp_tool(
        tool_name="data-indexer",
        idempotency_key="unique-invoke-123",  # REQUIRED: caller must supply
        permit_idempotency_key="permit-invoke-123",  # REQUIRED: stable for replay
        arguments={"documents": ["doc1", "doc2"]},
    )
    print(result)
    # Result: {'content': [...], 'receipt_id': '...', 'credits_charged': '2', 'signature': '...'}

    # Check balance
    balance = await tool.get_wallet_balance()
    print(f"Balance: {balance} credits")


asyncio.run(main())
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
async def main():
    tool = B2AFunctionTool(api_key="...", wallet_id="agent-001")

    # First call: charges credits
    result1 = await tool.call_mcp_tool(
        tool_name="partner.search",
        idempotency_key="search-abc-123",
        permit_idempotency_key="permit-abc-123",
        arguments={"query": "test"},
    )

    # Valid replay: same request returns the cached receipt, no additional charge
    result2 = await tool.call_mcp_tool(
        tool_name="partner.search",
        idempotency_key="search-abc-123",  # same invoke key
        permit_idempotency_key="permit-abc-123",  # same permit key
        arguments={"query": "test"},  # same arguments
    )
```

## Permit Configuration

Control permit budget and TTL:

```python
from decimal import Decimal

tool = B2AFunctionTool(
    api_key="your-api-key",
    wallet_id="agent-001",
    permit_budget=Decimal("50"),  # max 50 credits per permit
    permit_ttl_minutes=15,  # permit expires in 15 minutes
)
```

## Requirements

- Python 3.11+
- `autogen` 0.10.0+ (AG2 Classic, the maintained distribution of the legacy
  AutoGen 0.2 API)
- httpx 0.25.0+
