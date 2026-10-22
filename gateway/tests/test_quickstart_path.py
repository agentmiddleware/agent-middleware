"""CI guard for the documented 15-minute golden path (docs/quickstart.md).

Boots the server through the real entry point (``scripts/quickstart.py``,
the same command ``make quickstart`` runs) in a throwaway state directory,
then drives every step of the quickstart page over HTTP: self-provision a
key, self-issue a permit, invoke the governed tool, replay it, reuse the
idempotency key with a changed payload, overspend the permit on the call
the doc predicts, verify the wallet's audit chain with its own key, verify
the receipt offline with the SDK CLI, forge it the way the doc forges it,
and get denied by a permit that does not allow the tool.

If docs/quickstart.md and the code disagree, this test is what breaks.
Assertions deliberately pin the *documented* observables — error strings,
credit amounts, exit codes — not internal implementation details.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOVERNED_TOOL = "partner.notes.write"
TOOL_COST = "2.00000000"  # documented: 2 credits per call
PERMIT_MAX_CREDITS = 7  # documented: 3 calls fit, the 4th must be denied


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def quickstart_server(tmp_path_factory):
    """The quickstart entry point itself, serving on a free loopback port."""
    state_dir = tmp_path_factory.mktemp("quickstart-state")
    port = _free_port()
    log_path = state_dir / "quickstart-stdout.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "quickstart.py"),
                "--port",
                str(port),
                "--state-dir",
                str(state_dir),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "quickstart exited early:\n"
                    + log_path.read_text(encoding="utf-8", errors="replace")
                )
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("quickstart server never became healthy")
        yield base_url
    finally:
        # SIGINT exercises the script's own graceful-shutdown path; the
        # killpg fallback covers the uvicorn child if that path wedges.
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def _prepend_pythonpath(path: Path) -> dict:
    """Environment with ``path`` prepended to (not replacing) PYTHONPATH."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{path}{os.pathsep}{existing}" if existing else str(path)
    return env


