"""Canonical JSON and argument hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Sorted keys, no insignificant whitespace, ASCII only (the AMW signing form)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


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
