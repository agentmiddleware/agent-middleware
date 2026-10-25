"""``amw-replay`` / ``python -m app.replay`` ``--bundle <bundle.json> --calls <recorded receipts>``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from app.replay.policy_replay import (
    ReplayError,
    load_policy_bundle,
    load_recorded_calls,
    replay_calls,
)
from app.services.signing_keys import SigningKeyError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amw-replay",
        description=(
            "Re-decide recorded gateway calls under a new policy bundle. "
            "Never executes a tool or contacts any server."
        ),
    )
    parser.add_argument("--bundle", required=True, help="New policy bundle JSON")
    parser.add_argument(
        "--calls",
        required=True,
        help="Recorded gateway receipts: .json, .jsonl, or a directory",
    )
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument(
        "--recorded-keys",
        help="trust-keys.json to verify each recorded receipt before replay",
    )
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help="fail unless every recorded receipt verified against --recorded-keys",
    )
    args = parser.parse_args(argv)

    if args.require_verified and not args.recorded_keys:
        print(
            "amw-replay: --require-verified needs --recorded-keys",
            file=sys.stderr,
        )
        return 2

    try:
        recorded_keys = None
        if args.recorded_keys:
            from b2a_sdk.receipt_verifier import (
                VerificationError,
                key_set_from_document,
            )

            try:
                recorded_keys = key_set_from_document(
                    json.loads(Path(args.recorded_keys).read_text(encoding="utf-8"))
                )
            except VerificationError as exc:
                raise ReplayError(str(exc)) from exc
        calls = load_recorded_calls(args.calls, recorded_keys=recorded_keys)
        bundle = load_policy_bundle(args.bundle)
        result = replay_calls(calls, bundle)
    except (ValueError, SigningKeyError, OSError) as exc:
        print(f"amw-replay: {exc}", file=sys.stderr)
        return 2

    if args.require_verified:
        unverified = sum(
            1
            for receipt in result.receipts
            if json.loads(receipt["signing_input"]).get("recorded_signature_verified")
            is not True
        )
        if unverified:
            print(
                f"amw-replay: {unverified} unverified recorded calls",
                file=sys.stderr,
            )
            return 2

    out = Path(args.out)
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "receipts.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in result.receipts),
            encoding="utf-8",
        )
        (out / "summary.json").write_text(
            json.dumps(result.summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out / "keys.json").write_text(
            json.dumps({"keys": [result.public_key]}, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(f"amw-replay: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.summary, sort_keys=True))
    return 0
