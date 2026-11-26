#!/usr/bin/env python3
"""Inspect the pinned verdict fixtures offline, without dispatching a tool.

Adapted from upstream check.py, copyright (c) 2026 babyblueviper1.
MIT license, see LICENSE. Source and limitations are recorded in README.md.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

if __package__:
    from ._bip340_nostr import nostr_event_id, schnorr_verify
else:
    from _bip340_nostr import nostr_event_id, schnorr_verify

EXPECTED_PUBKEY = "6786e18a864893a900bd9858e650f67ccc3513f248fed374b591e2ff6922fbb7"


class CheckResult:
    def __init__(
        self, decision: str, reasons: tuple[str, ...], receipt_digest: str | None = None
    ):
        self.decision = decision
        self.reasons = reasons
        self.receipt_digest = receipt_digest


def canonical_call(call: dict) -> str:
    """Sorted compact JSON for these fixtures' string/integer call arguments."""
    return json.dumps(
        call, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def check(fixture: dict, call_canonical: str) -> CheckResult:
    """ALLOW only an authentic approve bound to the supplied call bytes."""
    try:
        event = fixture["proof"]["event"]
        for field, size in (("id", 64), ("pubkey", 64), ("sig", 128)):
            if not isinstance(event[field], str) or not re.fullmatch(
                rf"[0-9a-f]{{{size}}}", event[field]
            ):
                raise ValueError("invalid event hex")
        if (
            type(event["created_at"]) is not int
            or type(event["kind"]) is not int
            or event["kind"] != 30078
            or not isinstance(event["content"], str)
            or not isinstance(event["tags"], list)
            or any(
                not isinstance(tag, list) or not all(isinstance(v, str) for v in tag)
                for tag in event["tags"]
            )
        ):
            raise ValueError("invalid event shape")
        payload = json.loads(event["content"])
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "invinoveritas.verdict_proof.v1"
            or payload.get("artifact_type") != "tool_call"
        ):
            raise ValueError("invalid verdict shape")
        reasons = []
        if event["pubkey"] != EXPECTED_PUBKEY:
            reasons.append("signed by a key the checker does not pin")
        if nostr_event_id(event) != event["id"]:
            reasons.append("event id does not recompute")
        if not schnorr_verify(
            bytes.fromhex(event["id"]),
            bytes.fromhex(event["pubkey"]),
            bytes.fromhex(event["sig"]),
        ):
            reasons.append("BIP-340 signature does not verify")
        if not isinstance(call_canonical, str):
            raise ValueError("call must be canonical JSON text")
        if (
            payload.get("artifact_hash")
            != hashlib.sha256(call_canonical.encode("utf-8")).hexdigest()
        ):
            reasons.append("verdict is not bound to this call (artifact_hash mismatch)")
    except (KeyError, TypeError, ValueError):
        return CheckResult("BLOCK", ("malformed fixture or call",))
    if reasons:
        return CheckResult("BLOCK", tuple(reasons))
    verdict = payload.get("verdict")
    return CheckResult(
        "ALLOW" if verdict == "approve" else "BLOCK",
        (f"verdict={verdict}",),
        event["id"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path, nargs="+")
    parser.add_argument(
        "--call", type=Path, help="Check against this JSON call instead"
    )
    args = parser.parse_args()
    blocked = False
    for path in args.fixtures:
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
            call = (
                canonical_call(json.loads(args.call.read_text(encoding="utf-8")))
                if args.call is not None
                else fixture["artifact_canonical"]
            )
            result = check(fixture, call)
        except (OSError, KeyError, TypeError, ValueError):
            result = CheckResult("BLOCK", ("cannot read fixture or call",))
        blocked |= result.decision == "BLOCK"
        print(
            f"{path.name}: {result.decision} ({'; '.join(result.reasons)}) "
            f"receipt digest = {result.receipt_digest or 'unavailable'}"
        )
    return int(blocked)


if __name__ == "__main__":
    raise SystemExit(main())
