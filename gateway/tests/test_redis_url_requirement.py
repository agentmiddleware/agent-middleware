"""Tests for REDIS_URL requirement in production-like environments (issue 288).

Tests the maintainer decision: production-like environments refuse to boot
without REDIS_URL unless ALLOW_IN_MEMORY_STORE=true is set, in which case
they boot but report as degraded.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.health import gather_dependency_report
from app.core.runtime_degradation import (
    REASON_IN_MEMORY_STORE_ALLOWED,
    REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED,
    get_runtime_degradation,
    in_memory_store_allowed_in_production_like,
    reset_runtime_degradation,
)
from app.core.trust_mode import (
    TrustModeGuardrailError,
    validate_trust_mode_config,
)
from app.main import app


@pytest.fixture(autouse=True)
def _reset_degradation():
    reset_runtime_degradation()
    yield
    reset_runtime_degradation()


def _valid_config_baseline() -> dict:
    """Minimal valid production-like config for positive-path tests."""
    return {
        "environment": "production",
        "trust_mode_enabled": True,
        "signing_private_key_b64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "allow_legacy_unpermitted_mcp": False,
        "debug": False,
        "webauthn_allow_mock": False,
        "enable_proof_surfaces": False,
        "static_dev_api_keys": "",
        "enable_dev_key_self_provision": False,
        "enable_demo_tenant": False,
        "enable_public_mcp_endpoint": False,
        "allow_private_network_targets": False,
        "allow_unsafe_host_python_sandbox": False,
        "behavioral_sandbox_python_backend": "",
        "database_url": "postgresql://localhost/test",
        "cors_origins": "",
        "enable_dogfood_tool": False,
        "enable_dogfood_second_tool": False,
    }


# --- Boot refusal tests ------------------------------------------------------


def test_production_like_without_redis_url_refuses_to_boot():
    """Production-like environments must have REDIS_URL or explicit opt-out."""
    config = _valid_config_baseline()
    config["redis_url"] = ""
    config["allow_in_memory_store"] = False

    with pytest.raises(TrustModeGuardrailError) as exc_info:
        validate_trust_mode_config(**config)

    error_message = str(exc_info.value)
    assert "REDIS_URL is required in production-like environments" in error_message
    assert "ALLOW_IN_MEMORY_STORE=true" in error_message
    assert "shared rate limiting" in error_message
    # No em or en dashes in user facing copy
    assert "\u2014" not in error_message and "\u2013" not in error_message


@pytest.mark.parametrize("environment", ["production", "staging", "prod-eu", "stage"])
def test_every_production_like_environment_refuses_without_redis_url(environment):
    """Every production-like environment name refuses to boot without REDIS_URL."""
    config = _valid_config_baseline()
    config["environment"] = environment
    config["redis_url"] = ""
    config["allow_in_memory_store"] = False

    with pytest.raises(TrustModeGuardrailError) as exc_info:
        validate_trust_mode_config(**config)

    assert "REDIS_URL is required" in str(exc_info.value)


def test_whitespace_only_redis_url_is_treated_as_not_set():
    """Whitespace-only REDIS_URL is the same as empty."""
    config = _valid_config_baseline()
    config["redis_url"] = "   "
    config["allow_in_memory_store"] = False

    with pytest.raises(TrustModeGuardrailError) as exc_info:
        validate_trust_mode_config(**config)

    assert "REDIS_URL is required" in str(exc_info.value)


# --- Positive-path tests -----------------------------------------------------


def test_production_like_with_redis_url_boots_successfully():
    """Production-like environments with REDIS_URL set boot normally."""
    config = _valid_config_baseline()
    config["redis_url"] = "redis://localhost:6379/0"
    config["allow_in_memory_store"] = False

    # Should not raise
    validate_trust_mode_config(**config)


def test_production_like_with_opt_out_boots_successfully():
    """Production-like environments with ALLOW_IN_MEMORY_STORE=true boot."""
    config = _valid_config_baseline()
    config["redis_url"] = ""
    config["allow_in_memory_store"] = True

    # Should not raise
    validate_trust_mode_config(**config)


# --- Development/test environment unchanged ----------------------------------


@pytest.mark.parametrize("environment", ["development", "test", "local", "ci", ""])
def test_local_environment_boots_without_redis_url(environment):
    """Local/dev/test environments do not require REDIS_URL."""
    config = _valid_config_baseline()
    config["environment"] = environment
    config["redis_url"] = ""
    config["allow_in_memory_store"] = False
    # Local environments are permissive by design
    config["trust_mode_enabled"] = False
    config["allow_legacy_unpermitted_mcp"] = True
    config["enable_proof_surfaces"] = True
    config["debug"] = True

    # Should not raise
    validate_trust_mode_config(**config)


# --- Degradation reporting ---------------------------------------------------


def test_opt_out_is_reported_as_degraded(monkeypatch):
    """ALLOW_IN_MEMORY_STORE in production-like is reported as degraded."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    snap = get_runtime_degradation()

    assert snap["degraded"] is True
    assert REASON_IN_MEMORY_STORE_ALLOWED in snap["reasons"]
    assert snap["in_memory_store_allowed"] is True


