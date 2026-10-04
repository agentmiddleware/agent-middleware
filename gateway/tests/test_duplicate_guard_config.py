"""Unit tests for duplicate guard configuration validation."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import DuplicateGuardMode, Settings
from app.main import app


@pytest_asyncio.fixture
async def client():
    """AsyncClient for testing HTTP endpoints."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


def test_duplicate_guard_mode_enum_values():
    """Duplicate guard mode enum has expected values."""
    assert DuplicateGuardMode.OFF.value == "off"
    assert DuplicateGuardMode.LOG.value == "log"
    assert DuplicateGuardMode.ENFORCE.value == "enforce"
    # Enum members are strings for config compatibility
    assert isinstance(DuplicateGuardMode.LOG, str)


def test_duplicate_guard_mode_invalid_value_rejected():
    """Invalid duplicate guard mode values are rejected at startup."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            VALID_API_KEYS="test-key",
            MCP_UPSTREAM_DUPLICATE_GUARD="enforced",  # typo
        )
    error = str(exc_info.value)
    assert "MCP_UPSTREAM_DUPLICATE_GUARD" in error


def test_duplicate_guard_mode_default_is_log():
    """Default duplicate guard mode is log (observe-only)."""
    settings = Settings(VALID_API_KEYS="test-key")
    assert settings.MCP_UPSTREAM_DUPLICATE_GUARD == DuplicateGuardMode.LOG


def test_duplicate_guard_mode_off_accepted():
    """OFF mode disables duplicate detection."""
    settings = Settings(
        VALID_API_KEYS="test-key",
        MCP_UPSTREAM_DUPLICATE_GUARD="off",
    )
    assert settings.MCP_UPSTREAM_DUPLICATE_GUARD == DuplicateGuardMode.OFF


def test_duplicate_guard_mode_enforce_accepted():
    """ENFORCE mode blocks duplicates."""
    settings = Settings(
        VALID_API_KEYS="test-key",
        MCP_UPSTREAM_DUPLICATE_GUARD="enforce",
    )
    assert settings.MCP_UPSTREAM_DUPLICATE_GUARD == DuplicateGuardMode.ENFORCE


def test_duplicate_guard_window_default():
    """Default duplicate window is 24 hours."""
    settings = Settings(VALID_API_KEYS="test-key")
    assert settings.MCP_UPSTREAM_DUPLICATE_WINDOW_SECONDS == 86400


def test_repeat_window_seconds_rejects_zero_and_negative():
    """repeat_window_seconds must be positive."""
    from app.schemas.trust import PermitCreateRequest

    # Zero is rejected
    with pytest.raises(ValidationError) as exc_info:
        PermitCreateRequest(
            issuer_wallet_id="iss-1",
            subject_wallet_id="sub-1",
            allowed_tools=["tool"],
            scopes=["scope"],
            max_credits=100,
            expires_at="2026-12-31T23:59:59Z",
            repeat_window_seconds=0,
        )
    assert "repeat_window_seconds" in str(exc_info.value)

    # Negative is rejected
    with pytest.raises(ValidationError) as exc_info:
        PermitCreateRequest(
            issuer_wallet_id="iss-1",
            subject_wallet_id="sub-1",
            allowed_tools=["tool"],
            scopes=["scope"],
            max_credits=100,
            expires_at="2026-12-31T23:59:59Z",
            repeat_window_seconds=-1,
        )
    assert "repeat_window_seconds" in str(exc_info.value)


@pytest.mark.anyio
async def test_duplicate_guard_health_endpoint_requires_admin_auth(client):
    """GET /health/duplicate-guard requires bootstrap admin authentication."""
    # Without auth header - should fail
    r = await client.get("/health/duplicate-guard")
    assert r.status_code == 401

    # With bootstrap admin key - should succeed
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS

    r = await client.get("/health/duplicate-guard", headers=BOOTSTRAP_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "mode" in body
    assert "log_mode_blocks" in body
    assert "enforce_mode_blocks" in body
    assert "window_seconds" in body
    assert isinstance(body["enforce_mode_denials_durable"], int)
    scopes = body["metric_scopes"]
    assert scopes["log_mode_blocks"]["durable"] is False
    assert scopes["enforce_mode_blocks"]["durable"] is False
    assert scopes["enforce_mode_denials_durable"]["durable"] is True
    assert scopes["enforce_mode_denials_durable"]["source"] == "receipts"
    assert "enforce_mode_denials_durable_unavailable" not in body


@pytest.mark.anyio
async def test_duplicate_guard_metrics_without_database_report_null(monkeypatch):
    """No database is a supported posture: the process-local counters are
    still served and the durable count is null with a stable reason."""
    from app.services import mcp_dispatch_attempts as module

    monkeypatch.setattr(module, "is_database_configured", lambda: False)
    body = await module.get_duplicate_guard_metrics()
    assert body["enforce_mode_denials_durable"] is None
    assert body["enforce_mode_denials_durable_unavailable"] == "database_not_configured"
    assert "mode" in body
    assert isinstance(body["log_mode_blocks"], int)
    assert isinstance(body["enforce_mode_blocks"], int)


@pytest.mark.anyio
async def test_duplicate_guard_metrics_survive_a_failing_count(monkeypatch):
    """A failing count must not take the whole admin endpoint down, and the
    reason exposes an exception type only, never a message."""
    from app.services import mcp_dispatch_attempts as module

    def explode():
        raise RuntimeError("DATABASE_URL not configured. simulated secret-ish detail")

    monkeypatch.setattr(module, "get_session_factory", explode)
    body = await module.get_duplicate_guard_metrics()
    assert body["enforce_mode_denials_durable"] is None
    assert body["enforce_mode_denials_durable_unavailable"] == "RuntimeError"
    assert "secret-ish" not in str(body)


@pytest.mark.anyio
async def test_duplicate_guard_metrics_bound_the_count_with_a_timeout(monkeypatch):
    """A hung database must not hang the admin endpoint."""
    import asyncio

    from app.services import mcp_dispatch_attempts as module

    async def hang() -> int:
        await asyncio.sleep(5)
        return 0

    monkeypatch.setattr(module, "_query_duplicate_denial_receipts", hang)
    monkeypatch.setattr(module, "DUPLICATE_DENIAL_COUNT_TIMEOUT_SECONDS", 0.05)
    body = await module.get_duplicate_guard_metrics()
    assert body["enforce_mode_denials_durable"] is None
    assert body["enforce_mode_denials_durable_unavailable"] == "TimeoutError"


@pytest.mark.anyio
async def test_duplicate_guard_health_endpoint_rejects_non_admin_keys(client):
    """A wallet-scoped key is authenticated but not a bootstrap admin."""
    from tests.test_trust_helpers import provision_agent_wallet

    provisioned = await provision_agent_wallet(client)
    r = await client.get(
        "/health/duplicate-guard", headers=provisioned["agent_headers"]
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "admin_access_denied"


def test_repeat_window_seconds_rejects_excessive_values():
    """repeat_window_seconds has a maximum of 365 days."""
    from app.schemas.trust import PermitCreateRequest

    # 365 days is accepted
    PermitCreateRequest(
        issuer_wallet_id="iss-1",
        subject_wallet_id="sub-1",
        allowed_tools=["tool"],
        scopes=["scope"],
        max_credits=100,
        expires_at="2026-12-31T23:59:59Z",
        repeat_window_seconds=31536000,
    )

    # Over 365 days is rejected
    with pytest.raises(ValidationError) as exc_info:
        PermitCreateRequest(
            issuer_wallet_id="iss-1",
            subject_wallet_id="sub-1",
            allowed_tools=["tool"],
            scopes=["scope"],
            max_credits=100,
            expires_at="2026-12-31T23:59:59Z",
            repeat_window_seconds=31536001,
        )
    assert "repeat_window_seconds" in str(exc_info.value)


@pytest.mark.parametrize("value", [True, False])
def test_repeat_window_seconds_rejects_booleans(value):
    from app.schemas.trust import PermitCreateRequest

    with pytest.raises(ValidationError) as exc_info:
        PermitCreateRequest(
            issuer_wallet_id="iss-1",
            subject_wallet_id="sub-1",
            max_credits=100,
            expires_at="2026-12-31T23:59:59Z",
            repeat_window_seconds=value,
        )

    error = exc_info.value.errors()[0]
    assert error["loc"] == ("repeat_window_seconds",)
    assert "not a boolean" in error["msg"]


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, None),
        ({"repeat_window_seconds": None}, None),
        ({"repeat_window_seconds": 1}, 1),
        ({"repeat_window_seconds": 31536000}, 31536000),
        ({"repeat_window_seconds": "60"}, 60),
        ({"repeat_window_seconds": 60.0}, 60),
    ],
)
def test_repeat_window_seconds_preserves_valid_values(fields, expected):
    from app.schemas.trust import PermitCreateRequest

    request = PermitCreateRequest(
        issuer_wallet_id="iss-1",
        subject_wallet_id="sub-1",
        max_credits=100,
        expires_at="2026-12-31T23:59:59Z",
        **fields,
    )
    assert request.repeat_window_seconds == expected
    assert type(request.repeat_window_seconds) is type(expected)


@pytest.mark.anyio
@pytest.mark.parametrize("value", [True, False])
async def test_permit_api_rejects_boolean_repeat_window(client, value):
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS

    response = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": "iss-1",
            "subject_wallet_id": "sub-1",
            "max_credits": 100,
            "expires_at": "2026-12-31T23:59:59Z",
            "repeat_window_seconds": value,
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "boolean-repeat-window"},
    )
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "repeat_window_seconds"]
    assert "not a boolean" in error["msg"]
