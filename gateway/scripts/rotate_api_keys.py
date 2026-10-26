"""Environment API key rotation helper for the Agent Middleware gateway.

Two subcommands, both credential-free by construction — key material is
generated fresh or read from the environment, never from arguments or code:

    # 1. Generate replacement keys for VALID_API_KEYS (prints to stdout only)
    python scripts/rotate_api_keys.py generate [--count 2]

    # 2. Smoke-check after host configuration and rollout are confirmed
    export AGENT_MIDDLEWARE_API_URL=https://api.thisisatest.tech
    export OLD_API_KEY=...   # exact old bootstrap key
    export NEW_API_KEY=...   # exact intended replacement bootstrap key
    python scripts/rotate_api_keys.py verify

`verify` makes one request per key and exits non-zero unless BOTH sampled
responses match:
  - OLD_API_KEY receives 401/403 on GET /v1/audit/summary
  - NEW_API_KEY receives 200 on the same route

The requests may reach different load-balanced workers. This route also
accepts wallet keys, so a 200 does not prove bootstrap authority or that the
replacement is in VALID_API_KEYS. Use only intended bootstrap keys and host
configuration/rollout evidence to establish rotation completion. GET makes no
mutation request, but database-key authentication can update last_used_at or
consume a bounded use.

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
    # Stay on the named origin: an ambient proxy can move this key.
    # The SSL helper reads CA variables; the client still ignores proxy variables.
    custom_ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")
    verify = httpx.create_ssl_context(trust_env=True) if custom_ca else True
    with httpx.Client(
        base_url=base_url,
        timeout=30,
        follow_redirects=False,
        trust_env=False,
        verify=verify,
    ) as client:
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
        # This GET-only probe supports an explicitly selected production
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
    sub.add_parser("verify", help="sample old-key rejection and new-key acceptance")
    args = parser.parse_args()

    if args.command == "generate":
        return generate(args.count)
    return verify()


if __name__ == "__main__":
    sys.exit(main())
