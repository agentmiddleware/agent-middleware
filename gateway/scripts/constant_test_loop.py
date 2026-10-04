#!/usr/bin/env python3
"""Production-ready constant smoke test: permit → invoke → receipt → replay → deny.

Exercises the complete governed loop against a live trust plane. Prefers the
production tool ``partner.echo`` (upstream MCP) when available, falls back to
``partner.notes.write`` (dogfood, requires ``ENABLE_DOGFOOD_TOOL=true`` locally):

1. Permit → invoke the governed tool with scoped permit → signed receipt
2. Replay same idempotency key → same receipt_id, no second debit
3. Out-of-scope tool is denied (when multiple tools exist)

The loop reads the agent credential only from $CI_SMOKE_AGENT_KEY (never
hardcoded, never logged, never printed). Optionally reads $CI_SMOKE_WALLET_ID
and $CI_SMOKE_KEY_ID for faster startup; fetches them from the API if missing.
Exits 0 on success, 1 on any invariant failure, 2 on configuration error.

Run locally (auto-provisions agent key)::

    make quickstart  # in terminal 1
    python scripts/constant_test_loop.py  # in terminal 2

Run against production (once CI_SMOKE_AGENT_KEY is set in CI)::

    API_URL=https://api.thisisatest.tech \\
        python scripts/constant_test_loop.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_URL = "http://127.0.0.1:8000"
# Preferred governed tools, in order. Neither being present is no longer fatal:
# the registry differs between a local quickstart, a partner pilot, and
# production, and a loop that only runs where these two names exist is not a
# monitor you can point at an arbitrary deployment.
PRODUCTION_GOVERNED_TOOL = "partner.echo"
DOGFOOD_GOVERNED_TOOL = "partner.notes.write"

# Headroom over the selected tool's advertised creditsPerCall. One run spends a
# single call — the replay is free and the denial never charges — so this is
# slack for a price that moved between discovery and invocation. A fixed cap
# fails permit_budget_exceeded on a healthy deployment the moment a pricier
# tool is selected, which reads as a product fault rather than a config one.
PERMIT_CREDIT_HEADROOM = 3
PERMIT_MIN_CREDITS = 20


def credits_per_call(tool: dict) -> float:
    """The tool's advertised price, or 0 when the manifest states none."""
    try:
        return float((tool.get("annotations") or {}).get("creditsPerCall") or 0)
    except (TypeError, ValueError):
        return 0.0


def arguments_for(schema: dict, marker: str) -> dict:
    """Build a minimal argument set satisfying a tool's declared inputSchema.

    Only *required* properties are filled, typed as the schema declares, so the
    loop can invoke a tool it was not written against without inventing intent
    the caller never expressed. ``marker`` is woven into string values so a
    run's test data is traceable back to this loop in whatever the tool writes.

    Note the enum branch takes the first member, which is why an operator-
    supplied payload is mandatory off loopback: a tool declaring
    ``["delete", "preview"]`` would otherwise be sent ``delete`` every run.
    """
    properties = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    arguments: dict = {}
    for name in required:
        spec = properties.get(name) or {}
        if "default" in spec:
            arguments[name] = spec["default"]
        elif spec.get("enum"):
            arguments[name] = spec["enum"][0]
        else:
            kind = spec.get("type")
            arguments[name] = {
                "integer": 1,
                "number": 1.0,
                "boolean": False,
                "array": [],
                "object": {},
            }.get(kind, f"smoke-{marker}")
    return arguments


def select_governed_tool(
    registry: dict[str, dict], pinned: str | None
) -> tuple[str, str]:
    """Choose the tool the permit will scope to.

    Returns ``(tool, error)``. A pinned name must exist; otherwise the
    preferred production and dogfood tools are tried in order. Failing those,
    an explicit ``--tool`` pin is required: there is deliberately no fallback
    to an arbitrary registered tool, because a guessed tool can accept a
    schema-valid payload and still refuse the action, which reads as a broken
    trust plane rather than a bad choice of tool.
    """
    if pinned:
        if pinned not in registry:
            return "", (
                f"pinned tool {pinned} is not registered; available: "
                f"{sorted(registry)[:6]}"
            )
        return pinned, ""
    for preferred in (PRODUCTION_GOVERNED_TOOL, DOGFOOD_GOVERNED_TOOL):
        if preferred in registry:
            return preferred, ""
    if not registry:
        return "", "deployment exposes no tools"
    # Deliberately no "just use the first one" fallback. Schema-derived
    # arguments satisfy a tool's declared shape but not its semantics — a tool
    # can accept the payload and still refuse the action — and that failure
    # reads as a broken trust plane when it is really a bad tool choice. Ask
    # rather than guess.
    return "", (
        f"neither {PRODUCTION_GOVERNED_TOOL} nor {DOGFOOD_GOVERNED_TOOL} is "
        f"registered here. Pass --tool (or set $CI_SMOKE_TOOL) naming one "
        f"this credential may invoke, with --tool-args if it needs arguments. "
        f"Available: {', '.join(sorted(registry))}"
    )


