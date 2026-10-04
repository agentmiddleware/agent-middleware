from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import mcp as mcp_router
from app.services.signing_keys import get_signing_key_service
from scripts.agent_ops_war_room_demo import run_war_room


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def strict_trust_mode(monkeypatch):
    raw_private_key = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
    monkeypatch.setattr(mcp_router.settings, "TRUST_MODE_ENABLED", True)
    monkeypatch.setattr(mcp_router.settings, "ALLOW_LEGACY_UNPERMITTED_MCP", False)
    monkeypatch.setattr(
        mcp_router.settings,
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        base64.b64encode(raw_private_key).decode(),
    )
    signing_keys = get_signing_key_service()
    previous_private_key = signing_keys._private_key
    previous_key_id = signing_keys._key_id
    signing_keys._private_key = None
    try:
        yield
    finally:
        signing_keys._private_key = previous_private_key
        signing_keys._key_id = previous_key_id


@pytest.mark.anyio
async def test_agent_ops_war_room_demo_proves_control_plane_loop(
    client,
    clean_database,
    strict_trust_mode,
):
    result = await run_war_room(
        client=client,
        bootstrap_api_key="test-key",
        emit=False,
    )

    assert result["status"] == "pass"
    assert result["agent"]["wallet_id"].startswith("wallet-")
    assert result["permit"]["permit_id"].startswith("permit-")
    assert result["invoke"]["receipt"]["outcome"] == "success"
    assert result["replay"]["same_receipt"] is True
    assert result["ledger"]["matching_debits"] == 1
    assert result["self_inspection"]["permits"] >= 1
    assert result["self_inspection"]["receipts"] >= 1
    assert result["self_inspection"]["audit_events"] >= 1
    assert result["audit"]["chain"]["valid"] is True
    assert result["denial"]["reason"] == "permit_tool_not_allowed"
    assert (
        result["denial"]["ledger_debits_after"] == result["ledger"]["matching_debits"]
    )


def test_agent_ops_war_room_cli_json_proves_control_plane_loop(tmp_path: Path):
    # A subprocess must never share the parent pytest worker's SQLite file.
    # Replacing or mutating that file while SQLAlchemy still has pooled parent
    # connections makes every later trust test order-dependent.
    subprocess_env = os.environ.copy()
    subprocess_env["DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{tmp_path / 'agent-ops-demo.db'}"
    )
    result = subprocess.run(
        [sys.executable, "scripts/agent_ops_war_room_demo.py", "--assert", "--json"],
        cwd=ROOT,
        env=subprocess_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["status"] == "pass"
    assert proof["permit"]["permit_id"].startswith("permit-")
    assert proof["invoke"]["receipt"]["outcome"] == "success"
    assert proof["replay"]["same_receipt"] is True
    assert proof["ledger"]["matching_debits"] == 1
    assert proof["audit"]["chain"]["valid"] is True
    assert proof["denial"]["reason"] == "permit_tool_not_allowed"
