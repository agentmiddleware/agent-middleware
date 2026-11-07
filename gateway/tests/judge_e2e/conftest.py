"""Fresh local database and isolated process for each external judge flow."""

from __future__ import annotations

import base64
import multiprocessing
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


class Judge:
    def __init__(self, database: Path):
        self.database_url = f"sqlite+aiosqlite:///{database}"
        self._signing_key = base64.b64encode(
            Ed25519PrivateKey.generate().private_bytes(
                Encoding.Raw, PrivateFormat.Raw, NoEncryption()
            )
        ).decode()
        self._demo = None
        self.start()

    def start(self):
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
        if not self.connection.poll(45):
            raise AssertionError("judge worker did not respond within 45 seconds")
        return self.connection.recv()

    def rpc(self, operation, **kwargs):
        self.connection.send({"operation": operation, **kwargs})
        return self.receive()

    def issue(self):
        response = self.request("POST", "/v1/demo/keys", authenticated=False, json={})
        assert response["status"] == 201
        self._demo = response["body"]
        assert self._demo["tenant"] == "demo"
        assert self._demo["allowed_tools"] == ["partner.echo"]
        assert self._demo["permit_id"]
        return self._demo["permit_id"]

    def request(self, method, url, *, authenticated=True, **kwargs):
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

    def snapshot(self):
        return self.rpc(
            "snapshot",
            wallet_id=self._demo["wallet_id"],
            permit_id=self._demo["permit_id"],
        )

    def export(self, receipt_id):
        response = self.request("GET", f"/v1/receipts/{receipt_id}/portable")
        assert response["status"] == 200
        public = self.request(
            "GET", "/.well-known/trust-keys.json", authenticated=False
        )
        assert public["status"] == 200
        return response["body"], public["body"]

    def kill(self):
        self.process.kill()
        self.process.join(10)
        assert not self.process.is_alive()
        assert self.process.exitcode != 0
        self.connection.close()

    def close(self):
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
    # Keep all test-owned files inside the new path and remove them even on
    # failure. No credentials or portable receipts are written to this folder.
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
        value = Judge(Path(directory) / "judge.db")
        try:
            yield value
        finally:
            value.close()
