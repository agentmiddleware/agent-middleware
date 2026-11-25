---
title: CrewAI
section: Guides
order: 4
description: Attach the governed CrewAI source wrapper to an agent.
---

# CrewAI

From the root of a checkout of the [AMW repository](https://github.com/agentmiddleware/agent-middleware), install the CrewAI wrapper and its Python SDK dependency. The wrapper is not published to PyPI.

```bash
python3 -m pip install -e ./sdk/python
python3 -m pip install -e ./integrations/crewai-agent-middleware
```

Create a CrewAI tool for a wallet scoped caller. The direct call below uses the local quickstart tool:

```python
from crewai_b2a import CrewAIB2ATool

tool = CrewAIB2ATool(
    api_key="YOUR_WALLET_KEY",
    wallet_id="YOUR_WALLET_ID",
    base_url="http://127.0.0.1:8000",
)
result = tool.run(
    operation="call_tool",
    tool_name="partner.notes.write",
    idempotency_key="YOUR_CALL_KEY",
    permit_idempotency_key="YOUR_PERMIT_KEY",
    arguments={"text": "a governed note"},
)
print(result)
```

Attach this tool to a CrewAI `Agent` with `tools=[tool]` after configuring that agent's model. The tool also supports `discover_tools` and `balance`. A `call_tool` invocation requires both keys, supplied by the caller. For a finalized Call, a retry with the same key returns the original receipt instead of dispatching again. A new key is a new request. See the [wrapper operations](https://github.com/agentmiddleware/agent-middleware/blob/main/integrations/crewai-agent-middleware/README.md) for the call shape. The wrapper caches Permit requests in process memory.
