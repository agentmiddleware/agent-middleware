"""Call a governed tool through the gateway's standard MCP endpoint, then retry.

Uses the official MCP Python SDK (`pip install "mcp>=1.29,<2"`). Run a local
gateway first (`cd gateway && make quickstart`) and mint a key as shown in
QUICKSTART.md, then:

    AGENT_API_KEY=... python examples/mcp-client/call_with_retry.py

The script prints its idempotency key before the first call. The second call
sends that same key, so the gateway returns the original receipt instead of
dispatching or charging again. If the process dies after the first call, rerun
with that printed value in AMW_IDEMPOTENCY_KEY. A new process with no saved key
is a new charged call.

examples/mcp-client/mcp.json only sends the API key. A connection header would
attach one idempotency key to every tool call, so the second distinct action
would replay the first or be refused.

The process exits with an error when either call failed, when a receipt id is
missing, or when the two receipt ids differ.

Licensed under Apache-2.0.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Mapping

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("AMW_MCP_URL", "http://127.0.0.1:8000/mcp")
TOOL = os.environ.get("AMW_TOOL", "partner.notes.write")

RECEIPT_META_KEY = "io.agentmiddleware/receipt"
# Same bound the gateway stores in idempotency_records.idempotency_key.
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
# Same minimum the gateway applies before it will look up an API key.
_MIN_API_KEY_LENGTH = 8
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_NOTE = {"text": "hello from an MCP client"}


def require_api_key(environ: Mapping[str, str]) -> str:
    """Return AGENT_API_KEY, or stop before any request is sent."""
    api_key = environ.get("AGENT_API_KEY")
    if (
        isinstance(api_key, str)
        and _CONTROL_CHARACTERS.search(api_key) is None
        and len(api_key.strip()) >= _MIN_API_KEY_LENGTH
    ):
        return api_key
    raise SystemExit(
        "AGENT_API_KEY is missing, shorter than 8 characters, or contains a "
        "control character. Mint a wallet-scoped key as shown in QUICKSTART.md."
    )


def _validate_idempotency_key(key: object) -> str:
    if not isinstance(key, str) or key.strip() == "":
        raise SystemExit(
            "The idempotency key is blank. Set a non-blank key of at most "
            "128 characters, or unset AMW_IDEMPOTENCY_KEY to mint one."
        )
    if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise SystemExit(
            "The idempotency key is longer than 128 characters. The gateway "
            "refuses that key before any charge."
        )
    if _CONTROL_CHARACTERS.search(key):
        raise SystemExit(
            "The idempotency key contains a control character. The gateway "
            "refuses that key before any charge."
        )
    return key


def idempotency_key_for_run(environ: Mapping[str, str]) -> str:
    """Reuse AMW_IDEMPOTENCY_KEY when set, otherwise mint one key for this run."""
    if "AMW_IDEMPOTENCY_KEY" not in environ:
        return f"mcp-example-{uuid.uuid4()}"
    return _validate_idempotency_key(environ["AMW_IDEMPOTENCY_KEY"])


def _receipt_copies(result: dict) -> list[dict] | None:
    """Receipt objects from meta, _meta, and the top-level receipt field.

    None means the payload is malformed. Callers must not pick a receipt id
    out of it. An empty list means no receipt was present.
    """
    if not isinstance(result, dict):
        return None
    copies: list[dict] = []
    for key in ("meta", "_meta"):
        container = result.get(key)
        if container is None:
            continue
        if not isinstance(container, dict):
            return None
        if RECEIPT_META_KEY not in container:
            continue
        receipt = container[RECEIPT_META_KEY]
        if not isinstance(receipt, dict):
            return None
        copies.append(receipt)
    if "receipt" in result:
        top = result["receipt"]
        if not isinstance(top, dict):
            return None
        copies.append(top)
    return copies


def _agreed_receipt(result: dict) -> dict | None:
    """The one receipt whose copies all name the same id and outcome."""
    copies = _receipt_copies(result)
    if not copies:
        return None
    ids: list[str] = []
    outcomes: list[str] = []
    for receipt in copies:
        receipt_value = receipt.get("receipt_id")
        if not isinstance(receipt_value, str) or receipt_value.strip() == "":
            return None
        ids.append(receipt_value)
        outcome = receipt.get("outcome")
        if not isinstance(outcome, str) or outcome.strip() == "":
            return None
        outcomes.append(outcome)
    if len(set(ids)) != 1 or len(set(outcomes)) > 1:
        return None
    return copies[0]


def receipt_id(result: dict) -> str | None:
    """The gateway receipt id, when every copy of the receipt agrees."""
    receipt = _agreed_receipt(result)
    if receipt is None:
        return None
    value = receipt.get("receipt_id")
    if not isinstance(value, str) or value.strip() == "":
        return None
    return value


def _outcome(result: dict) -> str | None:
    receipt = _agreed_receipt(result)
    if receipt is None:
        return None
    value = receipt.get("outcome")
    if not isinstance(value, str) or value.strip() == "":
        return None
    return value


def retry_failure(first: dict, retry: dict) -> str | None:
    """Why this pair is not a successful replay, or None when it is."""
    if not isinstance(first, dict) or not isinstance(retry, dict):
        return "the tool result was not an object, so the retry cannot be checked"
    if first.get("isError") or retry.get("isError"):
        return (
            "the tool call returned an error, so this run did not show a "
            "successful governed call"
        )
    first_id = receipt_id(first)
    retry_id = receipt_id(retry)
    if not first_id or not retry_id:
        return (
            "a signed receipt id was missing or the receipt copies disagreed, "
            "so the retry cannot be checked"
        )
    if first_id != retry_id:
        return (
            "the retry returned a different receipt id, so this was not a "
            "replay of the first call"
        )
    first_outcome = _outcome(first)
    retry_outcome = _outcome(retry)
    if first_outcome != retry_outcome:
        return "the two calls report different outcomes for the same receipt id"
    if first_outcome != "success":
        return "the receipt outcome was not success, so this was not a successful governed call"
    return None


async def call_once(api_key: str, idempotency_key: str) -> dict:
    require_api_key({"AGENT_API_KEY": api_key})
    checked_key = _validate_idempotency_key(idempotency_key)
    headers = {"X-API-Key": api_key, "Idempotency-Key": checked_key}
    async with (
        streamablehttp_client(URL, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(TOOL, dict(_NOTE))
        return result.model_dump(mode="json")


async def main() -> None:
    api_key = require_api_key(os.environ)
    key = idempotency_key_for_run(os.environ)
    # Printed before the call so a lost response can be retried with the same key.
    print("idempotency key:", key, flush=True)
    first = await call_once(api_key, key)
    retry = await call_once(api_key, key)
    problem = retry_failure(first, retry)
    if problem is not None:
        raise SystemExit(problem)
    print("first receipt:", receipt_id(first), "outcome:", _outcome(first), flush=True)
    print("retry receipt:", receipt_id(retry), "outcome:", _outcome(retry), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
