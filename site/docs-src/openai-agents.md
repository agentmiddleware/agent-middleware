---
title: OpenAI Agents
section: Guides
order: 6
description: Run model tool calls through the governed OpenAI source wrapper.
---

# OpenAI Agents

From the root of an [AMW checkout](https://github.com/agentmiddleware/agent-middleware), install the [OpenAI wrapper](https://github.com/agentmiddleware/agent-middleware/tree/main/integrations/openai-agent-middleware) and its Python SDK dependency. The wrapper is not published to PyPI. Install the optional OpenAI dependency if you will call a model:

```bash
python3 -m pip install -e ./sdk/python
python3 -m pip install -e './integrations/openai-agent-middleware[openai]'
export OPENAI_API_KEY='YOUR_OPENAI_API_KEY'
```

With the [local quickstart](/docs/run-locally/) running, register its tool with the runner and pass the returned definition to Chat Completions:

```python
import asyncio
from openai import AsyncOpenAI
from openai_b2a import B2AClient, GovernedToolRunner, JsonFileOperationKeyStore

async def main():
    async with B2AClient(
        api_key="YOUR_WALLET_KEY", base_url="http://127.0.0.1:8000"
    ) as trust:
        runner = GovernedToolRunner(
            trust,
            wallet_id="YOUR_WALLET_ID",
            run_id="YOUR_STABLE_RUN_ID",
            key_store=JsonFileOperationKeyStore("operations.json"),
        )
        tools = [runner.register_tool(
            "partner.notes.write",
            description="Write a note",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )]
        response = await AsyncOpenAI().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Write a note that says hello"}],
            tools=tools,
        )
        for result in await runner.run_all(response.choices[0].message.tool_calls or []):
            print(result.receipt.receipt_id)

asyncio.run(main())
```

The runner derives the Request key from the model tool call ID and persists it before sending the Call. Keep the same `run_id`, store, wallet, and tool definitions when resuming a run. For Responses API function calls, pass each `function_call` item to `runner.run` and append `result.as_function_call_output()`. The wrapper's CI uses recorded tool call shapes; a live OpenAI model call is not verified there.
