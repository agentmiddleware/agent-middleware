"""Production-like trust posture and public-surface guards.

Default conftest stays permissive for unit speed. This module (and the CI
``production_trust`` job) explicitly engages production trust flags.

These tests must not leave process-wide settings/env polluted for later
modules in the same pytest session.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.trust_mode import (
    describe_permissive_trust_mode,
    validate_trust_mode_config,
)
from app.main import app
from app.services.mcp_phase9_tools import (
    DEFAULT_MCP_STUB_SERVICE_IDS,
    PROOF_SURFACE_MCP_STUB_IDS,
    sync_proof_surface_mcp_registration,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Deterministic non-secret 32-byte material so production-like validation can
# exercise the real Ed25519 key shape without loading a production secret.
_TEST_SIGNING_PRIVATE_KEY_B64 = base64.b64encode(bytes(range(32))).decode()

_PROD_TRUST_ENV = {
    "TRUST_MODE_ENABLED": "true",
    "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
    "ENABLE_PROOF_SURFACES": "false",
    "ENVIRONMENT": "production",
    "DEBUG": "false",
    "WEBAUTHN_ALLOW_MOCK": "false",
    "TRUST_SIGNING_PRIVATE_KEY_B64": _TEST_SIGNING_PRIVATE_KEY_B64,
    "PUBLIC_URL": "https://api.thisisatest.tech",
    # A production posture is not complete without the relational database the
    # wallets, permits, receipts, and ledger live in. It is PostgreSQL because
    # validate_trust_mode_config refuses SQLite here: SQLAlchemy silently drops
    # SELECT ... FOR UPDATE on SQLite, which the money and permit paths rely on.
    "DATABASE_URL": "postgresql+asyncpg://user:pw@db.internal:5432/trust",
}


def _bind_settings_aliases(cfg) -> dict:
    import app.main as main_mod
    import app.routers.discover as discover_mod
    import app.routers.well_known as well_known_mod

    previous = {
        "main": main_mod.settings,
        "discover": discover_mod.settings,
        "well_known": well_known_mod.settings,
    }
    main_mod.settings = cfg
    discover_mod.settings = cfg
    well_known_mod.settings = cfg
    return previous


def _restore_settings_aliases(previous: dict) -> None:
    import app.main as main_mod
    import app.routers.discover as discover_mod
    import app.routers.well_known as well_known_mod

    main_mod.settings = previous["main"]
    discover_mod.settings = previous["discover"]
    well_known_mod.settings = previous["well_known"]


@pytest.fixture
def production_trust_flags():
    """Engage the production trust flag set without flipping the whole suite."""
    saved_env = {key: os.environ.get(key) for key in _PROD_TRUST_ENV}
    previous_aliases: dict | None = None
    try:
        for key, value in _PROD_TRUST_ENV.items():
            os.environ[key] = value
        get_settings.cache_clear()
        cfg = get_settings()
        assert cfg.TRUST_MODE_ENABLED is True
        assert cfg.ALLOW_LEGACY_UNPERMITTED_MCP is False
        assert cfg.ENABLE_PROOF_SURFACES is False
        assert cfg.ENABLE_DOGFOOD_TOOL is False
        previous_aliases = _bind_settings_aliases(cfg)
        sync_proof_surface_mcp_registration()
        from app.services.dogfood_tool import sync_dogfood_tool_registration

        sync_dogfood_tool_registration()
        yield cfg
    finally:
        for key, old in saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        get_settings.cache_clear()
        if previous_aliases is not None:
            _restore_settings_aliases(previous_aliases)
        # Rebind aliases to a fresh Settings matching restored env.
        restored = get_settings()
        _bind_settings_aliases(restored)
        sync_proof_surface_mcp_registration()
        from app.services.dogfood_tool import sync_dogfood_tool_registration

        sync_dogfood_tool_registration()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.production_trust
def test_production_trust_flags_are_strict(production_trust_flags):
    cfg = production_trust_flags
    validate_trust_mode_config(
        environment=cfg.ENVIRONMENT,
        trust_mode_enabled=cfg.TRUST_MODE_ENABLED,
        signing_private_key_b64=cfg.TRUST_SIGNING_PRIVATE_KEY_B64,
        allow_legacy_unpermitted_mcp=cfg.ALLOW_LEGACY_UNPERMITTED_MCP,
        debug=cfg.DEBUG,
        webauthn_allow_mock=cfg.WEBAUTHN_ALLOW_MOCK,
        enable_proof_surfaces=cfg.ENABLE_PROOF_SURFACES,
        database_url=cfg.DATABASE_URL,
    )
    assert (
        describe_permissive_trust_mode(
            trust_mode_enabled=cfg.TRUST_MODE_ENABLED,
            allow_legacy_unpermitted_mcp=cfg.ALLOW_LEGACY_UNPERMITTED_MCP,
        )
        is None
    )


@pytest.mark.production_trust
def test_fresh_production_import_omits_passkey_routes():
    """Production env must gate AWI before the process-global app is built."""
    script = """
import json
from app.main import app