def select_companion_tool(
    registry: dict[str, dict], governed_tool: str, pinned: str | None
) -> tuple[str, str]:
    """Pick a real registered tool the permit does not cover.

    Drawn from the registry, never from the pinned name: with the governed
    tool pinned, selecting the companion from that same value would silently
    skip the out-of-scope check. It must genuinely exist, because invoking a
    name the registry has never heard of proves "tool not found" rather than
    "the permit refused it" — a check passing for the wrong reason.

    Returns ``(tool_or_empty, error)``. Empty with no error means the
    deployment has no second tool and the check is skipped.
    """
    if pinned:
        if pinned not in registry:
            return "", (
                "an unregistered name proves tool-not-found, not permit "
                f"refusal; available: {sorted(registry)[:6]}"
            )
        if pinned == governed_tool:
            return "", (
                "the companion must differ from the permitted tool, or the "
                "check asserts a denial that should never happen"
            )
        return pinned, ""
    return next((n for n in registry if n != governed_tool), ""), ""


class SmokeTestFailure(RuntimeError):
    """Raised when an invariant fails during the smoke test."""


class ConfigurationError(RuntimeError):
    """Raised when configuration is invalid (exits with status 2)."""


def require(condition: bool, message: str) -> None:
    """Fail immediately if condition is false."""
    if not condition:
        raise SmokeTestFailure(message)


def _get_agent_key() -> tuple[str, str, str]:
    """Read agent credential from $CI_SMOKE_AGENT_KEY or signal self-provision.

    Returns (api_key, wallet_id, key_id). If CI_SMOKE_AGENT_KEY is set but
    wallet_id/key_id are missing, returns (key, "", "") to signal they should
    be fetched from the API. Never logs or prints the key.
    """
    key = os.environ.get("CI_SMOKE_AGENT_KEY", "").strip()
    wallet_id = os.environ.get("CI_SMOKE_WALLET_ID", "").strip()
    key_id = os.environ.get("CI_SMOKE_KEY_ID", "").strip()

    if key and wallet_id and key_id:
        # All three provided - use them
        return (key, wallet_id, key_id)
    elif key:
        # Key provided but wallet_id/key_id missing - will fetch from API
        return (key, "", "")
    else:
        # Nothing provided - will self-provision in run_constant_test
        return ("", "", "")


def _get_api_url() -> str:
    """Read API URL from $API_URL or default to local quickstart.

    Validates that non-loopback URLs use HTTPS to prevent cleartext key leaks.
    """
    url = os.environ.get("API_URL", DEFAULT_API_URL).rstrip("/")

    # Enforce HTTPS for non-loopback URLs (same as partner_api_key_bootstrap.py)
    from urllib.parse import urlparse

    parsed = urlparse(url)
    is_loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1")

    if parsed.scheme == "http" and not is_loopback:
        raise ConfigurationError(
            f"refusing to send CI_SMOKE_AGENT_KEY over cleartext HTTP to "
            f"non-loopback host {parsed.hostname}. Use https:// or a loopback address."
        )

    return url


def _post_json(
    client: httpx.Client,
    path: str,
    body: dict[str, Any],
    *,
    expected_status: int,
) -> dict[str, Any]:
    """POST JSON and require expected_status."""
    resp = client.post(path, json=body)
    require(
        resp.status_code == expected_status,
        f"POST {path} → {resp.status_code}: {resp.text[:500]}",
    )
    data = resp.json()
    require(isinstance(data, dict), f"POST {path} returned non-object JSON")
    return data


def _get_json(
    client: httpx.Client,
    path: str,
    *,
    expected_status: int,
) -> dict[str, Any]:
    """GET JSON and require expected_status."""
    resp = client.get(path)
    require(
        resp.status_code == expected_status,
        f"GET {path} → {resp.status_code}: {resp.text[:500]}",
    )
    data = resp.json()
    require(isinstance(data, dict), f"GET {path} returned non-object JSON")
    return data


