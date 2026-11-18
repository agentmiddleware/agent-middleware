"""Fresh local database and isolated process for each external judge flow."""

from __future__ import annotations

import base64
import multiprocessing
import os
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from tests.judge_e2e.worker import serve


def pytest_collection_modifyitems(config, items):
    """Skip judge_e2e tests under xdist unless explicitly requested."""
    if hasattr(config, "workerinput") and "JUDGE_E2E_XDIST" not in os.environ:
        skip_xdist = pytest.mark.skip(reason="judge_e2e harness uses SIGKILL and is incompatible with xdist")
        for item in items:
            item.add_marker(skip_xdist)


class Judge:
    """Control an isolated gateway worker and its runtime-issued demo authority."""

    def __init__(self, database: Path):
        """Initialize the local database connection and start the worker."""
        self.database_url = f"sqlite+aiosqlite:///{database}"
        self._signing_key = base64.b64encode(
            Ed25519PrivateKey.generate().private_bytes(
                Encoding.Raw, PrivateFormat.Raw, NoEncryption()
            )
        ).decode()
        self._demo = None
        self._all_demos = []
        self.start()

    def start(self):
        """Start the worker and wait for its ready event."""
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(
            target=serve,
            args=(child, self.database_url, self._signing_key),
            daemon=True,
        )
        self.process.start()
        child.close()
        try:
            assert self.receive() == {"event": "ready"}
        except BaseException:
            self.close()
            raise

    def receive(self):
        """Receive a worker response within the timeout."""
        if not self.connection.poll(45):
            raise AssertionError("judge worker did not respond within 45 seconds")
        return self.connection.recv()

    def rpc(self, operation, **kwargs):
        """Send a worker operation and return its response."""
        self.connection.send({"operation": operation, **kwargs})
        return self.receive()

    def issue(self):
        """Request and retain demo authority issued by the gateway."""
        response = self.request("POST", "/v1/demo/keys", authenticated=False, json={})
        assert response["status"] == 201
        self._demo = response["body"]
        self._all_demos.append(self._demo)
        assert self._demo["tenant"] == "demo"
        assert self._demo["allowed_tools"] == ["partner.echo"]
        assert self._demo["permit_id"]
        return self._demo["permit_id"]

    def request(self, method, url, *, authenticated=True, **kwargs):
        """Send an ASGI request with optional demo authentication."""
        headers = kwargs.pop("headers", {})
        if authenticated:
            headers = {"X-API-Key": self._demo["api_key"], **headers}
        return self.rpc("request", method=method, url=url, headers=headers, **kwargs)

    def call(
        self,
        *,
        key="judge-call",
        message="hello",
        tool="partner.echo",
        permit_id=None,
        **kwargs,
    ):
        """Invoke the echo tool with the current demo authority."""
        return self.request(
            "POST",
            "/mcp/messages",
            headers={"Idempotency-Key": key},
            json={
                "jsonrpc": "2.0",
                "id": "judge-call",
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": {"message": message},
                    "mcpContext": {
                        "wallet_id": self._demo["wallet_id"],
                        "permit_id": permit_id or self._demo["permit_id"],
                    },
                },
            },
            **kwargs,
        )

    def snapshot(self, demo=None):
        """Read persisted state and the worker dispatch count."""
        if demo is None:
            demo = self._demo
        return self.rpc(
            "snapshot",
            wallet_id=demo["wallet_id"],
            permit_id=demo["permit_id"],
        )

    def export(self, receipt_id):
        """Export a portable receipt and the public trust key document."""
        response = self.request("GET", f"/v1/receipts/{receipt_id}/portable")
        assert response["status"] == 200
        public = self.request(
            "GET", "/.well-known/trust-keys.json", authenticated=False
        )
        assert public["status"] == 200
        return response["body"], public["body"]

    def kill(self):
        """Kill the worker and verify abnormal process termination."""
        self.process.kill()
        self.process.join(10)
        assert not self.process.is_alive()
        assert self.process.exitcode != 0
        self.connection.close()

    def close(self):
        """Stop the worker and close its pipe."""
        if self.process.is_alive():
            self.connection.send({"operation": "stop"})
            self.process.join(10)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(10)
        self.process.join(10)
        self.connection.close()


@pytest.fixture
def judge():
    """Provide a worker backed by a disposable local database."""
    # Keep all test-owned files inside the new path and remove them even on
    # failure. No credentials or portable receipts are written to this folder.
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
        value = Judge(Path(directory) / "judge.db")
        try:
            yield value
        finally:
            value.close()
