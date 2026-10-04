"""Environment API key rotation helper for the Agent Middleware gateway.

Two subcommands, both credential-free by construction — key material is
generated fresh or read from the environment, never from arguments or code:

    # 1. Generate replacement keys for VALID_API_KEYS (prints to stdout only)
    python scripts/rotate_api_keys.py generate [--count 2]

    # 2. Verify a completed rotation against a live deployment
    export AGENT_MIDDLEWARE_API_URL=https://api.thisisatest.tech
    export OLD_API_KEY=...   # the retired key — must now be rejected
    export NEW_API_KEY=...   # the replacement key — must authenticate
    python scripts/rotate_api_keys.py verify

`verify` exits non-zero unless BOTH hold:
  - OLD_API_KEY is rejected (401/403) on a bootstrap-admin read endpoint
  - NEW_API_KEY succeeds (200) on the same endpoint

The check endpoint is GET /v1/audit/summary: read-only, admin-gated, and
mutation-free, so verification never writes to production state.

Rotation itself happens at the host's secret manager (Railway: service
variables), not here — see docs/api-key-rotation.md for the full runbook.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys

import httpx

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.live_script_target import LiveTargetError, resolve_live_target  # noqa: E402

KEY_PREFIX = "amw_live_"
KEY_ENTROPY_BYTES = 32
CHECK_PATH = "/v1/audit/summary"


def generate(count: int) -> int:
    for _ in range(count):
        print(f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_ENTROPY_BYTES)}")
    print(
        "\nSet these as VALID_API_KEYS (comma-separated) in the host secret "
        "manager, redeploy, then run the verify subcommand.",
        file=sys.stderr,
    )
    return 0


def _probe(base_url: str, api_key: str) -> httpx.Response:
    with httpx.Client(base_url=base_url, timeout=30, follow_redirects=False) as client:
        return client.get(CHECK_PATH, headers={"X-API-Key": api_key})


def verify() -> int:
    base_url = os.environ.get("AGENT_MIDDLEWARE_API_URL", "").rstrip("/")
    old_key = os.environ.get("OLD_API_KEY", "")
    new_key = os.environ.get("NEW_API_KEY", "")
    missing = [
        name
        for name, value in [
            ("AGENT_MIDDLEWARE_API_URL", base_url),
            ("OLD_API_KEY", old_key),
            ("NEW_API_KEY", new_key),
        ]
        if not value
    ]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}")
        return 2

    try:
        # This read-only verifier supports an explicitly selected production
        # origin, but credentials must never travel over remote plaintext HTTP.
        base_url = resolve_live_target(base_url, confirm_production=True, environ={})
    except LiveTargetError as exc:
        print(f"Invalid AGENT_MIDDLEWARE_API_URL: {exc}")
        return 2

    failures = []

    old_resp = _probe(base_url, old_key)
    if old_resp.status_code in (401, 403):
        print(f"PASS old key rejected ({old_resp.status_code})")
    else:
        failures.append(
            f"old key was NOT rejected: {old_resp.status_code} — rotation "
            f"incomplete, the retired key still authenticates"
        )

    new_resp = _probe(base_url, new_key)
    if new_resp.status_code == 200:
        print("PASS new key accepted (200)")
    else:
        failures.append(
            f"new key did not authenticate: {new_resp.status_code} "
            f"{new_resp.text[:200]}"
        )

    for failure in failures:
        print(f"FAIL {failure}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="print fresh replacement keys")
    gen.add_argument("--count", type=int, default=2)
    sub.add_parser("verify", help="assert old key dead, new key live")
    args = parser.parse_args()

    if args.command == "generate":
        return generate(args.count)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