def _build_mcp_call(
    *,
    request_id: str,
    tool: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an MCP tools/call request with mcpContext."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments or {},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


def _first_jsonrpc_result(response: dict[str, Any]) -> dict[str, Any]:
    """Extract result from JSON-RPC response, failing if error is present."""
    require("result" in response, f"expected JSON-RPC result, got: {response}")
    return response["result"]


def _first_jsonrpc_error(response: dict[str, Any]) -> dict[str, Any]:
    """Extract error from JSON-RPC response, failing if result is present."""
    require("error" in response, f"expected JSON-RPC error, got: {response}")
    return response["error"]


def run_constant_test(
    api_url: str,
    agent_key: str,
    wallet_id: str,
    key_id: str,
    *,
    pinned_tool: str | None = None,
    pinned_other_tool: str | None = None,
    tool_arguments: dict | None = None,
) -> None:
    """Execute the constant test loop and assert all invariants."""
    print(f"[constant-test] target: {api_url}", file=sys.stderr)

    # Generate a unique run ID for this execution to make idempotency keys unique
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")

    # If credentials not provided, self-provision
    if not agent_key:
        print("[constant-test] self-provisioning agent key", file=sys.stderr)
        with httpx.Client(base_url=api_url, timeout=30.0) as provision_client:
            provision_resp = _post_json(
                provision_client,
                "/v1/dev-keys/self-provision",
                {"agent_id": f"constant-test-{run_id}"},
                expected_status=201,
            )
            agent_key = provision_resp["api_key"]
            wallet_id = provision_resp["wallet_id"]
            key_id = provision_resp["key_id"]
            print(
                f"[constant-test] self-provisioned: wallet_id={wallet_id}",
                file=sys.stderr,
            )
    elif not wallet_id or not key_id:
        # Key provided but wallet_id/key_id missing - fetch from API
        print("[constant-test] fetching wallet_id and key_id from API", file=sys.stderr)
        headers = {
            "X-API-Key": agent_key,
            "Content-Type": "application/json",
        }
        with httpx.Client(
            base_url=api_url, headers=headers, timeout=30.0
        ) as fetch_client:
            wallets_resp = _get_json(
                fetch_client, "/v1/billing/wallets", expected_status=200
            )
            wallets = wallets_resp.get("wallets", [])
            require(len(wallets) > 0, "no wallets found for this API key")
            # Use the first wallet (agent wallet)
            wallet_id = wallets[0]["wallet_id"]

            # Fetch key_id from the keys list
            keys_resp = _get_json(
                fetch_client,
                f"/v1/billing/wallets/{wallet_id}/keys",
                expected_status=200,
            )
            keys = keys_resp.get("keys", [])
            require(len(keys) > 0, "no keys found for this wallet")

            # Derive key_prefix: first 8 characters (same as generate_api_key format)
            # Validate key format before deriving prefix
            if len(agent_key) < 8 or "_" not in agent_key:
                raise ConfigurationError(
                    "malformed API key: expected format <prefix>_<suffix>"
                )
            key_prefix = agent_key[:8]

            matching_keys = [k for k in keys if k.get("key_prefix") == key_prefix]
            if len(matching_keys) == 0:
                raise ConfigurationError(f"no key found with prefix {key_prefix}")
            if len(matching_keys) > 1:
                raise ConfigurationError(
                    f"ambiguous: {len(matching_keys)} keys with prefix {key_prefix}"
                )
            key_id = matching_keys[0]["key_id"]

            print(
                f"[constant-test] fetched: wallet_id={wallet_id}, key_id={key_id}",
                file=sys.stderr,
            )

    # Never log or print the agent_key.
    headers = {
        "X-API-Key": agent_key,
        "Content-Type": "application/json",
    }

    with httpx.Client(base_url=api_url, headers=headers, timeout=30.0) as client:
        # Discover tools
        print("[constant-test] discovering tools", file=sys.stderr)
        tools = _get_json(client, "/mcp/tools.json", expected_status=200)
        registry = {
            tool["name"]: tool
            for tool in tools.get("tools", [])
            if isinstance(tool, dict) and tool.get("name")
        }
        governed_tool, selection_error = select_governed_tool(registry, pinned_tool)
        if selection_error:
            # ConfigurationError (exit 2), not SmokeTestFailure (exit 1). An
            # unusable tool pin is the operator's problem; exiting 1 would
            # page someone with the same signal a broken trust plane gives.
            raise ConfigurationError(selection_error)
        print(f"[constant-test] using tool: {governed_tool}", file=sys.stderr)

        # Resolved here rather than inside the denial block below: a typo in
        # --other-tool must be reported even on a single-tool deployment,
        # where that block never runs and the bad pin would be swallowed.
        other_tool, companion_error = select_companion_tool(
            registry, governed_tool, pinned_other_tool
        )
        if companion_error:
            raise ConfigurationError(companion_error)

        # Sized from what this tool actually costs rather than a fixed cap.
        max_credits = max(
            PERMIT_MIN_CREDITS,
            credits_per_call(registry[governed_tool]) * PERMIT_CREDIT_HEADROOM,
        )

        # Issue scoped permit
        print("[constant-test] issuing scoped permit", file=sys.stderr)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Add Idempotency-Key header for permit creation (unique per run)
        permit_headers = {
            **headers,
            "Idempotency-Key": f"constant-test-permit-{run_id}",
        }
        resp = client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [governed_tool],
                "scopes": [f"tool:{governed_tool}:invoke", "billing:charge"],
                "max_credits": max_credits,
                "expires_at": expires_at,
            },
            headers=permit_headers,
        )
        require(
            resp.status_code == 201,
            f"POST /v1/permits → {resp.status_code}: {resp.text[:500]}",
        )
        permit = resp.json()
        permit_id = permit["permit_id"]
        print(f"[constant-test] permit_id={permit_id}", file=sys.stderr)

        # Track initial spent_credits (should be 0 for new permit)
        initial_spent = Decimal(str(permit.get("spent_credits", "0")))

        # Invoke the governed tool (unique idempotency key per run)
        invoke_idempotency_key = f"constant-test-invoke-{run_id}"
        print(f"[constant-test] invoking {governed_tool}", file=sys.stderr)
        governed_arguments = (
            tool_arguments
            if tool_arguments is not None
            else arguments_for(registry[governed_tool].get("inputSchema") or {}, run_id)
        )
        call_body = _build_mcp_call(
            request_id=f"constant-test-req-{run_id}",
            tool=governed_tool,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key=invoke_idempotency_key,
            arguments=governed_arguments,
        )
        first_call = _post_json(client, "/mcp/messages", call_body, expected_status=200)
        result = _first_jsonrpc_result(first_call)
        require(result["isError"] is False, f"tool call failed: {result}")
        receipt = result["receipt"]
        require(receipt["outcome"] == "success", "receipt outcome != success")
        require(
            receipt["ledger_entry_id"] is not None,
            "success receipt missing ledger entry",
        )
        require(
            receipt["permit_id"] == permit_id,
            f"receipt permit_id {receipt.get('permit_id')} != used permit {permit_id}",
        )
        charged = Decimal(str(receipt["credits_charged"]))
        require(charged > 0, f"success charged {charged} credits (expected > 0)")
        print(
            f"[constant-test] success: receipt_id={receipt['receipt_id']} "
            f"charged={charged}",
            file=sys.stderr,
        )

        # Verify receipt signature
        print("[constant-test] verifying receipt signature", file=sys.stderr)
        verify_resp = _post_json(
            client,
            "/v1/receipts/verify",
            {"receipt_id": receipt["receipt_id"]},
            expected_status=200,
        )
        require(
            verify_resp.get("valid") is True,
            f"receipt signature invalid: {verify_resp.get('reason')}",
        )
        print(
            "[constant-test] signature OK: receipt signature verified",
            file=sys.stderr,
        )

        # Verify permit spent_credits increased
        print("[constant-test] verifying permit spent_credits", file=sys.stderr)
        permit_after = _get_json(
            client, f"/v1/permits/{permit_id}", expected_status=200
        )
        spent_after = Decimal(str(permit_after["spent_credits"]))
        require(
            spent_after == initial_spent + charged,
            f"permit spent_credits {spent_after} != initial {initial_spent} + charged {charged}",
        )
        print(
            f"[constant-test] permit budget OK: spent_credits increased by {charged}",
            file=sys.stderr,
        )

        # Check ledger debit
        print("[constant-test] verifying ledger debit", file=sys.stderr)
        ledger = _get_json(
            client, f"/v1/billing/ledger/{wallet_id}", expected_status=200
        )
        matching = [
            e for e in ledger["entries"] if e["entry_id"] == receipt["ledger_entry_id"]
        ]
        require(len(matching) == 1, "receipt ledger_entry_id not in wallet ledger")
        debit_amount = Decimal(str(matching[0]["amount"]))
        require(
            debit_amount == -charged,
            f"ledger debit {debit_amount} != receipt charge {-charged}",
        )
        debits_before_replay = len(
            [e for e in ledger["entries"] if governed_tool in e.get("description", "")]
        )

        # Replay same idempotency key → same receipt_id, no second debit
        print("[constant-test] replaying with same idempotency_key", file=sys.stderr)
        replay_call = _post_json(
            client, "/mcp/messages", call_body, expected_status=200
        )
        replay_result = _first_jsonrpc_result(replay_call)
        replay_receipt = replay_result["receipt"]
        require(
            replay_receipt["receipt_id"] == receipt["receipt_id"],
            "replay returned different receipt_id",
        )
        ledger_after_replay = _get_json(
            client, f"/v1/billing/ledger/{wallet_id}", expected_status=200
        )
        debits_after_replay = len(
            [
                e
                for e in ledger_after_replay["entries"]
                if governed_tool in e.get("description", "")
            ]
        )
        require(
            debits_after_replay == debits_before_replay,
            "replay created a second debit",
        )
        print(
            "[constant-test] replay OK: same receipt_id, no second debit",
            file=sys.stderr,
        )

        # Out-of-scope tool denial check (optional).
        # A single-tool deployment exercises the core loop but cannot test
        # permit_tool_not_allowed: invoking a name the registry has never
        # heard of proves "tool not found", not "the permit refused it".
        if other_tool:
            print("[constant-test] attempting out-of-scope tool", file=sys.stderr)
            denial_body = _build_mcp_call(
                request_id=f"constant-test-deny-{run_id}",
                tool=other_tool,
                wallet_id=wallet_id,
                permit_id=permit_id,
                idempotency_key=f"constant-test-deny-{run_id}",
                arguments=arguments_for(
                    registry[other_tool].get("inputSchema") or {}, run_id
                ),
            )
            denial_call = _post_json(
                client, "/mcp/messages", denial_body, expected_status=200
            )
            denial_error = _first_jsonrpc_error(denial_call)
            require(
                denial_error["message"] == "permit_tool_not_allowed",
                f"expected permit_tool_not_allowed, got {denial_error['message']}",
            )
            denial_receipt = denial_error["data"]["receipt"]
            require(
                denial_receipt["outcome"] == "denied",
                "denial receipt outcome != denied",
            )
            require(
                denial_receipt["permit_id"] == permit_id,
                f"denial receipt permit_id {denial_receipt.get('permit_id')} != used permit {permit_id}",
            )
            denial_charged = Decimal(str(denial_receipt["credits_charged"]))
            require(
                denial_charged == Decimal("0"),
                f"denial charged {denial_charged} credits (expected 0)",
            )
            print(
                "[constant-test] denial OK: permit_tool_not_allowed, 0 credits charged",
                file=sys.stderr,
            )

            # Verify denial receipt signature
            print("[constant-test] verifying denial receipt signature", file=sys.stderr)
            denial_verify_resp = _post_json(
                client,
                "/v1/receipts/verify",
                {"receipt_id": denial_receipt["receipt_id"]},
                expected_status=200,
            )
            require(
                denial_verify_resp.get("valid") is True,
                f"denial receipt signature invalid: {denial_verify_resp.get('reason')}",
            )
            print(
                "[constant-test] denial signature OK: denial receipt signature verified",
                file=sys.stderr,
            )
        else:
            print(
                "[constant-test] SKIPPED out-of-scope tool check "
                "(no registered tool outside the permit)",
                file=sys.stderr,
            )

    print("[constant-test] ALL INVARIANTS HELD", file=sys.stderr)