paths = sorted(app.openapi().get("paths", {}))
print(json.dumps(paths))
"""
    env = os.environ.copy()
    env.update(_PROD_TRUST_ENV)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    paths = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert "/v1/permits" in paths
    assert "/v1/awi/passkey/challenge" not in paths
    assert "/v1/awi/passkey/verify" not in paths
    assert not any(path.startswith("/v1/awi/") for path in paths)


@pytest.mark.parametrize(
    "environment,proof_surfaces,upstream_enabled",
    [
        ("local", "false", "false"),
        ("local", "true", "true"),
        ("production", "false", "true"),
    ],
)
def test_fresh_app_keeps_action_issuance_frozen(
    environment, proof_surfaces, upstream_enabled
):
    """Neither startup flags nor an upstream config advertise unbound issuance."""
    script = """
import asyncio
import json
from httpx import ASGITransport, AsyncClient
from app.main import app

async def probe():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = [await client.post("/v1/action-permits", json={}, headers=headers)
                     for headers in ({}, {"X-API-Key": "test-key"})]
    print(json.dumps({"paths": sorted(app.openapi()["paths"]),
                      "status_codes": [response.status_code for response in responses]}))

asyncio.run(probe())
"""
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    env.update(_PROD_TRUST_ENV)
    env.update(
        ENVIRONMENT=environment,
        ENABLE_PROOF_SURFACES=proof_surfaces,
        MCP_UPSTREAM_ENABLED=upstream_enabled,
        MCP_UPSTREAM_URL="https://fixture.invalid/mcp",
        MCP_UPSTREAM_TOOL_NAME="pay",
        MCP_UPSTREAM_PUBLIC_TOOL_ID="partner.pay",
        MCP_UPSTREAM_BEARER_TOKEN="synthetic-fixture-token",
        STATE_BACKEND="memory",
        VALID_API_KEYS="test-key",
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert "/v1/permits" in observed["paths"]
    assert "/v1/action-permits" not in observed["paths"]
    assert observed["status_codes"] == [404, 404]


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_agent_json_reports_proof_surfaces_off(client, production_trust_flags):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    body = resp.json()
    agent_first = body.get("agent_first") or {}
    assert agent_first.get("proof_surfaces_enabled") is False
    # Unmounted workloads are not discovery: the production manifest publishes
    # no proof-surface catalog at all.
    assert body.get("proof_surfaces") == []


_OPERATOR_KEY_HEADERS = {"X-API-Key": "test-key"}


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_tools_json_omits_proof_stubs_under_prod_flags(
    client, production_trust_flags
):
    resp = await client.get("/mcp/tools.json", headers=_OPERATOR_KEY_HEADERS)
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["tools"]}
    assert names.isdisjoint(PROOF_SURFACE_MCP_STUB_IDS)
    assert names.isdisjoint(DEFAULT_MCP_STUB_SERVICE_IDS)
    assert not any(name.startswith("awi_") for name in names)
    assert "partner.notes.write" not in names


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_anonymous_tool_catalogs_are_401_in_production(
    client, production_trust_flags
):
    for path in (
        "/mcp/tools.json",
        "/mcp/tools",
        "/mcp/tools/partner.echo",
        "/v1/discover",
        "/.well-known/mcp/tools.json",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401, path
        body = resp.json()["detail"]
        assert body["error"] == "missing_credentials"


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_unknown_key_cannot_read_tool_catalog(client, production_trust_flags):
    resp = await client.get(
        "/mcp/tools.json", headers={"X-API-Key": "bogusbogus-not-a-real-key"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "invalid_api_key"


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_operator_key_can_read_tool_catalog(client, production_trust_flags):
    resp = await client.get("/v1/discover", headers=_OPERATOR_KEY_HEADERS)
    assert resp.status_code == 200
    assert "mcp_tools" in resp.json()


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_anonymous_cannot_invoke_or_mint_in_production(
    client, production_trust_flags
):
    messages = await client.post(
        "/mcp/messages", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert messages.status_code == 401
    invoke = await client.post(
        "/mcp/tools/partner.echo/invoke", json={"arguments": {"message": "hi"}}
    )
    assert invoke.status_code == 401
    minted = await client.post("/v1/dev-keys/self-provision", json={})
    assert minted.status_code in {403, 404}


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_public_mcp_is_404_in_production_even_if_flag_on(
    client, production_trust_flags, monkeypatch
):
    monkeypatch.setenv("ENABLE_PUBLIC_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    resp = await client.post(
        "/mcp/public",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "lockdown-test", "version": "0"},
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 404


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_receipt_keys_remain_public_in_production(client, production_trust_flags):
    keys = await client.get("/.well-known/trust-keys.json")
    assert keys.status_code != 401
    assert keys.status_code != 403
    assert keys.status_code in {200, 503}
    jwks = await client.get("/.well-known/jwks.json")
    assert jwks.status_code != 401
    assert jwks.status_code != 403
    assert jwks.status_code in {200, 503}
    health = await client.get("/health")
    assert health.status_code == 200


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_agent_json_does_not_invite_public_use(client, production_trust_flags):
    resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    try_it = resp.json()["try_it"]
    assert try_it["mode"] == "private_experiment"
    assert try_it["requires_live_credentials"] is True
    assert try_it["public_tool_catalog"] is False
    assert try_it["public_self_serve"] is False
    assert "repository_access" not in try_it


@pytest.mark.production_trust
@pytest.mark.anyio
async def test_health_dependencies_reports_proof_surfaces_off(
    client, production_trust_flags
):
    resp = await client.get("/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("enable_proof_surfaces") is False
    # The production payload is the wedge projection: dogfood/test-tooling
    # flags and per-service simulation modes are not published there.
    assert "enable_dogfood_tool" not in body
    assert "simulation_modes" not in body
