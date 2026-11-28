"""Exercise the complete local example with external sockets blocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "langgraph-permit"

# A fresh interpreter keeps the gateway settings and database globals isolated
# from other examples. The socket guard covers imports as well as the demo.
CHILD = r"""
import ipaddress
import runpy
import socket
import sys
from pathlib import Path


def check(address):
    if not isinstance(address, tuple):
        return  # Unix sockets do not leave this machine.
    try:
        allowed = ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise AssertionError("external socket blocked")


for name in ("connect", "connect_ex", "sendto", "sendmsg"):
    original = getattr(socket.socket, name, None)
    if original is None:
        continue
    def guarded(self, *args, _name=name, _original=original, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            address = args[0] if _name in ("connect", "connect_ex") else args[-1]
            check(address)
        return _original(self, *args, **kwargs)
    setattr(socket.socket, name, guarded)

original_getaddrinfo = socket.getaddrinfo

def getaddrinfo(host, port, *args, **kwargs):
    check((host, port))
    return original_getaddrinfo(host, port, *args, **kwargs)

socket.getaddrinfo = getaddrinfo
# Prove the guard is active without sending any traffic.
for host in ("192.0.2.1", "example.com", "2001:db8::1"):
    try:
        socket.create_connection((host, 443))
    except AssertionError:
        pass
    else:
        raise AssertionError("socket guard did not block external traffic")

namespace = runpy.run_path(sys.argv[1], run_name="permit_demo")
main = namespace["main"]
main.__globals__["EXAMPLE_DIR"] = Path(sys.argv[2])
main.__globals__["OUTPUT_DIR"] = Path(sys.argv[2]) / "output"
main()
"""


@pytest.mark.timeout(110)
def test_langgraph_permit_example(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Real gateway calls replay once and both permit boundaries refuse dispatch."""
    assert request.config.pluginmanager.hasplugin("timeout"), "install pytest-timeout"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (ROOT / "integrations", ROOT / "gateway", EXAMPLE)
    )
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(EXAMPLE / "demo.py"), str(tmp_path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=100,
        check=False,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    lines = child.stdout.splitlines()
    first = next(line for line in lines if line.startswith("First receipt:"))
    replay = next(line for line in lines if line.startswith("Replay receipt:"))
    assert first.split()[2] == replay.split()[2]
    assert "outcome: success" in first
    assert "Tool executions: 1" in lines
    assert "No permit denial: -32003 permit_required" in lines
    assert "Local denial: permit_tool_not_allowed; HTTP requests: 0" in lines
    assert "Offline Ed25519 receipt verification: verified" in lines
    portable = json.loads((tmp_path / "output" / "receipt-bundle.json").read_text())
    keys = json.loads((tmp_path / "output" / "trust-keys.json").read_text())
    verification = verify_bundle(
        portable, key_set_from_document(keys), expected_issuer="http://127.0.0.1"
    )
    assert verification.ok, verification.reason
    assert verification.claims["tool"] == "demo.notes.append"
    assert portable["receipt_id"] == first.split()[2]