def _validate_api_url(url: str) -> str:
    """Validate API URL and enforce HTTPS for non-loopback hosts."""
    from urllib.parse import urlparse

    url = url.rstrip("/")
    parsed = urlparse(url)
    is_loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1")

    if parsed.scheme == "http" and not is_loopback:
        raise ConfigurationError(
            f"refusing to send CI_SMOKE_AGENT_KEY over cleartext HTTP to "
            f"non-loopback host {parsed.hostname}. Use https:// or a loopback address."
        )

    return url


def _resolve_tool_args(cli_value: str | None) -> dict | None:
    """Resolve the governed tool's payload from the flag or the environment.

    ``--tool`` and ``--other-tool`` already fall back to $CI_SMOKE_TOOL and
    $CI_SMOKE_OTHER_TOOL. Without the same fallback here, a CI job could
    configure everything the off-loopback guard demands *except* the payload,
    and would have to reach for argv to supply the one remaining piece —
    which for a secret-adjacent value is the wrong place.

    Returns a dict or None. Anything unparseable is a ConfigurationError
    (exit 2), never an invariant failure: a malformed payload means the loop
    was configured wrong, not that the trust plane broke.
    """
    raw = cli_value
    source = "--tool-args"
    if raw is None:
        raw = os.environ.get("CI_SMOKE_TOOL_ARGS", "").strip() or None
        source = "$CI_SMOKE_TOOL_ARGS"
    if raw is None:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        # A list or scalar would be forwarded as the tool's arguments and
        # rejected downstream as a tool error, which reads as the product
        # failing rather than the payload being the wrong shape.
        raise ConfigurationError(
            f"{source} must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _require_deliberate_production_target(
    api_url: str, args: argparse.Namespace
) -> None:
    """Off loopback, the tool and its payload must be operator-chosen.

    Discovery order and schema-derived arguments are development conveniences.
    Against a real deployment they would pick whatever the registry lists
    first and fill required fields from types, defaults, and the *first* enum
    member — potentially "delete" — then send that again on every run. Schema
    validity is not evidence of safety.
    """
    if urlparse(api_url).hostname in ("localhost", "127.0.0.1", "::1"):
        return
    if not args.tool:
        raise ConfigurationError(
            f"refusing to auto-select a tool against {api_url}. Pass --tool "
            "(or set $CI_SMOKE_TOOL) so you choose what this loop invokes."
        )
    if args.tool_args is None:
        raise ConfigurationError(
            f"refusing to send derived arguments to {api_url}. Pass "
            "--tool-args with a payload you have approved for repeated "
            "invocation, or --tool-args '{}' if the tool needs none."
        )


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "API URL to test against (default: $API_URL or "
            f"{DEFAULT_API_URL}). Reads $CI_SMOKE_AGENT_KEY, "
            "$CI_SMOKE_WALLET_ID, $CI_SMOKE_KEY_ID for auth, or self-provisions."
        ),
    )
    parser.add_argument(
        "--tool",
        default=os.environ.get("CI_SMOKE_TOOL") or None,
        help=(
            "Pin the tool the permit scopes to. Required off loopback: which "
            "tool a monitor invokes on every run is an operator's decision, "
            "not the registry ordering's."
        ),
    )
    parser.add_argument(
        "--other-tool",
        default=os.environ.get("CI_SMOKE_OTHER_TOOL") or None,
        help=(
            "Pin the registered tool used for the out-of-scope denial check "
            "(default: any other discovered tool)"
        ),
    )
    parser.add_argument(
        "--tool-args",
        default=None,
        help=(
            "JSON object of arguments for the governed tool, or set "
            "$CI_SMOKE_TOOL_ARGS. Required off loopback so the payload is one "
            "you approved for repeated invocation; pass '{}' if the tool "
            "needs none."
        ),
    )
    args = parser.parse_args(argv)

    try:
        # Resolved before the production guard reads args.tool_args, so an
        # env-configured payload satisfies it exactly as the flag does.
        args.tool_args = _resolve_tool_args(args.tool_args)
        # Validate API URL (applies to both --api-url and $API_URL)
        api_url = _validate_api_url(args.api_url or _get_api_url())
        _require_deliberate_production_target(api_url, args)
        agent_key, wallet_id, key_id = _get_agent_key()
        run_constant_test(
            api_url,
            agent_key,
            wallet_id,
            key_id,
            pinned_tool=args.tool,
            pinned_other_tool=args.other_tool,
            tool_arguments=args.tool_args,
        )
    except ConfigurationError as config_error:
        print(f"\n[constant-test] configuration error: {config_error}", file=sys.stderr)
        return 2
    except SmokeTestFailure as failure:
        print(f"\n[constant-test] FAILED: {failure}", file=sys.stderr)
        return 1
    except httpx.HTTPError as network_error:
        print(
            f"\n[constant-test] network error: {network_error}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