def _verify_cli(bundle_path: Path, keys_path: Path) -> subprocess.CompletedProcess:
    """Run the offline verifier exactly as the doc does: CLI, branchable exit."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "b2a_sdk.verify_cli",
            "--bundle",
            str(bundle_path),
            "--keys",
            str(keys_path),
        ],
        env=_prepend_pythonpath(REPO_ROOT.parent / "sdk" / "python" / "src"),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _invoke_body(
    wallet_id: str, permit_id: str, idempotency_key: str, text: str, call_id: str
) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {
            "name": GOVERNED_TOOL,
            "arguments": {"text": text},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


def test_documented_quickstart_path(quickstart_server, tmp_path):
    client = httpx.Client(base_url=quickstart_server, timeout=30)

    # Step 2 — discovery: exactly one invokable tool, no configuration.
    assert client.get("/.well-known/agent.json").status_code == 200
    manifest = client.get("/mcp/tools.json").json()
    assert [tool["name"] for tool in manifest["tools"]] == [GOVERNED_TOOL]

    # Step 3 — mint a key with no credential.
    provision = client.post(
        "/v1/dev-keys/self-provision", json={"agent_id": "quickstart-ci"}
    )
    assert provision.status_code in (200, 201), provision.text
    minted = provision.json()
    agent_key, wallet_id, key_id = (
        minted["api_key"],
        minted["wallet_id"],
        minted["key_id"],
    )
    auth = {"X-API-Key": agent_key}

    # Step 4 — self-issued permit: 7 credits at 2/call = 3 calls, 4th denied.
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    permit_resp = client.post(
        "/v1/permits",
        headers={**auth, "Idempotency-Key": "quickstart-ci-permit-1"},
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": [GOVERNED_TOOL],
            "scopes": [f"tool:{GOVERNED_TOOL}:invoke", "billing:charge"],
            "max_credits": PERMIT_MAX_CREDITS,
            "expires_at": expires_at,
        },
    )
    assert permit_resp.status_code == 201, permit_resp.text
    permit_id = permit_resp.json()["permit_id"]

    # Step 5 — governed invoke returns a signed success receipt.
    first = client.post(
        "/mcp/messages",
        headers=auth,
        json=_invoke_body(
            wallet_id, permit_id, "quickstart-ci-note-1", "first note", "call-1"
        ),
    ).json()
    receipt = first["result"]["receipt"]
    assert receipt["outcome"] == "success"
    assert receipt["credits_charged"] == TOOL_COST
    assert receipt["ledger_entry_id"]
    assert receipt["signature"]
    receipt_id = receipt["receipt_id"]

    # Step 6a — exact replay returns the same receipt, not a similar one.
    replay = client.post(
        "/mcp/messages",
        headers=auth,
        json=_invoke_body(
            wallet_id, permit_id, "quickstart-ci-note-1", "first note", "call-1"
        ),
    ).json()
    assert replay["result"]["receipt"]["receipt_id"] == receipt_id

    # Step 6b — same key, changed payload fails closed.
    conflict = client.post(
        "/mcp/messages",
        headers=auth,
        json=_invoke_body(
            wallet_id, permit_id, "quickstart-ci-note-1", "DIFFERENT text", "call-x"
        ),
    ).json()
    assert conflict["error"]["message"] == "idempotency_key_reused"

    # Step 7 — notes two and three fit the cap ...
    for n in (2, 3):
        ok = client.post(
            "/mcp/messages",
            headers=auth,
            json=_invoke_body(
                wallet_id,
                permit_id,
                f"quickstart-ci-note-{n}",
                f"note {n}",
                f"call-{n}",
            ),
        ).json()
        assert ok["result"]["receipt"]["credits_charged"] == TOOL_COST

    # ... and the predicted fourth call is denied, uncharged, and signed.
    overrun = client.post(
        "/mcp/messages",
        headers=auth,
        json=_invoke_body(
            wallet_id, permit_id, "quickstart-ci-note-4", "note 4", "call-4"
        ),
    ).json()
    assert overrun["error"]["message"] == "permit_budget_exceeded"
    denial = overrun["error"]["data"]["receipt"]
    assert denial["outcome"] == "denied"
    assert denial["credits_charged"] == "0"
    assert denial["ledger_entry_id"] is None
    assert denial["signature"]
    details = overrun["error"]["data"]["details"]
    assert details["required_credits"] == "2.0"
    assert details["remaining_credits"] == "1.00000000"

    # The ledger agrees: exactly three debits for the tool.
    ledger = client.get(f"/v1/billing/ledger/{wallet_id}", headers=auth).json()
    debits = [e for e in ledger["entries"] if GOVERNED_TOOL in e["description"]]
    assert len(debits) == 3

    # ... and the tamper-evident audit chain behind those entries verifies
    # with the wallet's own key — no operator credential required.
    chain = client.post("/v1/audit/verify-chain", headers=auth, json={}).json()
    assert chain["valid"] is True
    assert chain["checked_events"] > 0

    # Step 8 — offline verification, exactly as documented: fetch the
    # portable bundle and the unauthenticated key set, run the SDK CLI.
    bundle_path = tmp_path / "receipt-bundle.json"
    keys_path = tmp_path / "trust-keys.json"
    portable = client.get(f"/v1/receipts/{receipt_id}/portable", headers=auth)
    assert portable.status_code == 200
    bundle_path.write_text(portable.text, encoding="utf-8")
    trust_keys = client.get("/.well-known/trust-keys.json")  # no credential
    assert trust_keys.status_code == 200
    keys_path.write_text(trust_keys.text, encoding="utf-8")

    verified = _verify_cli(bundle_path, keys_path)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "VERIFIED" in verified.stdout

    # Forge it the way the doc forges it: claim the call was free. The
    # replace must actually bite — if canonicalization drifts and the doc's
    # forgery becomes a no-op, that is doc rot and this test must fail.
    # Note the canonical signing input stores "2", not the API's "2.00000000".
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    forged_input = bundle["signing_input"].replace(
        '"credits_charged":"2"', '"credits_charged":"0"'
    )
    assert forged_input != bundle["signing_input"]
    forged_path = tmp_path / "forged-receipt.json"
    forged_path.write_text(
        json.dumps({**bundle, "signing_input": forged_input}), encoding="utf-8"
    )
    forged = _verify_cli(forged_path, keys_path)
    assert forged.returncode == 1, forged.stdout + forged.stderr
    assert "INVALID" in forged.stderr  # the CLI reports failures on stderr

    # Step 9 — a permit that allows a different tool denies this one, with a
    # signed denial receipt that verifies offline like any other.
    deny_permit = client.post(
        "/v1/permits",
        headers={**auth, "Idempotency-Key": "quickstart-ci-permit-2"},
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": ["some.other.tool"],
            "scopes": ["tool:some.other.tool:invoke", "billing:charge"],
            "max_credits": PERMIT_MAX_CREDITS,
            "expires_at": expires_at,
        },
    ).json()
    denied = client.post(
        "/mcp/messages",
        headers=auth,
        json=_invoke_body(
            wallet_id,
            deny_permit["permit_id"],
            "quickstart-ci-denied-1",
            "should be denied",
            "deny-1",
        ),
    ).json()
    assert denied["error"]["message"] == "permit_tool_not_allowed"
    denial_receipt_id = denied["error"]["data"]["receipt"]["receipt_id"]

    denial_bundle_path = tmp_path / "denial-bundle.json"
    denial_portable = client.get(
        f"/v1/receipts/{denial_receipt_id}/portable", headers=auth
    )
    assert denial_portable.status_code == 200
    denial_bundle_path.write_text(denial_portable.text, encoding="utf-8")
    denial_verified = _verify_cli(denial_bundle_path, keys_path)
    assert denial_verified.returncode == 0
    assert "VERIFIED" in denial_verified.stdout

    client.close()


def _run_proof_script(
    *args: str, timeout: int = 30, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = _prepend_pythonpath(REPO_ROOT)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "live_loop_proof.py"), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_live_loop_proof_refuses_non_loopback_target(tmp_path):
    """The proof is loopback-only: any non-loopback host is refused before a
    credential is minted, over http or https alike. No server is needed."""
    out = tmp_path / "out"
    for url in ("https://example.com", "http://example.com"):
        result = _run_proof_script("--api-url", url, "--output-dir", str(out))
        assert result.returncode == 2, result.stdout + result.stderr
        assert "loopback-only" in result.stderr

    # Fail closed means no handoff directory, not a half-written bundle.
    assert not out.exists()


def test_live_loop_proof_reports_unreachable_server(tmp_path):
    """An unused loopback port is a reachability failure (exit 2), distinct
    from a broken invariant (exit 1); the codes must stay distinguishable."""
    dead_port = _free_port()
    result = _run_proof_script(
        "--api-url",
        f"http://127.0.0.1:{dead_port}",
        "--output-dir",
        str(tmp_path / "out"),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "make quickstart" in result.stderr


def test_live_loop_proof_ignores_ambient_proxy(quickstart_server, tmp_path):
    """The minted key must stay on loopback even under a proxy environment.

    With HTTP(S)_PROXY pointed at a dead port and NO_PROXY cleared, the run
    can only succeed if it does NOT honor those proxy vars — i.e. the client
    is built with trust_env=False. If proxy inheritance were left on, httpx
    would route the X-API-Key requests through the dead proxy and the run
    would fail. This is the regression guard for that credential-hygiene
    property.
    """
    dead_proxy = f"http://127.0.0.1:{_free_port()}"
    proxy_env = dict.fromkeys(
        (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ),
        dead_proxy,
    )
    # Clear any ambient loopback exclusion so the proxy vars would otherwise bite.
    proxy_env["NO_PROXY"] = ""
    proxy_env["no_proxy"] = ""

    output_dir = tmp_path / "handoff"
    result = _run_proof_script(
        "--api-url",
        quickstart_server,
        "--output-dir",
        str(output_dir),
        timeout=180,
        extra_env=proxy_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (output_dir / "receipt-bundle.json").exists()


def test_sanitize_url_for_record_strips_credentials():
    """The transcript ships to partners; a secret in --api-url must not."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from live_loop_proof import _sanitize_url_for_record

    sanitized = _sanitize_url_for_record(
        "http://alice:s3cr3t@127.0.0.1:8000/base?token=t0ken#frag"
    )
    assert sanitized == "http://127.0.0.1:8000/base"
    for secret in ("alice", "s3cr3t", "t0ken", "frag"):
        assert secret not in sanitized


