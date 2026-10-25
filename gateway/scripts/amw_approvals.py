#!/usr/bin/env python3
"""Local approve/deny CLI for AMW V2 pending approvals.

Talks to the gateway's local-only endpoints over loopback; run on the gateway
host. In-process callers can use the approval store directly instead.
Export VALID_API_KEYS with the gateway bootstrap admin key first; every
request sends it. The key is never printed.

Usage:
    python gateway/scripts/amw_approvals.py list
    python gateway/scripts/amw_approvals.py approve <nonce>
    python gateway/scripts/amw_approvals.py deny <nonce>
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
BASE_URL_ENV = "AMW_GATEWAY_URL"
# Same setting the gateway reads for its bootstrap admin keys
# (app/core/config.py VALID_API_KEYS, comma-separated) and the same header
# the auth dependency reads (app/core/auth.py, default X-API-Key).
API_KEY_ENV = "VALID_API_KEYS"
API_KEY_HEADER = "X-API-Key"

_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_address("::1")


def _refusal_reason(base_url: str) -> str | None:
    """Why the API key must not be sent to base_url; None when allowed.

    Allowed: any https URL, or http only on loopback (localhost, 127.0.0.0/8,
    ::1). The host comes from parsed.hostname, so userinfo tricks such as
    http://127.0.0.1@evil resolve to the real host (evil), and look-alikes
    such as localhost.example or 127.0.0.1.nip.io fail both the exact
    localhost match and the IP parse. Unknown schemes and empty hosts refuse.
    """
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
    except ValueError:
        return "unparseable URL"
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        if parsed.scheme:
            return f"unsupported scheme {parsed.scheme!r}"
        return "missing URL scheme"
    if not host:
        return "missing host"
    if scheme == "https":
        return None
    if host == "localhost":
        return None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return f"non-loopback http host {host!r}"
    if addr in _LOOPBACK_V4 or addr == _LOOPBACK_V6:
        return None
    return f"non-loopback http host {host!r}"


def _read_api_key() -> str:
    """First key from API_KEY_ENV; empty when unset or blank."""
    for entry in os.environ.get(API_KEY_ENV, "").split(","):
        if entry.strip():
            return entry.strip()
    return ""


def _client(base_url: str, api_key: str) -> httpx.Client:
    reason = _refusal_reason(base_url)
    if reason is not None:
        raise ValueError(f"refusing to send the API key: {reason}")
    return httpx.Client(
        base_url=base_url, timeout=10.0, headers={API_KEY_HEADER: api_key}
    )


def _format_time(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def cmd_list(args: argparse.Namespace) -> int:
    try:
        response = _client(args.base_url, args.api_key).get("/v1/approvals/pending")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"error: gateway not reachable at {args.base_url}: {exc}", file=sys.stderr
        )
        return 1
    approvals = response.json().get("approvals", [])
    if not approvals:
        print("no pending approvals")
        return 0
    for item in approvals:
        print(
            f"{item['nonce']} tool={item['tool']} principal={item['principal']} "
            f"deadline={_format_time(item['deadline'])}"
        )
    return 0


def _decide(args: argparse.Namespace, action: str) -> int:
    try:
        response = _client(args.base_url, args.api_key).post(
            f"/v1/approvals/{args.nonce}/{action}"
        )
    except httpx.HTTPError as exc:
        print(
            f"error: gateway not reachable at {args.base_url}: {exc}", file=sys.stderr
        )
        return 1
    if response.status_code == 404:
        print(
            f"error: unknown or already decided approval: {args.nonce}", file=sys.stderr
        )
        return 1
    if response.status_code == 403:
        print(
            "error: approvals are local-only; run on the gateway host", file=sys.stderr
        )
        return 1
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.nonce}: {response.json().get('status')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Approve or deny pending AMW tool calls."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL),
        help=f"gateway base URL (env {BASE_URL_ENV})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list pending approvals")
    approve = sub.add_parser("approve", help="approve a pending tool call")
    approve.add_argument("nonce", help="approval nonce from list")
    deny = sub.add_parser("deny", help="deny a pending tool call")
    deny.add_argument("nonce", help="approval nonce from list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Validate the destination before the key is read, so a refusal never
    # attaches the bootstrap admin key to a request.
    reason = _refusal_reason(args.base_url)
    if reason is not None:
        print(
            "error: refusing to send the API key: "
            f"{reason}; use an https URL or http on loopback "
            "(localhost, 127.0.0.0/8, ::1)",
            file=sys.stderr,
        )
        return 1
    api_key = _read_api_key()
    if not api_key:
        print(
            "error: VALID_API_KEYS is not set; export the gateway "
            "bootstrap admin key first",
            file=sys.stderr,
        )
        return 1
    args.api_key = api_key
    if args.command == "list":
        return cmd_list(args)
    if args.command in ("approve", "deny"):
        return _decide(args, args.command)
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
