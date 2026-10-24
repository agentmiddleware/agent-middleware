---
title: Python SDK
section: Reference
order: 3
description: Install the source SDK and discover tools with a wallet key.
---

# Python SDK

Install `b2a-sdk` from the [canonical source checkout](https://github.com/agentmiddleware/agent-middleware/tree/main/sdk/python). The source package is not published to PyPI. Add the `verify` extra for offline Receipt verification.

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware
python3 -m pip install -e './sdk/python[verify]'
```

Pass the gateway URL explicitly and replace the placeholders with your own key and endpoint:

```python
import asyncio
from b2a_sdk import AgentMiddlewareClient

async def main():
    async with AgentMiddlewareClient(
        api_key="YOUR_DEMO_KEY",
        base_url="YOUR_API_URL",
    ) as client:
        for tool in await client.discover_tools():
            print(tool.name)

asyncio.run(main())
```

`create_permit` and `invoke_tool` both require a caller supplied, nonblank Request key. `invoke_tool` returns a Receipt on success. A changed request under the same key raises `IdempotencyConflictError`; an uncertain upstream outcome raises `DeliveryUncertainError` with a `receipt_id`. See the [SDK example](https://github.com/agentmiddleware/agent-middleware/blob/main/sdk/python/README.md#governed-tool-call) for the full Permit and Call sequence and [Receipts](/docs/receipts/) for offline verification.
