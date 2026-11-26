"""Deterministic idempotency keys for unkeyed MCP ``tools/call`` requests.

Without a client key, the gateway used to mint ``mcp-auto-<uuid4>`` per
attempt, so a retry after a lost response got a new key and a new auto
permit and the tool ran twice (SEL-484; MCP issue #3394: "generating a fresh
key on every invocation does not correlate separate retry attempts").

The key binds the caller (wallet, API key, and the verified enterprise/IGA
principal when one rode in), the tool, and the canonical JSON of the
arguments. The JSON-RPC request id is deliberately excluded: retries arrive
with a fresh id. Keyed calls never reach this module.

The time dimension is a *slot*: ``floor(now / window)`` plus an attempt
counter. Which slot a call joins is decided by
``app.routers.mcp_standard._resolve_unkeyed_retry`` under a per-identity
lock, so the previous window's live slot is reused across a boundary
(sliding window from the first call) and an attempt that provably failed
before dispatch rolls forward to a fresh attempt instead of replaying.
"""

from __future__ import annotations

import hashlib
import json
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


def principal_binding(principal: Any) -> str:
    """Stable string for a verified enterprise principal ('' when absent)."""
    if principal is None:
        return ""
    return "\x1e".join(
        [
            str(getattr(principal, "provider", "")),
            str(getattr(principal, "issuer", "")),
            str(getattr(principal, "subject", "")),
        ]
    )


def derive_retry_identity(
    *,
    wallet_id: str,
    key_id: str | None,
    principal: str,
    tool_name: str,
    arguments: Any,
) -> str:
    """Time-independent identity of one logical unkeyed call."""
    material = "\x1f".join(
        [
            "v2",
            wallet_id,
            key_id or "",
            principal,
            tool_name,
            canonical_arguments(arguments),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def slot_key(identity: str, *, window_seconds: int, bucket: int, attempt: int) -> str:
    """Idempotency key for one (window bucket, attempt) slot of an identity."""
    material = f"{identity}\x1f{window_seconds}\x1f{bucket}\x1f{attempt}"
    return DERIVED_KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def bucket_for(now: float, window_seconds: int) -> int:
    return int(now // window_seconds)


def is_no_effect_failure(response_json: Any) -> bool:
    """True when a stored governed result is a refunded pre-dispatch failure.

    Only the pre-dispatch path stores ``upstream_result`` as the gateway's own
    ``{"error": "failed_refunded", "error_code": ...}`` terminal payload with a
    ``failed_refunded`` receipt: nothing reached the upstream, so a retry must
    run again rather than replay the failure.
    """
    if isinstance(response_json, str):
        try:
            response_json = json.loads(response_json)
        except ValueError:
            return False
    if not isinstance(response_json, dict):
        return False
    upstream = response_json.get("upstream_result")
    receipt = response_json.get("receipt")
    return (
        isinstance(upstream, dict)
        and upstream.get("error") == "failed_refunded"
        and "error_code" in upstream
        and isinstance(receipt, dict)
        and receipt.get("outcome") == "failed_refunded"
    )
