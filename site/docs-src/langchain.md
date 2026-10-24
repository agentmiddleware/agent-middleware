---
title: LangChain
section: Guides
order: 3
description: Call a governed MCP tool through the LangChain source wrapper.
---

# LangChain

From the root of a checkout of the [AMW repository](https://github.com/agentmiddleware/agent-middleware), install the LangChain wrapper and its Python SDK dependency. They are source packages and are not published to PyPI.

```bash
python3 -m pip install -e ./sdk/python
python3 -m pip install -e ./integrations/langchain-agent-middleware
```

With the [local quickstart](/docs/run-locally/) running and a wallet scoped key, invoke its `partner.notes.write` tool:

```python
import asyncio
from langchain_b2a import B2AClient, get_mcp_tools

async def main():
    async with B2AClient(
        api_key="YOUR_WALLET_KEY", base_url="http://127.0.0.1:8000"
    ) as client:
        tool = get_mcp_tools(client, wallet_id="YOUR_WALLET_ID")[0]
        result = await tool.ainvoke({
            "tool_name": "partner.notes.write",
            "idempotency_key": "YOUR_CALL_KEY",
            "permit_idempotency_key": "YOUR_PERMIT_KEY",
            "arguments": {"text": "a governed note"},
        })
        print(result)

asyncio.run(main())
```

Both keys are caller supplied. Keep each key and the arguments unchanged for a retry: a retry with the same key returns the original receipt instead of dispatching again. A new key is a new request. The wrapper keeps its Permit request snapshot in process memory, so this example does not establish crash recovery across a process restart. See the [wrapper source](https://github.com/agentmiddleware/agent-middleware/tree/main/integrations/langchain-agent-middleware).
