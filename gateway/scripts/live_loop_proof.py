#!/usr/bin/env python3
"""Drive the complete core loop over live HTTP and emit a partner handoff bundle.

The quickstart walkthrough (docs/quickstart.md) proves each stage by hand;
this script proves all of them in one command, as the non-admin caller a
partner's agent would be, against an already-running quickstart-posture
server (``make quickstart``):

  discover -> authenticate -> authorize -> verify -> invoke -> meter
  -> receipt -> replay -> audit -> govern

Every stage is asserted, not just printed: the run exits non-zero the moment
any invariant breaks (wrong charge, replay minting a second receipt, audit
chain invalid, out-of-scope call not denied, offline verification failing).

On success it writes a handoff bundle a partner engineer can verify with no
access to this server and no credential:

  receipt-bundle.json   portable signed success receipt
  denial-bundle.json    portable signed out-of-scope denial receipt
  trust-keys.json       the issuer's published public key set
  transcript.json       machine-readable record of every stage
  VERIFY.md             instructions for independent offline verification

Run::

    make quickstart          # terminal 1: boots the server
    make live-loop-proof     # terminal 2: drives the loop, writes the bundle

Self-provision only exists on dev-like postures — production-like
deployments (staging, preprod, preview, ...) refuse it — so this script is
loopback-only by design. The run mints an API key and sends it on every
request; refusing any non-loopback target keeps that credential on the local
host and off the network entirely.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

if __package__:
    from .live_script_target import LiveTargetError, require_loopback_origin
else:
    from live_script_target import LiveTargetError, require_loopback_origin

ROOT = Path(__file__).resolve().parents[1]
SDK_SRC = ROOT.parent / "sdk" / "python" / "src"
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "live-loop-proof"
GOVERNED_TOOL = "partner.notes.write"
UNGRANTED_TOOL = "some.other.tool"
PERMIT_MAX_CREDITS = 10
# The quickstart dogfood tool bills a fixed 2 credits/call
# (DOGFOOD_CREDITS_PER_UNIT in app/services/dogfood_tool.py). Pinning it here
# lets the meter stage prove the charge is not just present but *correct* — a
# server that regressed metering to 0 (revenue leak) must fail, not pass.
EXPECTED_CREDITS_PER_CALL = Decimal("2")
# Every file the run writes into the handoff directory.
ARTIFACTS = (
    "receipt-bundle.json",
    "denial-bundle.json",
    "trust-keys.json",
    "transcript.json",
    "VERIFY.md",
)


def _child_env(prepend: Path) -> dict[str, str]:
    """Inherit the environment, prepending ``prepend`` to PYTHONPATH.

    Prepending rather than replacing keeps an inherited PYTHONPATH (CI, tox, a
    developer shell) intact while ensuring the intended source wins on import.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{prepend}{os.pathsep}{existing}" if existing else str(prepend)
    return env


