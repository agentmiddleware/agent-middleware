#!/usr/bin/env python3
"""Local approve/deny CLI for AMW V2 pending approvals.

Talks to the gateway's local-only endpoints over loopback; run on the gateway
host. In-process callers can use the approval store directly instead.

Usage:
    python gateway/scripts/amw_approvals.py list
    python gateway/scripts/amw_approvals.py approve <nonce>
    python gateway/scripts/amw_approvals.py deny <nonce>
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
BASE_URL_ENV = "AMW_GATEWAY_URL"


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=10.0)


def _format_time(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def cmd_list(args: argparse.Namespace) -> int:
    try:
        response = _client(args.base_url).get("/v1/approvals/pending")
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
        response = _client(args.base_url).post(f"/v1/approvals/{args.nonce}/{action}")
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
    if args.command == "list":
        return cmd_list(args)
    if args.command in ("approve", "deny"):
        return _decide(args, args.command)
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
