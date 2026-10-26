# LangGraph Integration

Use Agent Middleware API tools directly in LangGraph agents.

## Installation

No PyPI package is published. The SDK distribution will be published as
`amw-sdk` (`pip install amw-sdk`). For now, work from a checkout of this
repository:

```bash
git clone https://github.com/agentmiddleware/agent-middleware.git
cd agent-middleware
python -m pip install -r gateway/requirements.txt -e ./sdk/python
export PYTHONPATH="$PWD/integrations"
python -m pip install langgraph langchain-core
```

Import from `framework_integrations` (the module in this repository); there is
no `agent_middleware` package.

## Quick Start

```python
from langgraph.prebuilt import create_react_agent
from framework_integrations import B2AClient, get_langgraph_tools

# Initialize client
client = B2AClient(
    api_url="http://localhost:8000",
    api_key="your-api-key",
    wallet_id="your-wallet-id",
    agent_id="your-registered-agent-id",  # Required for send_message
)

# Get LangGraph-compatible tools
tools = get_langgraph_tools(client)

# Create agent
agent = create_react_agent(model, tools)

# Use the agent (the tools are async: B2AClient is an async client)
result = await agent.ainvoke(
    {"messages": ["Check my balance and emit a telemetry event"]}
)
```

The tools are async-only. Drive the agent with `ainvoke`; LangChain refuses a
sync `invoke` of an async-only tool with `NotImplementedError`.

## Available Tools

| Tool | Description | Credits |
|------|-------------|---------|
| `emit_telemetry` | Track agent events | 1 |
| `get_balance` | Check wallet balance | 0 |
| `send_message` | Message another agent | 1 |
| `ai_decide` | Make AI decision | 10 |
| `self_heal` | Diagnose and fix issues | 15 |
| `awi_session` | Start web automation | 5 |

## Example: Research Agent

```python
from langgraph.prebuilt import create_react_agent

tools = get_langgraph_tools(client)

researcher = create_react_agent(
    model,
    tools=tools,
    state_modifier="You are a research agent. Use tools to gather information.",
)

result = await researcher.ainvoke(
    {
        "messages": [
            "Research the latest AI developments and send results to researcher-002"
        ]
    }
)
```

## Example: Autonomous Task Agent

```python
tools = get_langgraph_tools(client)

task_agent = create_react_agent(
    model,
    tools=tools,
    prompt="You autonomously complete tasks. Monitor your budget and heal when needed.",
)

result = await task_agent.ainvoke({"messages": ["Process the pending task queue"]})
```
