"""External end-to-end proof of the minimal viable MCP path.

Boots the real server (uvicorn subprocess, strict trust mode, sqlite) and
drives it over HTTP with the official MCP SDK client — the same transport
any standards-compliant MCP client (Claude, ChatGPT, MCP Inspector) uses.

The invariant under test: an arbitrary standards-compliant MCP client can
call one governed tool through the middleware, and the middleware proves the
call was authorized (server-minted, wallet-bounded permit), charged correctly
(exactly one ledger debit), executed at most once for an idempotency key
(replay returns the original receipt), and answered with a verifiable signed
receipt.
"""

from __future__ import annotations

import base64
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession

# streamablehttp_client is soft-deprecated in newer SDKs, but its replacement
# (streamable_http_client) does not exist at the mcp>=1.28.1 floor and takes a
# pre-built httpx client instead of headers. Keep the entry point that spans
# the pinned range.
from mcp.client.streamable_http import streamablehttp_client

from tests.test_trust_helpers import provision_agent_wallet

REPO_ROOT = Path(__file__).resolve().parent.parent
GOVERNED_TOOL = "partner.notes.write"  # the one dogfood tool
RECEIPT_META_KEY = "io.agentmiddleware/receipt"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """A real uvicorn process in strict trust mode with one governed tool."""
    tmp_path = tmp_path_factory.mktemp("minimal-path-e2e")
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "ENVIRONMENT": "local",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/e2e.db",
        "ALLOW_METADATA_CREATE_ALL": "true",
        "VALID_API_KEYS": "test-key",
        "TRUST_MODE_ENABLED": "true",
        "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
        "TRUST_SIGNING_PRIVATE_KEY_B64": base64.b64encode(
            secrets.token_bytes(32)
        ).decode(),
        "ENABLE_STANDARD_MCP_ENDPOINT": "true",
        "ENABLE_DOGFOOD_TOOL": "true",
        "ENABLE_PROOF_SURFACES": "false",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                output = process.stdout.read().decode(errors="replace")
                raise RuntimeError(f"server exited during startup:\n{output}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                process.kill()
                output = process.stdout.read().decode(errors="replace")
                raise RuntimeError(f"server never became healthy:\n{output}")
            time.sleep(0.25)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture(scope="module")
def wallet(live_server):
    """One funded agent wallet with one wallet-scoped API key, over HTTP."""

    async def provision():
        async with httpx.AsyncClient(base_url=live_server) as client:
            return await provision_agent_wallet(client)

    return anyio.run(provision)


@pytest.mark.anyio
async def test_minimal_viable_path(live_server, wallet):
    headers = {**wallet["agent_headers"], "Idempotency-Key": f"e2e-{uuid.uuid4().hex}"}
    async with streamablehttp_client(f"{live_server}/mcp", headers=headers) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert GOVERNED_TOOL in [tool.name for tool in tools.tools]
            first = await session.call_tool(GOVERNED_TOOL, {"text": "governed hello"})
            retry = await session.call_tool(GOVERNED_TOOL, {"text": "governed hello"})

    assert first.isError is False
    receipt = first.meta[RECEIPT_META_KEY]
    assert receipt["permit_id"]  # authorized: server-minted wallet-bounded permit
    assert retry.meta[RECEIPT_META_KEY]["receipt_id"] == receipt["receipt_id"]

    async with httpx.AsyncClient(base_url=live_server) as api:
        verified = await api.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt["receipt_id"]},
            headers=wallet["agent_headers"],
        )
        assert verified.json()["valid"] is True  # signed receipt verifies
        ledger = await api.get(
            f"/v1/billing/ledger/{wallet['agent_wallet_id']}",
            headers=wallet["agent_headers"],
        )
        debits = [
            entry
            for entry in ledger.json()["entries"]
            if GOVERNED_TOOL in entry["description"]
        ]
        assert len(debits) == 1  # charged correctly, executed at most once
