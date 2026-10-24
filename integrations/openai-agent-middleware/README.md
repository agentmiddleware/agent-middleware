# OpenAI + Agent Middleware API

OpenAI function-calling and Agents SDK integration for the Agent Middleware API
via the governed **permit → invoke → receipt** flow.

**Status:** source-only integration example, not a published package. Start
with the [documentation guide](../../gateway/docs/README.md) to evaluate the supported
one-tool MCP path before adopting a framework wrapper. This wrapper has not
been exercised against a live OpenAI model in this repository's CI; its tests
drive the governed loop with recorded tool-call shapes.

## The one idea: the model's `tool_call.id` is the operation identity

Every tool call OpenAI emits carries an id (`call_…`) that is assigned once
and lives in the conversation transcript your application already persists.
A retry of the same tool call — after a dropped connection, a crashed worker,
a resumed run — therefore carries the same id. That is exactly what the trust
plane's `Idempotency-Key` exists to capture, so `GovernedToolRunner`:

1. derives the key from the id (`oai-<tool_call.id>`) and **never invents
   one** — a tool call without an id is refused, because an invented key would
   turn the retry into a second charged action;
2. writes the derivation, including the canonical JSON arguments, to an
   `OperationKeyStore` **before the first network call**, so a crash between
   "the model asked" and "the receipt came back" resumes with the same key
   and the same arguments;
3. issues one permit per `(run_id, tool)` under a stable key, recording its
   complete JSON request before sending it. Retries reuse the original wallet,
   budget, scopes, and expiry, so the server receives identical request bytes.

Register each allowed tool with `register_tool` before executing calls. The
runner accepts only the returned OpenAI function names; a model-emitted raw
MCP name or an unregistered function is refused before persistence or HTTP.

The server side of the same contract is described in
[`docs/failure-semantics.md`](../../gateway/docs/failure-semantics.md): a key that is
present but malformed is refused with `-32602 invalid_idempotency_key` before
anything is minted or charged.

## Installation

This package is not published to PyPI. Install it from a checkout of this
repository:

```bash
git clone https://github.com/PetrefiedThunder/agent-middleware.git
cd agent-middleware
python -m pip install -e ./sdk/python
python -m pip install -e integrations/openai-agent-middleware
```

`b2a_sdk` remains an import package. The SDK distribution will be published
as `amw-sdk` (`pip install amw-sdk`). Until then, install it from the local
path first because this wrapper depends on `amw-sdk>=0.4.0` and cannot
resolve that dependency from PyPI. The `openai` package is optional
(`pip install -e "integrations/openai-agent-middleware[openai]"`): the runner
accepts the tool-call objects any OpenAI SDK version emits, and plain dicts.

## Chat Completions (function calling)

```python
import asyncio
from openai import AsyncOpenAI
from openai_b2a import B2AClient, GovernedToolRunner, JsonFileOperationKeyStore


async def main() -> None:
    trust = B2AClient(api_key="<wallet-scoped key>", base_url="http://127.0.0.1:8000")
    runner = GovernedToolRunner(
        trust,
        wallet_id="<your wallet id>",
        run_id="job-2026-09-05-001",  # stable across a resume of this run
        key_store=JsonFileOperationKeyStore("operations.json"),
    )
    tools = [
        runner.register_tool(
            "partner.notes.write",
            description="Append a note to the partner notebook.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    ]

    openai = AsyncOpenAI()
    messages = [{"role": "user", "content": "Write the note: shipped the fix."}]
    completion = await openai.chat.completions.create(
        model="gpt-4o", messages=messages, tools=tools
    )
    assistant = completion.choices[0].message
    messages.append(assistant)

    # Each tool call becomes exactly one governed action; retries replay.
    for outcome in await runner.run_all(assistant.tool_calls or []):
        messages.append(outcome.as_tool_message())
        print(outcome.receipt.receipt_id, outcome.receipt.credits_charged)


asyncio.run(main())
```

## Responses API / Agents SDK

`function_call` items carry the operation identity in `call_id` (the item's own
`id` is *not* used). Hand the item to the same runner and append the result as
a `function_call_output`:

```python
outcome = await runner.run(item)  # item.type == "function_call"
input_items.append(outcome.as_function_call_output())
```

## Resuming after a crash

Keep `operations.json` (or your own `OperationKeyStore` implementation) with
the run's transcript. Construct a new runner with the **same `run_id` and
wallet**, register the same tools, and replay the transcript's tool calls:
every call that already
completed returns its original receipt, every call that was interrupted
finishes as one action, and nothing is charged twice. A run resumed after its
permits expired needs a new `run_id` and new tool-call IDs for new work;
previously recorded calls remain bound to their original run. Changing the
runner's permit budget or TTL does not alter a persisted permit request; use a
new run for new limits.

Older stores without request snapshots can reuse permits whose IDs were
already saved. An incomplete legacy permit cannot safely reconstruct the
original request and is refused. Legacy records that contain neither a wallet
binding nor a request snapshot cannot be checked locally for a changed wallet;
keep them with their original wallet. New operation records persist that
binding, including for completed-call replays. Records written before
argument snapshots existed can still resume, but this runner cannot tell
locally if the arguments changed. Keep those stores with the original
transcript.

## What is refused client-side

| Input | Why |
| --- | --- |
| tool call without an id, or with a blank/padded/non-ASCII/non-printable id | no valid operation identity to persist; an invented key would make retries new actions |
| tool call id longer than 124 characters | `oai-` + id would exceed the trust plane's 128-character key column |
| blank, padded, non-ASCII, or non-printable run IDs or raw tool names; derived permit keys longer than 128 characters | invalid HTTP idempotency keys must fail before persistence or HTTP |
| a function name that was not registered with this runner | the model cannot expand the application's allowed tool set |
| a recorded tool call replayed under a different tool name | the operation identity is bound to one action |
| a recorded tool call replayed with different JSON arguments | the arguments are part of the action and are saved before the first network call, so a retry cannot commit a new body under the same id |
| a receipt whose wallet, tool, permit, or receipt id disagrees with the call | the runner must not treat another action's receipt as this call's result |
| an operation permit id that disagrees with the saved permit record | the call must use the permit that was issued for this run and tool |
| a blank wallet, a non-positive or unstorable permit budget, or a non-positive permit lifetime | those requests cannot succeed, and saving them would stick the failure to the run |
| a recorded call resumed under a different run ID, or stored record IDs/keys that disagree with the call, run, or tool | persisted identity must match the deterministic operation and permit key derivations |
| a persisted operation or permit request reused with a different wallet | the original issuer and subject remain bound to the action |
| an incomplete legacy permit without a request snapshot | rebuilding its body could change the request under the original key |
| two MCP tools whose names collapse to the same OpenAI function name | the runner could not map the model's call back |

## Tests

```bash
# from the repository root
python -m pytest integrations/openai-agent-middleware/tests -q
```

The in-repository test `tests/test_openai_wrapper_trust_loop.py` also drives
this runner against the real trust plane in-process: a retried tool call
returns the same signed receipt and produces one ledger debit.
