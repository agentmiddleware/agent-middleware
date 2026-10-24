# LlamaIndex Integration

Use Agent Middleware API tools with LlamaIndex agents.

## Installation

No PyPI package is published. The SDK distribution will be published as
`amw-sdk` (`pip install amw-sdk`). For now, work from a checkout of this
repository:

```bash
git clone https://github.com/PetrefiedThunder/agent-middleware.git
cd agent-middleware
python -m pip install -r gateway/requirements.txt -e ./sdk/python
export PYTHONPATH="$PWD/integrations"
python -m pip install llama-index
```

Import from `framework_integrations` (the module in this repository); there is
no `agent_middleware` package.

## Quick Start

```python
from llama_index.core.agent import ReActAgent
from framework_integrations import B2AClient, get_llamaindex_tools

# Initialize client
client = B2AClient(
    api_url="http://localhost:8000",
    api_key="your-api-key",
    wallet_id="your-wallet-id"
)

# Get LlamaIndex-compatible tools
tools = get_llamaindex_tools(client)

# Create agent
agent = ReActAgent.from_tools(tools, llm=llm, verbose=True)

# Use the agent (the tools are async: B2AClient is an async client)
result = await agent.achat("Check my balance and emit a telemetry event")
```

Each tool is a `FunctionTool` built from an async function, so `acall` awaits
the request in your event loop. Prefer the async agent entry points.

## Available Tools

| Tool | Description | Credits |
|------|-------------|---------|
| `emit_telemetry` | Track agent events | 1 |
| `get_balance` | Check wallet balance | 0 |
| `send_message` | Message another agent | 1 |
| `ai_decide` | Make AI decision | 10 |
| `self_heal` | Diagnose and fix issues | 15 |
| `awi_session` | Start web automation | 5 |

## Example: Data Processing Agent

```python
from llama_index.core.agent import ReActAgent

tools = get_llamaindex_tools(client)

agent = ReActAgent.from_tools(
    tools,
    llm=llm,
    system_prompt="You are a data processing agent. Use tools to handle data."
)

result = await agent.achat("Process the uploaded dataset")
```

## Example: Query Engine with Tools

```python
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.agent import FnRetriever

# Combine retrieval with B2A tools
tools = get_llamaindex_tools(client)

agent = ReActAgent.from_tools(tools, llm=llm)
result = await agent.achat("Find documents and summarize them")
```
