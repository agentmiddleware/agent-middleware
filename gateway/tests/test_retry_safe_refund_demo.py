"""Run the example against the gateway and check its offline evidence."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "retry-safe-refund"


def test_retry_safe_refund_example() -> None:
    _check_demo(False)
    _check_demo(True)


def _check_demo(optimize: bool) -> None:
    demo = subprocess.run(
        [sys.executable, *(["-O"] if optimize else []), str(EXAMPLE / "demo.py")],
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

    env = os.environ.copy()
    sdk_src = str(ROOT / "sdk" / "python" / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (sdk_src, env.get("PYTHONPATH")) if path
    )
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
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert "VERIFIED" in verified.stdout
