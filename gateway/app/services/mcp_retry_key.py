"""Deterministic idempotency keys for unkeyed MCP ``tools/call`` requests.

Without a client key, the gateway used to mint ``mcp-auto-<uuid4>`` per
attempt, so a retry after a lost response got a new key and a new auto
permit and the tool ran twice (SEL-484; MCP issue #3394: "generating a fresh
key on every invocation does not correlate separate retry attempts").

The derived key binds the caller (wallet + API key), the tool, and the
canonical JSON of the arguments to a fixed time bucket. The JSON-RPC request
id is deliberately excluded: retries arrive with a fresh id. Keyed calls never
reach this module, so their behavior is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

DERIVED_KEY_PREFIX = "mcp-retry-"


def canonical_arguments(arguments: Any) -> str:
    """Stable JSON for arguments: sorted keys, no whitespace variance."""
    return json.dumps(
        arguments if arguments is not None else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def derive_unkeyed_retry_key(
    *,
    wallet_id: str | None,
    key_id: str | None,
    tool_name: str,
    arguments: Any,
    window_seconds: int,
    now: float | None = None,
) -> str | None:
    """Return a deterministic key, or None when derivation is disabled.

    None (window <= 0 or no wallet to scope by) tells the caller to fall back
    to the legacy random key.
    """
    if window_seconds <= 0 or not wallet_id:
        return None
    bucket = int((time.time() if now is None else now) // window_seconds)
    material = "\x1f".join(
        [
            "v1",
            wallet_id,
            key_id or "",
            tool_name,
            canonical_arguments(arguments),
            str(window_seconds),
            str(bucket),
        ]
    )
    return DERIVED_KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()
