"""Canonical JSON and argument hashing."""

from __future__ import annotations

import hashlib
from typing import Any

from b2a_sdk.receipt_verifier import canonical_json


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def args_hash(arguments: dict[str, Any] | None) -> str:
    return "sha256:" + sha256_hex(canonical_json(arguments or {}))


def idempotency_key(
    *, caller: str, tool: str, arguments: dict[str, Any] | None, session_id: str
) -> str:
    """Fallback key: verified caller + tool + canonical argument hash + agent-session-id.

    ``agent-tool-call-request-id`` is deliberately NOT an input: the G1 gate showed
    Goose issues a new id when the model retries a failed call.
    """
    material = {
        "caller": caller,
        "tool": tool,
        "args_hash": args_hash(arguments),
        "session_id": session_id,
    }
    return "amw-fb-" + sha256_hex(canonical_json(material))


__all__ = ["args_hash", "canonical_json", "idempotency_key", "sha256_hex"]