def _sanitize_url_for_record(url: str) -> str:
    """Drop any embedded credential (userinfo) and query/fragment from a URL.

    The transcript ships inside the partner handoff bundle, so a secret carried
    in ``--api-url`` (basic-auth userinfo or a token query param) must never be
    persisted there. Keep only scheme, host, port, and path. Bracket an IPv6
    host so the saved URL still parses.
    """
    parts = urlparse(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunparse((parts.scheme, netloc, parts.path, "", "", ""))


VERIFY_MD = """\
# Independent verification of this receipt bundle

You are holding signed artifacts from an Agent Middleware API trust plane.
Nothing in this directory requires an account, an API key, or network access
to the issuing server.

## What is here

- `receipt-bundle.json` — a portable signed receipt for one governed,
  metered tool invocation.
- `denial-bundle.json` — a portable signed receipt for a call the permit
  did **not** allow, denied with zero credits charged. Denials are receipted
  and signed exactly like successes.
- `trust-keys.json` — the issuer's published Ed25519 public key set
  (`/.well-known/trust-keys.json`). Public keys only.
- `transcript.json` — the stage-by-stage record of the run that produced
  these artifacts.

## Verify offline (no network)

The only dependency is `cryptography` for the Ed25519 check — no networking
library, no account. Run the commands below **from this directory** (the one
holding `receipt-bundle.json`); the bundle and key paths are relative to it.

With the SDK installed from a clone — `pip install "/path/to/agent-middleware/sdk/python[verify]"`,
which pulls in `cryptography` and provides the `b2a-verify-receipt` command:

    b2a-verify-receipt --bundle receipt-bundle.json --keys trust-keys.json

Or straight from a clone's source, pointing `PYTHONPATH` at its `b2a_sdk/src`
by **absolute** path so it resolves no matter your working directory:

    pip install cryptography
    PYTHONPATH=/path/to/agent-middleware/sdk/python/src \\
        python -m b2a_sdk.verify_cli \\
        --bundle receipt-bundle.json --keys trust-keys.json

Expected: `VERIFIED`, exit code 0. Repeat with `denial-bundle.json`.

Exit codes are meant to be branched on: 0 verified, 1 well-formed but does
not verify (treat as tampered), 2 undetermined (unknown key, malformed
input). Do not treat 2 as forgery.

## Prove tampering is detected

Edit one digit inside the `signing_input` field of `receipt-bundle.json`
(for example, change `"credits_charged":"2"` to `"0"`) and re-run the
verifier. It must exit 1.

## Verify against the live issuer instead

If you can reach the issuing server, you can fetch its key set yourself
rather than trusting the copy in this directory (this path fetches over the
network, so it also needs `httpx`: `pip install "./b2a_sdk[verify]" httpx`):

    b2a-verify-receipt --bundle receipt-bundle.json --issuer <server origin>
"""


class StageFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise StageFailure(message)


class ProofRun:
    def __init__(self, client: httpx.Client, output_dir: Path) -> None:
        self.client = client
        self.output_dir = output_dir
        self.run_id = uuid.uuid4().hex[:12]
        self.stages: list[dict[str, Any]] = []

    def record(self, stage: str, **detail: Any) -> None:
        self.stages.append({"stage": stage, "ok": True, **detail})
        summary = ", ".join(f"{k}={v}" for k, v in detail.items())
        print(f"[{len(self.stages)}] {stage:<12} {summary}")

    # -- stages ------------------------------------------------------------

    def discover(self) -> dict[str, Any]:
        agent_manifest = self.client.get("/.well-known/agent.json")
        require(agent_manifest.status_code == 200, "agent.json not served")
        tools = self.client.get("/mcp/tools.json")
        require(tools.status_code == 200, "tools.json not served")
        tool_names = [tool["name"] for tool in tools.json().get("tools", [])]
        require(
            GOVERNED_TOOL in tool_names,
            f"{GOVERNED_TOOL} not discoverable; is the server running "
            "the quickstart posture (ENABLE_DOGFOOD_TOOL=true)?",
        )
        self.record("discover", tools=tool_names)
        return tools.json()

    def authenticate(self) -> dict[str, str]:
        minted = self.client.post(
            "/v1/dev-keys/self-provision",
            json={"agent_id": f"live-loop-proof-{self.run_id}"},
        )
        hint = (
            " -- self-provision is refused on production-like targets by "
            "design; run this against a local `make quickstart` server"
            if minted.status_code == 403
            else ""
        )
        require(
            minted.status_code in (200, 201),
            f"self-provision refused ({minted.status_code}): {minted.text}{hint}",
        )
        body = minted.json()
        identity = {
            "wallet_id": body["wallet_id"],
            "key_id": body["key_id"],
        }
        self.client.headers["X-API-Key"] = body["api_key"]
        self.record("authenticate", **identity)
        return identity

    def authorize(
        self,
        identity: dict[str, str],
        *,
        allowed_tool: str,
        label: str,
        record: bool = True,
    ) -> str:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        permit = self.client.post(
            "/v1/permits",
            headers={"Idempotency-Key": f"proof-{self.run_id}-{label}"},
            json={
                "issuer_wallet_id": identity["wallet_id"],
                "subject_wallet_id": identity["wallet_id"],
                "subject_key_id": identity["key_id"],
                "allowed_tools": [allowed_tool],
                "scopes": [f"tool:{allowed_tool}:invoke", "billing:charge"],
                "max_credits": PERMIT_MAX_CREDITS,
                "expires_at": expires_at,
            },
        )
        require(
            permit.status_code == 201,
            f"permit creation failed ({permit.status_code}): {permit.text}",
        )
        permit_id = permit.json()["permit_id"]
        if record:
            self.record(
                "authorize",
                permit_id=permit_id,
                allowed_tools=[allowed_tool],
                max_credits=PERMIT_MAX_CREDITS,
            )
        return permit_id

    def _invoke_body(
        self, wallet_id: str, permit_id: str, idempotency_key: str, call_id: str
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {
                "name": GOVERNED_TOOL,
                "arguments": {"text": f"live loop proof {self.run_id}"},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                },
            },
        }

    def invoke(self, wallet_id: str, permit_id: str) -> dict[str, Any]:
        response = self.client.post(
            "/mcp/messages",
            json=self._invoke_body(
                wallet_id, permit_id, f"proof-{self.run_id}-note-1", "call-1"
            ),
        ).json()
        require(
            "result" in response,
            f"governed invoke failed: {json.dumps(response.get('error'))}",
        )
        receipt = response["result"]["receipt"]
        require(receipt["outcome"] == "success", "receipt outcome != success")
        require(bool(receipt["signature"]), "receipt is unsigned")
        require(
            receipt["ledger_entry_id"] is not None,
            "success receipt has no ledger entry",
        )
        # A metered call must actually charge the known price. Without this a
        # server that regressed to charging 0 (revenue leak) or a wrong amount
        # would still green the loop -- defeating the billing-integrity claim
        # the meter stage exists to prove.
        charged = Decimal(str(receipt["credits_charged"]))
        require(
            charged == EXPECTED_CREDITS_PER_CALL,
            f"success charged {charged} credits, expected {EXPECTED_CREDITS_PER_CALL}",
        )
        self.record(
            "invoke",
            receipt_id=receipt["receipt_id"],
            outcome=receipt["outcome"],
            credits_charged=str(charged),
        )
        return receipt

    def meter(self, wallet_id: str, receipt: dict[str, Any]) -> int:
        ledger = self.client.get(f"/v1/billing/ledger/{wallet_id}")
        require(ledger.status_code == 200, "ledger read failed")
        entries = ledger.json()["entries"]
        matching = [e for e in entries if e["entry_id"] == receipt["ledger_entry_id"]]
        require(
            len(matching) == 1,
            "receipt's ledger_entry_id not found in the wallet ledger",
        )
        # The ledger debit must agree with the receipt's charge, in the right
        # direction and magnitude -- the receipt and the money must tell the
        # same story, or metering integrity is broken.
        debit_amount = Decimal(str(matching[0]["amount"]))
        require(
            debit_amount == -EXPECTED_CREDITS_PER_CALL,
            f"ledger debit {debit_amount} != expected {-EXPECTED_CREDITS_PER_CALL}",
        )
        require(
            -debit_amount == Decimal(str(receipt["credits_charged"])),
            "ledger debit magnitude disagrees with the receipt's credits_charged",
        )
        debits = [e for e in entries if GOVERNED_TOOL in e["description"]]
        self.record(
            "meter",
            ledger_entry_id=matching[0]["entry_id"],
            amount=matching[0]["amount"],
            balance_after=matching[0]["balance_after"],
        )
        return len(debits)

    def receipt_bundle(
        self,
        receipt_id: str,
        filename: str,
        *,
        expected_outcome: str,
        expected_credits: Decimal,
    ) -> Path:
        portable = self.client.get(f"/v1/receipts/{receipt_id}/portable")
        require(
            portable.status_code == 200,
            f"portable receipt export failed ({portable.status_code})",
        )
        path = self.output_dir / filename
        path.write_text(portable.text, encoding="utf-8")
        # Verify the *exported* bundle says what this stage claims. Offline
        # signature verification proves the bytes are authentic, not that they
        # carry the right outcome; a validly-signed denial exported as a
        # success would otherwise ship in the handoff bundle unnoticed.
        signed = json.loads(
            json.loads(path.read_text(encoding="utf-8"))["signing_input"]
        )
        require(
            signed.get("outcome") == expected_outcome,
            f"{filename}: exported outcome {signed.get('outcome')!r} != "
            f"{expected_outcome!r}",
        )
        require(
            Decimal(str(signed.get("credits_charged"))) == expected_credits,
            f"{filename}: exported credits_charged "
            f"{signed.get('credits_charged')!r} != {expected_credits}",
        )
        return path

    def trust_keys(self) -> Path:
        # Deliberately unauthenticated: the key set must be fetchable by a
        # verifier who holds no credential at all.
        # base_url.join avoids a double slash: some httpx versions store
        # base_url with a trailing slash, and Starlette does not collapse
        # "//.well-known/..." to the single-slash route.
        keys_url = self.client.base_url.join(".well-known/trust-keys.json")
        # trust_env=False for the same reason as the main client: keep every
        # request on the loopback host, never through an ambient proxy.
        # Redirects stay off so a 30x cannot leave this host.
        keys = httpx.get(
            str(keys_url), timeout=30, trust_env=False, follow_redirects=False
        )
        require(keys.status_code == 200, "trust-keys.json not served")
        path = self.output_dir / "trust-keys.json"
        path.write_text(keys.text, encoding="utf-8")
        return path

    def verify_permit(self, wallet_id: str, permit_id: str) -> dict[str, Any]:
        """Prove the standalone authorization check agrees with the dispatcher.

        ``POST /v1/permits/verify`` answers "would this exact action be
        admitted?", so it is asked with the whole action: acting wallet, tool,
        and price. Asked that way it must admit the granted call and refuse the
        same permit for a *registered* tool it does not name — permit scope,
        not tool existence. An unregistered name would prove nothing here:
        "no such tool" is not "your permit does not cover it".
        """
        allowed = self.client.post(
            "/v1/permits/verify",
            json={
                "permit_id": permit_id,
                "wallet_id": wallet_id,
                "tool": GOVERNED_TOOL,
                "estimated_credits": str(EXPECTED_CREDITS_PER_CALL),
            },
        )
        require(
            allowed.status_code == 200,
            f"permit verify failed ({allowed.status_code}): {allowed.text}",
        )
        verdict = allowed.json()
        require(
            verdict["valid"] is True,
            f"granted action verified as invalid: reason={verdict.get('reason')} "
            f"details={verdict.get('details')}",
        )
        self.record("verify-permit", tool=GOVERNED_TOOL, valid=True)
        return verdict

    def verify_permit_scope(self, wallet_id: str, narrow_permit: str) -> None:
        """The same endpoint must refuse a registered tool the permit omits."""
        refused = self.client.post(
            "/v1/permits/verify",
            json={
                "permit_id": narrow_permit,
                "wallet_id": wallet_id,
                "tool": GOVERNED_TOOL,
                "estimated_credits": str(EXPECTED_CREDITS_PER_CALL),
            },
        )
        require(
            refused.status_code == 200,
            f"permit verify failed ({refused.status_code}): {refused.text}",
        )
        verdict = refused.json()
        require(
            verdict["valid"] is False
            and verdict.get("reason") == "permit_tool_not_allowed",
            "out-of-scope action verified as allowed: "
            f"valid={verdict.get('valid')} reason={verdict.get('reason')}",
        )
        self.record(
            "verify-scope",
            tool=GOVERNED_TOOL,
            valid=False,
            reason=verdict["reason"],
        )

    def replay(
        self,
        wallet_id: str,
        permit_id: str,
        receipt: dict[str, Any],
        debits_before: int,
    ) -> None:
        replayed = self.client.post(
            "/mcp/messages",
            json=self._invoke_body(
                wallet_id, permit_id, f"proof-{self.run_id}-note-1", "call-1"
            ),
        ).json()
        require("result" in replayed, "replay was rejected instead of replayed")
        require(
            replayed["result"]["receipt"]["receipt_id"] == receipt["receipt_id"],
            "replay returned a different receipt",
        )
        ledger = self.client.get(f"/v1/billing/ledger/{wallet_id}").json()
        debits_after = len(
            [e for e in ledger["entries"] if GOVERNED_TOOL in e["description"]]
        )
        require(debits_after == debits_before, "replay produced a second debit")
        self.record(
            "replay",
            receipt_id=receipt["receipt_id"],
            second_debit="none",
        )

    def audit(self) -> None:
        # A wallet-scoped key verifies its own chain; no operator needed.
        verdict = self.client.post("/v1/audit/verify-chain", json={})
        require(
            verdict.status_code == 200,
            f"audit chain verification failed ({verdict.status_code}): {verdict.text}",
        )
        chain = verdict.json()
        require(
            chain["valid"] is True,
            f"audit chain INVALID: reason={chain.get('reason')} "
            f"broken_event_id={chain.get('broken_event_id')}",
        )
        require(chain["checked_events"] > 0, "audit chain verified zero events")
        self.record("audit", valid=True, checked_events=chain["checked_events"])

    def govern(self, identity: dict[str, str]) -> dict[str, Any]:
        # record=False: the denial's own "govern" stage is what the transcript
        # records, not this setup permit.
        narrow_permit = self.authorize(
            identity, allowed_tool=UNGRANTED_TOOL, label="deny", record=False
        )
        # Same permit, asked ahead of time: the standalone check must refuse
        # the call the dispatcher is about to deny, for the same reason.
        self.verify_permit_scope(identity["wallet_id"], narrow_permit)
        denied = self.client.post(
            "/mcp/messages",
            json=self._invoke_body(
                identity["wallet_id"],
                narrow_permit,
                f"proof-{self.run_id}-deny-1",
                "deny-1",
            ),
        ).json()
        require(
            "error" in denied,
            "out-of-scope invoke was NOT denied — governance failure",
        )
        require(
            denied["error"]["message"] == "permit_tool_not_allowed",
            f"expected permit_tool_not_allowed, got {denied['error']['message']}",
        )
        denial = denied["error"]["data"]["receipt"]
        require(denial["outcome"] == "denied", "denial receipt outcome != denied")
        # Compare as Decimal, like the success charge, so a formatting change
        # ("0.00000000") cannot make this assertion silently pass or fail.
        denial_charged = Decimal(str(denial["credits_charged"]))
        require(
            denial_charged == Decimal("0"),
            f"denial was charged {denial_charged} credits",
        )
        require(bool(denial["signature"]), "denial receipt is unsigned")
        self.record(
            "govern",
            denied="permit_tool_not_allowed",
            denial_receipt=denial["receipt_id"],
            credits_charged=denial["credits_charged"],
        )
        return denial

    def verify_offline(self, bundle_path: Path, keys_path: Path, label: str) -> str:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "b2a_sdk.verify_cli",
                "--bundle",
                str(bundle_path),
                "--keys",
                str(keys_path),
            ],
            env=_child_env(SDK_SRC),
            capture_output=True,
            text=True,
            timeout=60,
        )
        require(
            result.returncode == 0,
            f"offline verification of {bundle_path.name} failed "
            f"(exit {result.returncode}): {result.stdout}{result.stderr}",
        )
        self.record(f"verify:{label}", bundle=bundle_path.name, exit_code=0)
        return result.stdout


