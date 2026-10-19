# Agent Middleware Python SDK

Typed async client for the governed MCP trust loop:

`discover → authenticate → authorize → invoke → meter → receipt → audit → govern`

CI builds version `0.5.0` as wheel and source artifacts. Pushing the
`python-sdk-v0.5.0` tag attaches them to a GitHub release. The package is not
published to PyPI.

## Installation

For the supported evaluation, integration, security, and pilot paths, start
with the repository [documentation guide](../../gateway/docs/README.md).

### From a checkout (recommended)

Install the version in this checkout:

```bash
python -m pip install -e ./sdk/python
```

Add offline receipt verification when you need it:

```bash
python -m pip install -e './sdk/python[verify]'
```

Add the MCP server dependency (`python -m b2a_sdk.mcp serve`) when you need it.
The extra pins `mcp` below 2.x, which renamed the class the server imports:

```bash
python -m pip install -e './sdk/python[mcp]'
```

### Released artifact

The source in this checkout is `0.5.0`; the currently released artifact is
[`python-sdk-v0.4.0`](https://github.com/PetrefiedThunder/agent-middleware/releases/tag/python-sdk-v0.4.0).
Download
[`b2a_sdk-0.4.0-py3-none-any.whl`](https://github.com/PetrefiedThunder/agent-middleware/releases/download/python-sdk-v0.4.0/b2a_sdk-0.4.0-py3-none-any.whl)
and install that downloaded file:

```bash
python -m pip install ./b2a_sdk-0.4.0-py3-none-any.whl
```

`httpx` is the only runtime dependency. Offline receipt verification
additionally needs `cryptography`, kept behind the `verify` extra.

## Governed tool call

```python
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from b2a_sdk import AgentMiddlewareClient, PermitRequest


async def main() -> None:
    async with AgentMiddlewareClient(
        api_key="agt-your-api-key",
        base_url="https://your-gateway.example.com",
    ) as client:
        tools = await client.discover_tools()
        tool = next(item for item in tools if item.name == "partner.search")

        permit = await client.create_permit(
            PermitRequest(
                issuer_wallet_id="agt-wallet",
                subject_wallet_id="agt-wallet",
                subject_key_id="key-runtime",
                scopes=[f"tool:{tool.name}:invoke", "billing:charge"],
                allowed_tools=[tool.name],
                max_credits=Decimal("25"),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            ),
            idempotency_key="permit-run-001",
        )

        invocation = await client.invoke_tool(
            tool.name,
            {"query": "quarterly risk"},
            wallet_id="agt-wallet",
            permit_id=permit.permit_id,
            idempotency_key="invoke-run-001",
        )

        verification = await client.verify_receipt(invocation.receipt.receipt_id)
        evidence = await client.get_evidence(invocation.receipt.receipt_id)
        assert verification.valid and evidence.valid


asyncio.run(main())
```

Callers must provide nonblank idempotency keys when creating permits and
invoking tools. Reusing a key with a different request raises
`IdempotencyConflictError`.

The client defaults to `http://localhost:8000`, never a hosted server.
Pass `base_url` explicitly for any other deployment.

## Synchronous use

`AgentMiddlewareClient` is async-only. Pilot scripts with plain blocking
code can use `SyncAgentMiddlewareClient`, which covers the same
discover, permit, invoke, receipt, and evidence calls with no event loop
of their own:

```python
from b2a_sdk import PermitRequest, SyncAgentMiddlewareClient

with SyncAgentMiddlewareClient(
    api_key="agt-your-api-key",
    base_url="https://your-gateway.example.com",
) as client:
    tools = client.discover_tools()
    permit = client.create_permit(PermitRequest(...), idempotency_key="permit-run-001")
    invocation = client.invoke_tool(
        "partner.search",
        {"query": "quarterly risk"},
        wallet_id="agt-wallet",
        permit_id=permit.permit_id,
        idempotency_key="invoke-run-001",
    )
    assert client.verify_receipt(invocation.receipt.receipt_id).valid
```

For concurrent or long-lived work, use the async client directly.

If a remote tool was dispatched but its outcome could not be confirmed,
`invoke_tool` raises `DeliveryUncertainError`. Its `receipt_id` identifies the
signed, charged uncertainty receipt; the SDK never retries the dispatch.

## In-process permit validation (0.5+)

`LocalPermitValidator` and `GovernedEdgeSession` let framework integrations
verify a permit's Ed25519 signature locally and mirror the server's per-call
permit checks in-process (expiry, tool scope, per-tool call caps, and budget
when `estimated_credits` is supplied) with no RPC. The local checks eliminate
the server round trip only for calls the cached permit already provably denies; the server's `authorize_and_reserve`
remains authoritative and can still deny a call the local mirror allowed.

Local signature verification needs the Ed25519 verifier, which is an optional
dependency kept out of the base install. Without it, `GovernedEdgeSession.open()`
raises `PermitDeniedError("permit_verification_unavailable")` before the first
governed call:

```bash
python -m pip install './sdk/python[verify]'
```

```python
from decimal import Decimal

from b2a_sdk import AgentMiddlewareClient
from b2a_sdk.edge_client import GovernedEdgeSession

async with AgentMiddlewareClient(api_key="...") as client:
    session = await GovernedEdgeSession.open(
        client,
        permit_id="pmt-xyz",
        wallet_id="agt-wallet",
    )
    # Locally-denied calls raise PermitDeniedError immediately (no server
    # round trip); allowed calls go through the governed invoke_tool loop.
    result = await session.invoke(
        "partner.search",
        {"query": "quarterly risk"},
        idempotency_key="invoke-run-002",
        # Without estimated_credits the local mirror skips the budget check.
        estimated_credits=Decimal("2"),
    )
```

See `sdk/python/src/b2a_sdk/edge_client.py` for the full `LocalPermitValidator`
and `GovernedEdgeSession` surface.

## Verifying a receipt offline

`b2a_sdk.receipt_verifier` checks a receipt with no server, no database, and
no credential — it imports nothing from the middleware application. Use it to
audit a receipt handed to you by another agent, or to re-check your own long
after the call.

```python
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

# bundle:   GET /v1/receipts/{receipt_id}/portable   (authorized)
# keys:     GET /.well-known/trust-keys.json         (unauthenticated)
result = verify_bundle(bundle, key_set_from_document(keys))

if result.ok:
    print(result.claims["tool"], result.claims["credits_charged"])
elif result.is_tampered:
    print("receipt does not verify:", result.reason)
else:
    print("cannot determine:", result.status.value, result.reason)
```

That three-way split is deliberate. `is_tampered` is a verdict on the receipt;
`UNKNOWN_KEY`, `MALFORMED`, and `UNSUPPORTED` say only that this verifier could
not decide — usually a stale key set. Treating them alike turns an outage into
a fraud alarm.

The same check from a shell:

```bash
b2a-verify-receipt --bundle receipt.json --keys trust-keys.json
# exit 0 verified, 1 forged, 2 undetermined
```

Pass `--issuer https://api.example.com` to fetch the key set instead of
supplying it, and `--expect-issuer` to require the bundle to name the origin
you meant to audit.

## Compatibility

`B2AClient` remains available for existing integrations and emits a
`DeprecationWarning`. Legacy wallet, telemetry, dry-run, decorator, and edge
client methods remain available during the `0.4.x` transition. New code should
use `AgentMiddlewareClient` and the typed trust-loop methods.

Legacy `charge()` and `@billable` calls made without an idempotency key are
not replay-safe: retrying one whose response was lost bills the wallet again.
Unkeyed `charge()` calls now emit a `UserWarning` to say so.
Pass `charge(..., idempotency_key="...")`, or give `@billable` /
`@combined` an `idempotency_key_factory` that derives the same key from the
call's arguments on every retry, and the server replays the original charge
instead of debiting twice. A key reused for a different charge raises
`IdempotencyConflictError`.

`@monitored` reports only the exception type on error by default; pass
`capture_traceback=True` to also send the exception message and traceback,
which can contain secrets.

## Build and test

```bash
uv build sdk/python --out-dir dist
uv run --directory sdk/python --extra dev pytest
ruff check sdk/python/src sdk/python/tests
```
