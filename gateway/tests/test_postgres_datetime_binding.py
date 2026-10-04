"""Regression coverage for tz-aware datetimes reaching naive DB columns.

Every datetime column in this schema is ``TIMESTAMP WITHOUT TIME ZONE`` (see
``migrations/versions`` — there is not a single ``DateTime(timezone=True)``).
SQLite accepts a tz-aware value for such a column silently; asyncpg rejects it
with ``DataError: invalid input for query argument ... (can't subtract
offset-naive and offset-aware datetimes)``.

Because the whole test suite runs on SQLite, a call site reaching for
``datetime.now(timezone.utc)`` instead of ``app.core.time.utc_now()`` used to
pass CI and then 500 on Railway. That is what took down ``POST /v1/permits``
and the governed ``POST /mcp/messages`` denial path: both sign, and signing
lazily inserts ``signing_keys.activated_at``.

The dialect-agnostic bind guard in ``app.db.database`` is the fix. The unit
tests below run everywhere; ``TestPostgresTrustLoop`` exercises the real
driver and is skipped unless ``TEST_POSTGRES_URL`` is set (CI sets it).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.core.time import to_naive_utc, utc_now
from app.db.database import _normalize_bind_parameters


class TestToNaiveUtc:
    def test_naive_input_is_unchanged(self):
        naive = datetime(2026, 8, 4, 12, 0, 0)
        assert to_naive_utc(naive) is naive

    def test_aware_utc_loses_tzinfo_but_keeps_instant(self):
        aware = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
        assert to_naive_utc(aware) == datetime(2026, 8, 4, 12, 0, 0)
        assert to_naive_utc(aware).tzinfo is None

    def test_non_utc_offset_is_converted_not_truncated(self):
        # 12:00-05:00 is 17:00 UTC. Dropping tzinfo without converting would
        # silently shift the stored instant by five hours.
        aware = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
        assert to_naive_utc(aware) == datetime(2026, 8, 4, 17, 0, 0)

    def test_utc_now_is_naive(self):
        assert utc_now().tzinfo is None


class TestNormalizeBindParameters:
    def test_tuple_datetimes_are_normalized(self):
        aware = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
        result = _normalize_bind_parameters(("key-1", aware, None))
        assert result == ("key-1", datetime(2026, 8, 4, 12, 0, 0), None)
        assert result[1].tzinfo is None

    def test_dict_datetimes_are_normalized(self):
        aware = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
        result = _normalize_bind_parameters({"id": "k", "activated_at": aware})
        assert result["activated_at"].tzinfo is None

    def test_untouched_parameters_are_returned_identically(self):
        # The common case must not pay for a copy.
        params = ("a", 1, datetime(2026, 8, 4, 12, 0, 0), None)
        assert _normalize_bind_parameters(params) is params

    def test_non_datetime_values_pass_through(self):
        params = ("a", 1, 2.5, None, b"bytes")
        assert _normalize_bind_parameters(params) is params

    def test_scalar_and_none_parameters_are_safe(self):
        assert _normalize_bind_parameters(None) is None
        assert _normalize_bind_parameters("raw") == "raw"


POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "").strip()


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set; Postgres-dialect coverage skipped",
)
class TestPostgresTrustLoop:
    """End-to-end trust loop against the real asyncpg driver.

    This is the coverage that was missing: on SQLite these paths passed while
    every one of them 500'd on Postgres.
    """

    @pytest.fixture(scope="module")
    def pg_app(self):
        import base64
        import importlib
        import subprocess

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

        previous = {
            k: os.environ.get(k)
            for k in (
                "DATABASE_URL",
                "TRUST_SIGNING_PRIVATE_KEY_B64",
                "TRUST_MODE_ENABLED",
                "ALLOW_LEGACY_UNPERMITTED_MCP",
                "ENABLE_DOGFOOD_TOOL",
                "VALID_API_KEYS",
            )
        }
        os.environ["DATABASE_URL"] = POSTGRES_URL
        os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = base64.b64encode(raw).decode()
        os.environ["TRUST_MODE_ENABLED"] = "true"
        os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
        os.environ["ENABLE_DOGFOOD_TOOL"] = "true"
        os.environ["VALID_API_KEYS"] = "pg-test-key"

        subprocess.run(["alembic", "upgrade", "head"], check=True, env=dict(os.environ))

        # get_settings() and the engine are process-level singletons primed by
        # conftest's SQLite defaults; rebuild both against Postgres.
        from app.core import config as config_module
        from app.db import database as database_module

        config_module.get_settings.cache_clear()
        database_module._engine = None
        database_module._session_factory = None

        import app.main as main_module

        importlib.reload(main_module)

        yield main_module.app

        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config_module.get_settings.cache_clear()

    @pytest.fixture
    async def client(self, pg_app):
        """Give each test its own engine, created and disposed in its own loop.

        The asyncpg pool binds to the running loop; a module-scoped engine
        would be torn down after its loop closed ("Event loop is closed").
        """
        import httpx

        from app.db import database as database_module

        database_module._engine = None
        database_module._session_factory = None

        transport = httpx.ASGITransport(app=pg_app, raise_app_exceptions=False)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://pg-test"
            ) as c:
                yield c
        finally:
            engine = database_module._engine
            if engine is not None:
                await engine.dispose()
            database_module._engine = None
            database_module._session_factory = None

    @pytest.fixture
    async def wallets(self, client):
        headers = {"X-API-Key": "pg-test-key"}
        sponsor = await client.post(
            "/v1/billing/wallets/sponsor",
            headers=headers,
            json={
                "sponsor_name": "PG Regression",
                "email": "pg@example.com",
                "initial_credits": 1000,
            },
        )
        assert sponsor.status_code == 201, sponsor.text
        sponsor_id = sponsor.json()["wallet_id"]

        agent = await client.post(
            "/v1/billing/wallets/agent",
            headers=headers,
            json={
                "sponsor_wallet_id": sponsor_id,
                "agent_id": "agt-pg-regression",
                "budget_credits": 100,
            },
        )
        assert agent.status_code == 201, agent.text
        return sponsor_id, agent.json()["wallet_id"]

    @pytest.mark.asyncio
    async def test_create_permit_does_not_500_on_postgres(self, client, wallets):
        """The exact live failure: 500 from the signing-key / permit inserts."""
        sponsor_id, agent_id = wallets
        response = await client.post(
            "/v1/permits",
            headers={"X-API-Key": "pg-test-key", "Idempotency-Key": "pg-permit-1"},
            json={
                "issuer_wallet_id": sponsor_id,
                "subject_wallet_id": agent_id,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": "10",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "active"
        assert body["signature"]
        # Persisted timestamps must come back naive (no offset suffix).
        assert "+" not in body["issued_at"]

    @pytest.mark.asyncio
    async def test_api_key_rotation_revokes_old_key_on_postgres(self, client, wallets):
        """Revocation metadata must survive the atomic asyncpg update."""
        _, agent_id = wallets
        headers = {"X-API-Key": "pg-test-key"}
        created = await client.post(
            "/v1/api-keys",
            headers=headers,
            json={"wallet_id": agent_id, "key_name": "pg-rotation-old"},
        )
        assert created.status_code == 201, created.text
        old_key = created.json()

        rotated = await client.post(
            "/v1/api-keys/rotate",
            headers=headers,
            json={
                "wallet_id": agent_id,
                "key_id": old_key["key_id"],
                "revoke_old": True,
                "reason": "pg_compromise_response",
            },
        )
        assert rotated.status_code == 200, rotated.text
        new_key = rotated.json()["new_key"]
        assert rotated.json()["revoked_keys"] == [old_key["key_id"]]

        old_auth = await client.get(
            f"/v1/billing/wallets/{agent_id}",
            headers={"X-API-Key": old_key["api_key"]},
        )
        new_auth = await client.get(
            f"/v1/billing/wallets/{agent_id}",
            headers={"X-API-Key": new_key["api_key"]},
        )
        assert old_auth.status_code == 403
        assert new_auth.status_code == 200

    @pytest.mark.asyncio
    async def test_permit_with_non_utc_offset_normalizes_correctly(
        self, client, wallets
    ):
        """Regression: non-UTC offset must be converted, not truncated.

        2099-01-01T00:00:00+05:00 is 2098-12-31T19:00:00 UTC.
        The signed payload and persisted row must both use the naive UTC
        instant so that signature verification and governed invocation
        succeed on every dialect (SQLite and PostgreSQL/asyncpg).
        """
        sponsor_id, agent_id = wallets
        response = await client.post(
            "/v1/permits",
            headers={"X-API-Key": "pg-test-key", "Idempotency-Key": "pg-permit-offset"},
            json={
                "issuer_wallet_id": sponsor_id,
                "subject_wallet_id": agent_id,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": "10",
                "expires_at": "2099-01-01T00:00:00+05:00",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "active"
        assert body["signature"]

        # The stored expires_at must be the naive UTC instant, not the raw
        # local wall-clock time (which would silently shift by 5 hours).
        assert body["expires_at"] == "2098-12-31T19:00:00"
        assert "+" not in body["expires_at"]

        # Governed invocation with this permit must succeed, which exercises
        # signature verification against the normalized timestamp.
        invoke = await client.post(
            "/mcp/messages",
            headers={
                "X-API-Key": "pg-test-key",
                "Idempotency-Key": "pg-invoke-offset",
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "offset regression"},
                    "mcpContext": {
                        "wallet_id": agent_id,
                        "permit_id": body["permit_id"],
                    },
                },
            },
        )
        assert invoke.status_code == 200, invoke.text
        result = invoke.json()
        assert "error" not in result, result
        assert result["result"]["receipt"]["receipt_id"]

    @pytest.mark.asyncio
    async def test_unpermitted_mcp_call_is_denied_not_500(self, client, wallets):
        """Strict trust mode must return a JSON-RPC denial, not crash."""
        _, agent_id = wallets
        response = await client.post(
            "/mcp/messages",
            headers={"X-API-Key": "pg-test-key"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "denied"},
                    "mcpContext": {"wallet_id": agent_id},
                },
            },
        )
        assert response.status_code == 200, response.text
        error = response.json()["error"]
        assert error["code"] == -32003
        assert error["message"] == "permit_required"

    @pytest.mark.asyncio
    async def test_governed_invoke_writes_receipt_and_audit_event(
        self, client, wallets
    ):
        """Receipt and audit-chain inserts also bind datetimes; exercise them."""
        sponsor_id, agent_id = wallets
        headers = {"X-API-Key": "pg-test-key"}

        permit = await client.post(
            "/v1/permits",
            headers={**headers, "Idempotency-Key": "pg-permit-2"},
            json={
                "issuer_wallet_id": sponsor_id,
                "subject_wallet_id": agent_id,
                "allowed_tools": ["partner.notes.write"],
                "max_credits": "10",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        assert permit.status_code == 201, permit.text
        permit_id = permit.json()["permit_id"]

        invoke = await client.post(
            "/mcp/messages",
            headers={**headers, "Idempotency-Key": "pg-invoke-1"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "partner.notes.write",
                    "arguments": {"text": "hello from postgres"},
                    "mcpContext": {"wallet_id": agent_id, "permit_id": permit_id},
                },
            },
        )
        assert invoke.status_code == 200, invoke.text
        result = invoke.json()
        assert "error" not in result, result
        assert result["result"]["receipt"]["receipt_id"]

        events = await client.get(
            "/v1/audit/events", headers=headers, params={"wallet_id": agent_id}
        )
        assert events.status_code == 200, events.text
        assert events.json()["events"], "governed invoke must leave an audit event"
