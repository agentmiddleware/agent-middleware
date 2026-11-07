"""Goose PreToolUse hook: deny a tool call that has no AMW permit.

Goose runs this before the tool call with the hook context JSON on stdin.
Printing ``{"decision":"block","reason":...}`` stops the call before it
reaches the MCP server; Goose shows the reason to the user and the model.

Usage in ``hooks/hooks.json``::

    goose-amw-hook --policy /path/policy.json --state-dir /path/state
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import permits
from .canonical import args_hash
from .receipts import ReceiptSigner, load_or_create_key


def decide(payload: dict, policy: dict) -> dict:
    tool = payload.get("tool_name") or payload.get("matcher_context") or ""
    arguments = payload.get("tool_input") or {}
    decision = permits.check(policy, tool, arguments)
    if decision.allowed:
        return {"decision": "allow"}
    return {"decision": "block", "reason": f"AMW permit denied: {decision.reason}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", required=True)
    ap.add_argument("--state-dir", required=True)
    ns = ap.parse_args(argv)
    payload = json.load(sys.stdin)
    out = decide(payload, permits.load_policy(ns.policy))
    if out["decision"] == "block":
        state = Path(ns.state_dir)
        signer = ReceiptSigner(
            load_or_create_key(state / "receipt_signing_key.pem"), state / "receipts.jsonl"
        )
        signer.issue(
            tool=payload.get("tool_name") or "",
            args_hash=args_hash(payload.get("tool_input") or {}),
            decision="deny",
            outcome="not_run",
            enforced_by="goose_pre_tool_use_hook",
            reason=out["reason"],
            session_id=payload.get("session_id"),
            agent_tool_call_request_id=payload.get("tool_call_id"),
        )
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