def test_opt_out_helper_recognizes_production_like_with_opt_in(monkeypatch):
    """Helper correctly identifies production-like + opt-out."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    assert in_memory_store_allowed_in_production_like() is True


def test_opt_out_helper_returns_false_when_redis_url_is_set(monkeypatch):
    """Helper returns False when REDIS_URL is set (not using in-memory)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    assert in_memory_store_allowed_in_production_like() is False


def test_opt_out_helper_returns_false_in_local_environment(monkeypatch):
    """Helper returns False in local environments (not degraded)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    assert in_memory_store_allowed_in_production_like() is False


def test_degradation_includes_both_redis_not_configured_and_opt_in(monkeypatch):
    """Degradation reasons include both memory-only and opt-in."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    snap = get_runtime_degradation()

    assert snap["degraded"] is True
    # Both reasons are present: no REDIS_URL and explicit opt-in
    assert REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED in snap["reasons"]
    assert REASON_IN_MEMORY_STORE_ALLOWED in snap["reasons"]


@pytest.mark.anyio
async def test_health_dependencies_reports_opt_out_as_degraded(monkeypatch):
    """Health endpoint reports opt-out as degraded."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    report = await gather_dependency_report()

    assert report["runtime_degradation"]["degraded"] is True
    assert REASON_IN_MEMORY_STORE_ALLOWED in report["runtime_degradation"]["reasons"]
    assert report["status"] == "degraded"
    assert "runtime_degradation" in report["unhealthy"]


@pytest.mark.anyio
async def test_health_dependencies_endpoint_shows_opt_out_degradation(monkeypatch):
    """Health endpoint HTTP response includes opt-out degradation."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/health/dependencies")

    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_degradation"]["degraded"] is True
    assert REASON_IN_MEMORY_STORE_ALLOWED in body["runtime_degradation"]["reasons"]
    assert body["status"] == "degraded"


@pytest.mark.anyio
async def test_readiness_endpoint_is_degraded_with_opt_out(monkeypatch):
    """The /health/ready endpoint reports degraded when opt-out is used."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    monkeypatch.setattr(settings, "ALLOW_IN_MEMORY_STORE", True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_degradation"]["degraded"] is True
    assert REASON_IN_MEMORY_STORE_ALLOWED in body["runtime_degradation"]["reasons"]


# --- Stable reason code ------------------------------------------------------


def test_stable_reason_code_is_not_renamed():
    """The reason code is part of the operator contract; do not rename it."""
    assert REASON_IN_MEMORY_STORE_ALLOWED == "in_memory_store_allowed_in_production"
