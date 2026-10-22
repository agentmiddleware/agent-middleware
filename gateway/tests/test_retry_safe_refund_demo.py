"""Run the example against the gateway and check its offline evidence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "retry-safe-refund"


def test_retry_safe_refund_example() -> None:
    demo = subprocess.run(
        [sys.executable, str(EXAMPLE / "demo.py")],
        cwd=ROOT / "gateway",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert demo.returncode == 0, demo.stderr
    assert (
        "a retry with the same key returns the original receipt instead of dispatching again"
        in demo.stdout
    )
    assert "Provider dispatches: 1" in demo.stdout
    assert "Offline Ed25519 receipt verification: verified" in demo.stdout

    verified = subprocess.run(
        [
            sys.executable,
            "-m",
            "b2a_sdk.verify_cli",
            "--bundle",
            str(EXAMPLE / "output" / "receipt-bundle.json"),
            "--keys",
            str(EXAMPLE / "output" / "trust-keys.json"),
            "--expect-issuer",
            "http://sandbox",
        ],
        cwd=ROOT / "gateway",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert "VERIFIED" in verified.stdout
