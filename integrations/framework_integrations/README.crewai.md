# CrewAI Integration

Use Agent Middleware API tools in CrewAI agents and crews.

## Status: use the governed wrapper

`framework_integrations.get_crewai_tools` is not supported and raises
`NotImplementedError`. CrewAI executes every tool through a synchronous
`invoke` that runs an async tool body on a fresh event loop per call
(`asyncio.run`), and the async `B2AClient` pools its connections on the loop
that opened them, so the call after the first fails with "Event loop is
closed". The earlier tools never awaited the client at all: no request was
sent, and the balance and AWI tools returned a coroutine repr or crashed.

Use the governed CrewAI wrapper instead. Every call goes through the
**permit → invoke → signed receipt** loop:

- [`integrations/crewai-agent-middleware`](../../integrations/crewai-agent-middleware/README.md)
  — `CrewAIB2ATool`.
- For LangGraph, `framework_integrations.LangGraphGovernedTools`.

## Installation

No PyPI package is published. The SDK distribution will be published as
`amw-sdk` (`pip install amw-sdk`). For now, work from a checkout of this
repository:

```bash
git clone https://github.com/agentmiddleware/agent-middleware.git
cd agent-middleware
python -m pip install -e ./sdk/python
python -m pip install -e integrations/crewai-agent-middleware
```

## Quick Start

```python
from crewai import Agent
from crewai_b2a import CrewAIB2ATool

b2a_tool = CrewAIB2ATool(
    api_key="your-api-key",
    wallet_id="agent-001",
)

researcher = Agent(
    role="Researcher",
    goal="Research topics using available tools",
    backstory="An expert researcher with access to MCP tools",
    tools=[b2a_tool],
)
```

See the wrapper's README for the operations it exposes and its permit
settings.