def test_live_loop_proof_script(quickstart_server, tmp_path):
    """The one-command live proof drives every stage and writes the bundle.

    scripts/live_loop_proof.py is the scripted form of the walkthrough above;
    this keeps the two from drifting apart.
    """
    output_dir = tmp_path / "handoff"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "live_loop_proof.py"),
            "--api-url",
            quickstart_server,
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        env=_prepend_pythonpath(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    for artifact in (
        "receipt-bundle.json",
        "denial-bundle.json",
        "trust-keys.json",
        "transcript.json",
        "VERIFY.md",
    ):
        assert (output_dir / artifact).exists(), f"missing {artifact}"

    transcript = json.loads(
        (output_dir / "transcript.json").read_text(encoding="utf-8")
    )
    assert [stage["stage"] for stage in transcript["stages"]] == [
        "discover",
        "authenticate",
        "authorize",
        "verify-permit",
        "invoke",
        "meter",
        "receipt",
        "replay",
        "audit",
        "verify-scope",
        "govern",
        "verify:success",
        "verify:denial",
    ]

    # The bundle must be independently verifiable exactly as VERIFY.md
    # instructs a partner engineer to do it.
    for bundle in ("receipt-bundle.json", "denial-bundle.json"):
        verified = _verify_cli(output_dir / bundle, output_dir / "trust-keys.json")
        assert verified.returncode == 0, verified.stdout + verified.stderr

    # Signature validity alone does not prove a bundle carries the right
    # outcome. Assert the *semantics* of each exported bundle so a validly
    # signed but wrong-outcome receipt (e.g. a denial exported as a success)
    # cannot ship in the handoff unnoticed.
    def _signed(bundle_name: str) -> dict:
        outer = json.loads((output_dir / bundle_name).read_text(encoding="utf-8"))
        return json.loads(outer["signing_input"])

    success_signed = _signed("receipt-bundle.json")
    assert success_signed["outcome"] == "success"
    assert success_signed["credits_charged"] == "2"

    denial_signed = _signed("denial-bundle.json")
    assert denial_signed["outcome"] == "denied"
    assert denial_signed["credits_charged"] == "0"

    # The recorded api_url is the sanitized loopback target verbatim (it
    # carries no credential to strip). The credential-stripping behavior itself
    # is proven by test_sanitize_url_for_record_strips_credentials, so this
    # assertion is not the sole guard on the sanitizer.
    assert transcript["api_url"] == quickstart_server
