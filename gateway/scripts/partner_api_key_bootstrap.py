#!/usr/bin/env python3
"""Gated design-partner API key bootstrap against a running API.

Uses a bootstrap/admin key (VALID_API_KEYS) only to provision:
  sponsor wallet → agent wallet → DB-scoped agent API key.

The agent secret is printed once. Requires network access to --api-url.
Does not mint keys without a bootstrap secret (fail closed).

Resume: pass --sponsor-wallet-id / --agent-wallet-id to skip earlier creates
after a partial run (avoids duplicate wallets/credits).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

import httpx


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"error: {message}")


def _require_safe_api_url(api_url: str) -> str:
    """Require HTTPS for remote hosts; allow http:// for loopback only."""
    base = api_url.strip().rstrip("/")
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https":
        return base
    if parsed.scheme == "http" and loopback:
        return base
    raise SystemExit(
        "error: --api-url must be https:// for non-loopback hosts "
        "(bootstrap key must not travel in cleartext)"
    )


def _bootstrap_key(cli_value: str | None) -> str:
    """Prefer env over argv so the secret is not in process arguments."""
    key = (os.environ.get("BOOTSTRAP_KEY") or "").strip()
    if key:
        return key
    if cli_value:
        print(
            "warning: prefer BOOTSTRAP_KEY env over --bootstrap-key "
            "(argv is visible to local process listings)",
            file=sys.stderr,
        )
        return cli_value.strip()
    raise SystemExit(
        "error: set BOOTSTRAP_KEY in the environment "
        "(or pass --bootstrap-key for local-only use)"
    )


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(path, json=body)
    if resp.status_code >= 400:
        raise SystemExit(f"error: POST {path} → {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    _require(isinstance(data, dict), f"POST {path} returned non-object JSON")
    return data


def provision(
    *,
    api_url: str,
    bootstrap_key: str,
    sponsor_name: str,
    agent_id: str,
    budget_credits: float,
    initial_credits: float,
    key_name: str,
    sponsor_wallet_id: str | None,
    agent_wallet_id: str | None,
    daily_limit: float | None = None,
    expires_in_days: int | None = None,
    max_uses: int | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Provision sponsor → agent → key via bootstrap/admin key.

    Args:
        json_mode: If True, print only machine-readable status to stderr (never
            the api_key or bootstrap_key). Human text goes to stderr so
            `jq -r .api_key` works on stdout JSON output.
    """
    base = _require_safe_api_url(api_url)
    headers = {
        "X-API-Key": bootstrap_key,
        "Content-Type": "application/json",
    }
    with httpx.Client(base_url=base, headers=headers, timeout=60.0) as client:
        health = client.get("/health")
        _require(health.status_code == 200, f"/health → {health.status_code}")

        if sponsor_wallet_id:
            print(
                f"[resume] using sponsor_wallet_id={sponsor_wallet_id}", file=sys.stderr
            )
        else:
            sponsor = _post(
                client,
                "/v1/billing/wallets/sponsor",
                {
                    "sponsor_name": sponsor_name,
                    "email": f"{agent_id}@partners.local",
                    "initial_credits": initial_credits,
                    "require_kyc": False,
                },
            )
            sponsor_wallet_id = sponsor.get("wallet_id")
            _require(bool(sponsor_wallet_id), "sponsor wallet_id missing")
            print(
                f"[created] sponsor_wallet_id={sponsor_wallet_id}",
                file=sys.stderr,
            )

        if agent_wallet_id:
            print(f"[resume] using agent_wallet_id={agent_wallet_id}", file=sys.stderr)
        else:
            agent_body: dict[str, Any] = {
                "sponsor_wallet_id": sponsor_wallet_id,
                "agent_id": agent_id,
                "budget_credits": budget_credits,
            }
            if daily_limit is not None:
                agent_body["daily_limit"] = daily_limit
            agent = _post(client, "/v1/billing/wallets/agent", agent_body)
            agent_wallet_id = agent.get("wallet_id")
            _require(bool(agent_wallet_id), "agent wallet_id missing")
            print(
                f"[created] agent_wallet_id={agent_wallet_id}",
                file=sys.stderr,
            )

        key_body: dict[str, Any] = {
            "wallet_id": agent_wallet_id,
            "key_name": key_name,
        }
        if expires_in_days is not None:
            key_body["expires_in_days"] = expires_in_days
        if max_uses is not None:
            key_body["max_uses"] = max_uses
        key = _post(client, "/v1/api-keys", key_body)
        api_key = key.get("api_key")
        _require(bool(api_key), "api_key missing (shown only once)")

        return {
            "api_url": base,
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_wallet_id": agent_wallet_id,
            "wallet_id": agent_wallet_id,  # Documented field name (agent_wallet_id kept for compat)
            "agent_id": agent_id,
            "key_id": key.get("key_id"),
            "key_prefix": key.get("key_prefix"),
            "api_key": api_key,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision a wallet-scoped partner API key (gated bootstrap)."
    )
    parser.add_argument(
        "--api-url",
        required=True,
        help="Canonical public API origin, https://api.thisisatest.tech",
    )
    parser.add_argument(
        "--bootstrap-key",
        default=None,
        help="Deprecated: prefer BOOTSTRAP_KEY env (argv is process-visible)",
    )
    parser.add_argument("--sponsor-name", default="Design Partner")
    parser.add_argument("--agent-id", default="partner-agent-001")
    parser.add_argument("--budget-credits", type=float, default=1000.0)
    parser.add_argument("--initial-credits", type=float, default=10000.0)
    parser.add_argument("--key-name", default="partner-agent")
    parser.add_argument(
        "--daily-limit",
        type=float,
        default=None,
        help="Optional daily spend cap (credits) on the agent wallet",
    )
    parser.add_argument(
        "--expires-in-days",
        type=int,
        default=None,
        help="Optional key expiry in days (default: never expires)",
    )
    parser.add_argument(
        "--max-uses",
        type=int,
        default=None,
        help="Optional cap on successful authentications for the minted key",
    )
    parser.add_argument(
        "--sponsor-wallet-id",
        default=None,
        help="Resume: skip sponsor create and reuse this wallet id",
    )
    parser.add_argument(
        "--agent-wallet-id",
        default=None,
        help="Resume: skip agent create and reuse this wallet id",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON including api_key (default: human summary)",
    )
    parser.add_argument(
        "--key-only",
        action="store_true",
        help=(
            "Print only the minted api_key to stdout. Lets the secret be "
            "piped straight into a secret store without a jq dependency, and "
            "without the surrounding metadata landing wherever stdout goes."
        ),
    )
    args = parser.parse_args()

    if args.key_only and args.json:
        raise SystemExit("error: --key-only and --json are mutually exclusive")

    if args.agent_wallet_id and not args.sponsor_wallet_id:
        raise SystemExit(
            "error: --agent-wallet-id resume requires --sponsor-wallet-id "
            "(for operator record-keeping)"
        )

    result = provision(
        api_url=args.api_url,
        bootstrap_key=_bootstrap_key(args.bootstrap_key),
        sponsor_name=args.sponsor_name,
        agent_id=args.agent_id,
        budget_credits=args.budget_credits,
        initial_credits=args.initial_credits,
        key_name=args.key_name,
        sponsor_wallet_id=args.sponsor_wallet_id,
        agent_wallet_id=args.agent_wallet_id,
        daily_limit=args.daily_limit,
        expires_in_days=args.expires_in_days,
        max_uses=args.max_uses,
        json_mode=args.json,
    )

    if args.key_only:
        print(result["api_key"])
    elif args.json:
        # Machine-readable: JSON to stdout, all status to stderr.
        # Never print bootstrap_key. This lets `jq -r .api_key` work.
        print(json.dumps(result, indent=2))
    else:
        # Human-readable summary to stdout (legacy behavior).
        print("Partner API key bootstrap OK")
        print(f"  api_url:            {result['api_url']}")
        print(f"  sponsor_wallet_id:  {result['sponsor_wallet_id']}")
        print(f"  agent_wallet_id:    {result['agent_wallet_id']}")
        print(f"  key_id:             {result['key_id']}")
        print(f"  key_prefix:         {result['key_prefix']}")
        print(f"  api_key (once):     {result['api_key']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
