#!/usr/bin/env python3
"""One-command local trust plane for the 15-minute golden path.

Boots the real API server on loopback with everything a stranger needs to
drive the governed loop end to end with no operator and no pre-shared
secret:

- strict trust mode, with an Ed25519 signing seed generated on first run and
  persisted to the state directory (rebinding a key id to new material is
  rejected by the app, so the seed must survive restarts);
- ``ENABLE_DEV_KEY_SELF_PROVISION=true`` so ``POST /v1/dev-keys/self-provision``
  mints a wallet-scoped key with synthetic dev credits;
- ``ENABLE_DOGFOOD_TOOL=true`` so exactly one invokable governed tool
  (``partner.notes.write``, 2 credits/call) is discoverable and executable;
- ``ENABLE_STANDARD_MCP_ENDPOINT=true`` so off-the-shelf MCP clients can
  connect to ``POST /mcp``.

The server binds 127.0.0.1 only, on purpose: self-provision mints keys and
synthetic credits for anyone who can reach the port. Never expose this
posture on a shared or hosted deployment — production-like environments
refuse to boot with these flags set.

Walkthrough: docs/quickstart.md. State lives in ``data/quickstart/`` by
default; delete that directory (or pass ``--reset``) to start from scratch.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = ROOT / "data" / "quickstart"
# The dogfood tool writes here (fixed ROOT-relative path in
# app/services/dogfood_tool.py); --reset clears it so the walkthrough's
# note-count observations start from zero alongside the fresh database.
DOGFOOD_NOTES_PATH = ROOT / "data" / "dogfood_partner_notes.jsonl"
SIGNING_KEY_ID = "quickstart-local-ed25519"
HEALTH_DEADLINE_SECONDS = 90.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Loopback port to serve on (default: 8000).",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=(
            "Directory for the quickstart database, signing seed, and server "
            "log (default: data/quickstart)."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Start over: delete the state directory (fresh database, fresh "
            "signing seed, all previously minted keys and receipts gone) and "
            "the dogfood notes file the governed tool appends to."
        ),
    )
    return parser.parse_args(argv)


def load_or_create_seed(state_dir: Path) -> str:
    """Return the persisted base64 signing seed, generating it on first run.

    The app refuses to rebind an existing key id to new key material
    (``signing_key_id_public_key_mismatch``), which protects historical
    receipt verification. Persisting the seed next to the database keeps the
    pair consistent across restarts; ``--reset`` discards both together.
    """
    seed_path = state_dir / "signing-seed.b64"
    if seed_path.exists():
        seed = seed_path.read_text(encoding="utf-8").strip()
        if seed:
            return seed
    seed = base64.b64encode(secrets.token_bytes(32)).decode()
    seed_path.touch(mode=0o600)
    seed_path.write_text(seed + "\n", encoding="utf-8")
    return seed


def build_server_env(state_dir: Path, seed: str) -> dict[str, str]:
    """Pin every setting that shapes the golden-path posture.

    Environment variables take precedence over the repo-root ``.env`` file,
    so pinning here shields the walkthrough from ambient or dotfile
    configuration for the settings that matter: credentials, the tool list,
    trust flags, and storage. Settings outside that posture (log levels,
    rate limits, ...) still flow through normally.
    """
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "ENVIRONMENT": "local",
            "DEBUG": "false",
            "DATABASE_URL": f"sqlite+aiosqlite:///{state_dir / 'api.db'}",
            "STATE_BACKEND": "sqlite",
            "SQLITE_URL": str(state_dir / "state.db"),
            # No pre-shared credentials of any class: the only credential is
            # the one you mint yourself through /v1/dev-keys/self-provision.
            "VALID_API_KEYS": "",
            "STATIC_DEV_API_KEYS": "",
            "TRUST_MODE_ENABLED": "true",
            "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
            "ENABLE_PROOF_SURFACES": "false",
            "ENABLE_DOGFOOD_TOOL": "true",
            "ENABLE_DEV_KEY_SELF_PROVISION": "true",
            "ENABLE_STANDARD_MCP_ENDPOINT": "true",
            # Exactly one governed tool: a stray upstream config would add a
            # second tool or fail boot on unreachable-upstream readiness.
            "MCP_UPSTREAM_ENABLED": "false",
            "TRUST_SIGNING_KEY_ID": SIGNING_KEY_ID,
            "TRUST_SIGNING_PRIVATE_KEY_B64": seed,
        }
    )
    return env


def wait_for_health(port: int, process: subprocess.Popen) -> bool:
    deadline = time.monotonic() + HEALTH_DEADLINE_SECONDS
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def print_banner(port: int, state_dir: Path) -> None:
    base = f"http://127.0.0.1:{port}"
    print(
        f"""
[quickstart] Trust plane is up: {base}
[quickstart]
[quickstart]   Discovery      {base}/.well-known/agent.json
[quickstart]   Tools          {base}/mcp/tools.json   (partner.notes.write, 2 credits/call)
[quickstart]   Mint a key     POST {base}/v1/dev-keys/self-provision   (no credential needed)
[quickstart]   MCP endpoint   POST {base}/mcp   (off-the-shelf Streamable HTTP clients)
[quickstart]   API reference  {base}/docs
[quickstart]
[quickstart] Next: open docs/quickstart.md and start at step 2. It takes you
[quickstart] from minting your own key to holding a signed receipt that
[quickstart] verifies offline — and a forged one that fails.
[quickstart]
[quickstart] State: {state_dir}  (server log: {state_dir / "server.log"})
[quickstart] Loopback only; never expose this posture on a shared host.
[quickstart] Press Ctrl-C to stop.
""".rstrip()
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_dir: Path = args.state_dir

    if args.reset:
        if state_dir.exists():
            shutil.rmtree(state_dir)
            print(f"[quickstart] Reset: removed {state_dir}")
        if DOGFOOD_NOTES_PATH.exists():
            DOGFOOD_NOTES_PATH.unlink()
            print(f"[quickstart] Reset: removed {DOGFOOD_NOTES_PATH}")
    state_dir.mkdir(parents=True, exist_ok=True)

    seed = load_or_create_seed(state_dir)
    env = build_server_env(state_dir, seed)
    log_path = state_dir / "server.log"

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
            ],
            cwd=ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            print(f"[quickstart] Starting the trust plane on 127.0.0.1:{args.port} ...")
            if not wait_for_health(args.port, process):
                print(
                    "[quickstart] Server did not become healthy within "
                    f"{HEALTH_DEADLINE_SECONDS:.0f}s. Last log lines:",
                    file=sys.stderr,
                )
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")
                    print("\n".join(tail.splitlines()[-20:]), file=sys.stderr)
                except OSError:
                    pass
                process.terminate()
                process.wait(timeout=10)
                return 1
            print_banner(args.port, state_dir)
            return process.wait()
        except KeyboardInterrupt:
            print("\n[quickstart] Stopping ...")
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            print(
                f"[quickstart] Stopped. State kept in {state_dir}; run again to resume."
            )
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
