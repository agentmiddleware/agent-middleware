---
title: AutoGen
section: Guides
order: 5
description: Call a governed tool with the AG2 Classic AutoGen wrapper.
---

# AutoGen

Use the `autogen` distribution that provides AG2 Classic's `ConversableAgent`. This [source wrapper](https://github.com/agentmiddleware/agent-middleware/tree/main/integrations/autogen-agent-middleware) does not target the newer `autogen-agentchat` API and is not published to PyPI. Install it after the Python SDK from the root of an AMW checkout:

```bash
python3 -m pip install -e ./sdk/python
python3 -m pip install -e ./integrations/autogen-agent-middleware
```

With the [local quickstart](/docs/run-locally/) running, call its governed tool:

```python
import asyncio
from autogen_b2a import B2AFunctionTool

async def main():
    tool = B2AFunctionTool(
        api_key="YOUR_WALLET_KEY",
        wallet_id="YOUR_WALLET_ID",
        base_url="http://127.0.0.1:8000",
    )
    result = await tool.call_mcp_tool(
        tool_name="partner.notes.write",
        idempotency_key="YOUR_CALL_KEY",
        permit_idempotency_key="YOUR_PERMIT_KEY",
        arguments={"text": "a governed note"},
    )
    print(result["receipt_id"])

asyncio.run(main())
```

For an AutoGen chat, pass `tool.get_function_schemas()` to the assistant and use `register_b2a_tools` on the execution agent. Keep the same keys and arguments for a retry: a retry with the same key returns the original receipt instead of dispatching again. A new key is a new request. The wrapper keeps Permit request snapshots in process memory.