def run(api_url: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Clear a prior run's artifacts first: a partial failure must not leave a
    # transcript describing one run beside receipts from another, since the
    # default output directory is reused and the directory is handed to a
    # partner as a single coherent bundle.
    for name in ARTIFACTS:
        (output_dir / name).unlink(missing_ok=True)
    # trust_env=False: the run mints an API key and sends it in X-API-Key on
    # every request. httpx honors ambient HTTP_PROXY/HTTPS_PROXY/ALL_PROXY by
    # default, which could route that credential off the loopback host through
    # a proxy. Disabling env trust keeps the whole loopback-only guarantee
    # airtight regardless of the caller's proxy environment.
    with httpx.Client(
        base_url=api_url, timeout=30, trust_env=False, follow_redirects=False
    ) as client:
        proof = ProofRun(client, output_dir)
        print(f"live loop proof {proof.run_id} against {api_url}\n")

        proof.discover()
        identity = proof.authenticate()
        permit_id = proof.authorize(identity, allowed_tool=GOVERNED_TOOL, label="grant")
        proof.verify_permit(identity["wallet_id"], permit_id)
        receipt = proof.invoke(identity["wallet_id"], permit_id)
        debits = proof.meter(identity["wallet_id"], receipt)

        receipt_path = proof.receipt_bundle(
            receipt["receipt_id"],
            "receipt-bundle.json",
            expected_outcome="success",
            expected_credits=EXPECTED_CREDITS_PER_CALL,
        )
        keys_path = proof.trust_keys()
        proof.record(
            "receipt",
            bundle=receipt_path.name,
            signature_key_id=receipt.get("signature_key_id"),
        )

        proof.replay(identity["wallet_id"], permit_id, receipt, debits)
        proof.audit()
        denial = proof.govern(identity)
        denial_path = proof.receipt_bundle(
            denial["receipt_id"],
            "denial-bundle.json",
            expected_outcome="denied",
            expected_credits=Decimal("0"),
        )

        success_out = proof.verify_offline(receipt_path, keys_path, "success")
        proof.verify_offline(denial_path, keys_path, "denial")

        (output_dir / "VERIFY.md").write_text(VERIFY_MD, encoding="utf-8")
        (output_dir / "transcript.json").write_text(
            json.dumps(
                {
                    "run_id": proof.run_id,
                    "api_url": _sanitize_url_for_record(api_url),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "stages": proof.stages,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print(f"\n{success_out.rstrip()}")
        print(f"\nAll stages held. Handoff bundle: {output_dir}")
        print("Hand the directory to a partner engineer; VERIFY.md tells them")
        print("how to verify both receipts offline with no credential.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Running quickstart-posture server (default: {DEFAULT_API_URL}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write the handoff bundle (default: {DEFAULT_OUTPUT_DIR}).",
    )
    args = parser.parse_args(argv)

    # Loopback only, by design: the run self-provisions a key and sends it on
    # every request, and self-provision exists only on dev-like postures. There
    # is no supported remote target, so any non-loopback host is refused before
    # a credential is minted.
    try:
        api_url = require_loopback_origin(args.api_url)
    except LiveTargetError as exc:
        print(
            f"{exc}; run it against a local `make quickstart` server",
            file=sys.stderr,
        )
        return 2

    try:
        run(api_url, args.output_dir)
    except StageFailure as failure:
        print(f"\nSTAGE FAILED: {failure}", file=sys.stderr)
        return 1
    except httpx.HTTPError as network_error:
        print(
            f"\nCould not reach {args.api_url}: {network_error}\n"
            "Is the server running? Start it with: make quickstart",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
