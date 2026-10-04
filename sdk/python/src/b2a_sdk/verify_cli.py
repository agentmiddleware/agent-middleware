"""Command-line offline verification of a trust-plane receipt.

Usage::

    b2a-verify-receipt --bundle receipt.json --keys trust-keys.json
    b2a-verify-receipt --bundle receipt.json --issuer https://api.example.com

With ``--issuer`` the key set is fetched from that origin's published
``/.well-known/trust-keys.json``; that fetch needs no credential. Pass
``--keys`` instead to verify with a key set you already hold, with no network
access at all.

Exit codes are meant to be branched on:

===  ==========================================================
0    signature verified
1    well-formed bundle that does not hold together — treat as
     tampered (failed signature, or a signature that verified over
     bytes contradicting the envelope around them)
2    could not determine (unknown key, malformed input, bad usage)
===  ==========================================================

The 1/2 split matters: a script that treats "I could not reach the key
server" the same as "this receipt is forged" will eventually raise a fraud
alarm during an outage.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .receipt_verifier import (
    VerificationError,
    key_set_from_document,
    verify_bundle,
)

EXIT_VERIFIED = 0
EXIT_INVALID = 1
EXIT_UNDETERMINED = 2

_KEYS_PATH = "/.well-known/trust-keys.json"


def _read_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _fetch_key_document(issuer: str, timeout: float) -> Any:
    try:
        import httpx
    except ImportError as exc:  # httpx is only needed for the --issuer fetch
        raise VerificationError(
            "Fetching a key set requires httpx; pass --keys to verify offline."
        ) from exc
    url = issuer.rstrip("/") + _KEYS_PATH
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b2a-verify-receipt",
        description=(
            "Verify a portable trust-plane receipt offline with no account or "
            "live issuer callback. Issuer authenticity requires a public key "
            "obtained through a trusted channel."
        ),
    )
    parser.add_argument(
        "--bundle",
        required=True,
        help="Path to the portable receipt bundle JSON, or '-' for stdin.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--keys",
        help=(
            "Path to a trust-keys.json document held locally (fully offline). "
            "The verifier treats this supplied key set as trusted input."
        ),
    )
    source.add_argument(
        "--issuer",
        help="Origin to fetch /.well-known/trust-keys.json from, e.g. https://api.example.com",
    )
    parser.add_argument(
        "--expect-issuer",
        help=(
            "Require the bundle's unsigned issuer label to equal this origin. "
            "This is a consistency guard, not issuer authentication."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Key fetch timeout in seconds (default: 10).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON instead of human-readable text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        bundle = _read_json(args.bundle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read bundle: {exc}", file=sys.stderr)
        return EXIT_UNDETERMINED

    try:
        if args.keys:
            key_document = _read_json(args.keys)
        else:
            key_document = _fetch_key_document(args.issuer, args.timeout)
        key_set = key_set_from_document(key_document)
    except VerificationError as exc:
        print(f"could not load key set: {exc}", file=sys.stderr)
        return EXIT_UNDETERMINED
    except Exception as exc:
        print(f"could not load key set: {exc}", file=sys.stderr)
        return EXIT_UNDETERMINED

    try:
        result = verify_bundle(
            bundle,
            key_set,
            expected_issuer=args.expect_issuer,
        )
    except VerificationError as exc:
        print(f"verification could not run: {exc}", file=sys.stderr)
        return EXIT_UNDETERMINED

    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status.value,
                    "ok": result.ok,
                    "reason": result.reason,
                    "receipt_id": result.receipt_id,
                    "key_id": result.key_id,
                    "claims": result.claims,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif result.ok:
        claims = result.claims
        print(f"VERIFIED  {result.receipt_id}")
        print(f"  signed by   {result.key_id}")
        print(f"  permit      {claims.get('permit_id')}")
        print(f"  tool        {claims.get('tool')}")
        print(f"  outcome     {claims.get('outcome')}")
        reason_code = claims.get("reason_code")
        if reason_code:
            print(f"  reason      {reason_code}")
        print(
            "  credits     "
            f"{claims.get('credits_charged')} charged "
            f"of {claims.get('credits_authorized')} authorized"
        )
        print(f"  at          {claims.get('created_at')}")
    else:
        print(f"{result.status.value.upper()}  {result.reason}", file=sys.stderr)

    if result.ok:
        return EXIT_VERIFIED
    if result.is_rejected:
        # MISMATCH belongs here, not in EXIT_UNDETERMINED. It is a determined
        # rejection -- the signature verified and the signed bytes then
        # contradicted the envelope -- which is exactly the relabelling attack
        # this tool exists to catch. Routing it to "could not determine" told
        # a branching script to treat a caught forgery as an outage.
        return EXIT_INVALID
    return EXIT_UNDETERMINED


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
