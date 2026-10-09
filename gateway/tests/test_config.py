"""Focused unit tests for app.core.config.

Covers Settings defaults, environment-variable loading, the EXCHANGE_RATE
validator, DuplicateGuardMode parsing, get_settings caching, and
public_api_origin normalization. No network, no database, no app import.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import (
    DuplicateGuardMode,
    Settings,
    get_settings,
    public_api_origin,
)


def test_defaults_match_local_strict_posture(monkeypatch):
    # conftest.py exports test-only env overrides (STATE_BACKEND=memory,
    # permissive trust flags, ...). Drop them so this asserts the code
    # defaults, not the suite posture.
    for key in (
        "STATE_BACKEND",
        "TRUST_MODE_ENABLED",
        "ALLOW_LEGACY_UNPERMITTED_MCP",
        "DATABASE_URL",
        "VALID_API_KEYS",
        "ENABLE_PROOF_SURFACES",
        "RATE_LIMIT_PER_MINUTE",
        "ALLOW_METADATA_CREATE_ALL",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings()

    assert settings.APP_NAME == "Agent Middleware API"
    assert settings.ENVIRONMENT == "local"
    assert settings.DEBUG is False
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8000
    assert settings.PUBLIC_URL == ""
    assert settings.STATE_BACKEND == "auto"
    assert settings.TRUST_MODE_ENABLED is True
    assert settings.ALLOW_LEGACY_UNPERMITTED_MCP is False
    assert settings.MCP_UPSTREAM_DUPLICATE_GUARD == DuplicateGuardMode.LOG
    assert settings.EXCHANGE_RATE == Decimal("1000.0")
    assert settings.MAX_REQUEST_BODY_BYTES == 1_048_576


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("PORT", "9001")
    monkeypatch.setenv("TRUST_MODE_ENABLED", "false")

    settings = Settings()

    assert settings.ENVIRONMENT == "staging"
    assert settings.PORT == 9001
    assert settings.TRUST_MODE_ENABLED is False


def test_explicit_constructor_kwargs_win():
    settings = Settings(PORT=9002, ENVIRONMENT="test")

    assert settings.PORT == 9002
    assert settings.ENVIRONMENT == "test"


@pytest.mark.parametrize("bad_rate", ["0", "0.0", "-1", "-1000.5"])
def test_exchange_rate_zero_or_negative_is_rejected(bad_rate):
    with pytest.raises(ValidationError) as exc_info:
        Settings(EXCHANGE_RATE=bad_rate)

    assert "EXCHANGE_RATE" in str(exc_info.value)


def test_exchange_rate_non_numeric_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        Settings(EXCHANGE_RATE="not-a-number")

    assert "EXCHANGE_RATE" in str(exc_info.value)


def test_exchange_rate_positive_custom_value_is_accepted():
    settings = Settings(EXCHANGE_RATE="2500.5")

    assert settings.EXCHANGE_RATE == Decimal("2500.5")


def test_duplicate_guard_mode_parses_from_string():
    settings = Settings(MCP_UPSTREAM_DUPLICATE_GUARD="enforce")

    assert settings.MCP_UPSTREAM_DUPLICATE_GUARD == DuplicateGuardMode.ENFORCE
    assert isinstance(settings.MCP_UPSTREAM_DUPLICATE_GUARD, str)


def test_duplicate_guard_mode_invalid_value_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        Settings(MCP_UPSTREAM_DUPLICATE_GUARD="sometimes")

    assert "MCP_UPSTREAM_DUPLICATE_GUARD" in str(exc_info.value)


def test_secret_defaults_start_empty():
    settings = Settings()

    assert settings.MCP_UPSTREAM_BEARER_TOKEN.get_secret_value() == ""
    assert settings.TYPESAFE_API_KEY.get_secret_value() == ""


def test_get_settings_returns_cached_instance():
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", ""),
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
        ("  https://example.com//  ", "https://example.com"),
        ("   ", ""),
    ],
)
def test_public_api_origin_normalizes_public_url(monkeypatch, configured, expected):
    monkeypatch.setenv("PUBLIC_URL", configured)
    get_settings.cache_clear()
    try:
        assert public_api_origin() == expected
    finally:
        get_settings.cache_clear()
