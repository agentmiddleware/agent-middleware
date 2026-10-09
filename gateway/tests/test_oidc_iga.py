"""Focused tests for the enterprise IGA bridge (app/core/oidc_iga.py).

DB-free complement to tests/test_iga_policy.py: tokens are minted in-test
with HS256 and a pinned shared secret (no RSA key generation, no database),
and PolicyBundle lookups are stubbed. Covers token verification, provider
claim mapping, routing, config fail-closed behavior, and the runtime caps.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

import app.services.policies as policies
from app.core import oidc_iga
from app.core.config import get_settings
from app.core.oidc_iga import (
    IGAError,
    enforce_tool_call,
    is_iga_issuer_token,
    parse_enterprise_token,
    release_tool_use,
    reset_iga_counters,
    resolve_policy_grants,
    token_issuer_is_trusted,
)

ISS = "https://idp.example.invalid/oidc"
AUD = "api://test-middleware"
SECRET = "test-shared-secret-of-at-least-32-bytes"
OTHER_SECRET = "a-different-shared-secret-32-bytes-ok"
TOOL = "test.tool"


def _issuers(**overrides) -> dict:
    entry = {
        "audience": AUD,
        "algorithms": ["HS256"],
        "public_key_pem": SECRET,
        # Unknown host suffix, so the provider must be explicit.
        "provider": "okta",
    }
    entry.update(overrides)
    return {ISS: entry}


def _mint(
    *,
    secret: str = SECRET,
    iss: str = ISS,
    aud: str = AUD,
    exp_delta: int = 300,
    extra: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": "user-1",
        "iat": now - timedelta(seconds=60),
        "exp": now + timedelta(seconds=exp_delta),
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def iga_env(monkeypatch):
    """Set IGA env config, clearing the cached settings before and after."""

    def _apply(trusted: dict | str = "", group_map: dict | str = "") -> None:
        monkeypatch.setenv(
            "IGA_TRUSTED_ISSUERS",
            trusted if isinstance(trusted, str) else json.dumps(trusted),
        )
        monkeypatch.setenv(
            "IGA_GROUP_POLICY_MAP",
            group_map if isinstance(group_map, str) else json.dumps(group_map),
        )
        get_settings.cache_clear()

    _apply(_issuers())
    yield _apply
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_counters():
    reset_iga_counters()
    yield
    reset_iga_counters()


def _allow_bundle(monkeypatch, tools: list[str] | None = None):
    async def _bundle(_policy_id: str):
        return SimpleNamespace(is_active=True, allowed_tools=tools)

    monkeypatch.setattr(policies, "get_policy_bundle", _bundle)


# --- token verification and claim mapping ------------------------------------


def test_parse_okta_token_maps_groups(iga_env):
    token = _mint(extra={"groups": ["ops", "eng"], "email": "u@example.com"})
    principal = parse_enterprise_token(token)
    assert principal.subject == "user-1"
    assert principal.provider == "okta"
    assert principal.issuer == ISS
    assert principal.groups == ("ops", "eng")
    assert principal.email == "u@example.com"


def test_parse_entra_prefers_roles_over_groups(iga_env):
    iga_env(_issuers(provider="entra"))
    token = _mint(extra={"roles": ["App.Admin"], "groups": ["fallback"]})
    principal = parse_enterprise_token(token)
    assert principal.provider == "entra"
    assert principal.groups == ("App.Admin",)


def test_parse_entra_falls_back_to_groups(iga_env):
    iga_env(_issuers(provider="entra"))
    principal = parse_enterprise_token(_mint(extra={"groups": ["g1"]}))
    assert principal.groups == ("g1",)


def test_missing_group_claim_is_empty_not_error(iga_env):
    assert parse_enterprise_token(_mint()).groups == ()


def test_non_string_group_entries_are_ignored(iga_env):
    token = _mint(extra={"groups": ["ops", 7, None, {"g": 1}]})
    assert parse_enterprise_token(token).groups == ("ops",)


def test_non_string_email_is_none(iga_env):
    principal = parse_enterprise_token(_mint(extra={"email": ["not", "a", "str"]}))
    assert principal.email is None


# --- routing ------------------------------------------------------------------


def test_routing_true_only_for_configured_issuer(iga_env):
    assert is_iga_issuer_token(_mint()) is True
    assert is_iga_issuer_token(_mint(iss="https://evil.example.invalid")) is False
    assert is_iga_issuer_token("not-a-jwt") is False


def test_routing_false_when_iga_disabled(iga_env):
    iga_env("", "")
    assert token_issuer_is_trusted(_mint()) is False
    assert is_iga_issuer_token(_mint()) is False


# --- verification error cases --------------------------------------------------


def test_expired_token_rejected(iga_env):
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(exp_delta=-60))
    assert excinfo.value.reason == "iga_token_expired"


def test_wrong_secret_rejected(iga_env):
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(secret=OTHER_SECRET))
    assert excinfo.value.reason == "iga_signature_invalid"


def test_unlisted_algorithm_rejected(iga_env):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": ISS,
            "aud": AUD,
            "sub": "user-1",
            "iat": now,
            "exp": now + timedelta(seconds=300),
        },
        SECRET,
        algorithm="HS384",
    )
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(token)
    assert excinfo.value.reason == "iga_algorithm_not_allowed"


def test_untrusted_issuer_rejected(iga_env):
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(iss="https://evil.example.invalid"))
    assert excinfo.value.reason == "iga_issuer_not_trusted"


def test_malformed_token_rejected(iga_env):
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token("not-a-jwt")
    assert excinfo.value.reason == "iga_token_malformed"


# --- configuration fails closed -------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {
            "audience": AUD,
            "algorithms": ["HS256", "none"],
            "public_key_pem": SECRET,
            "provider": "okta",
        },
        {
            "audience": AUD,
            "algorithms": ["HS256"],
            "public_key_pem": SECRET,
            "provider": "okta",
            "jwks": {"keys": []},
        },
        {"audience": AUD, "algorithms": ["HS256"], "provider": "okta"},
        {"algorithms": ["HS256"], "public_key_pem": SECRET, "provider": "okta"},
        {
            "audience": AUD,
            "algorithms": [],
            "public_key_pem": SECRET,
            "provider": "okta",
        },
    ],
    ids=[
        "none-alg",
        "both-key-sources",
        "no-key-source",
        "missing-audience",
        "empty-algorithms",
    ],
)
def test_structurally_invalid_issuer_config_fails_closed(iga_env, entry):
    iga_env({ISS: entry})
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint())
    assert excinfo.value.reason == "iga_config_invalid"


def test_non_json_issuer_config_fails_closed(iga_env):
    iga_env("{not json", "")
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint())
    assert excinfo.value.reason == "iga_config_invalid"


def test_unknown_host_without_provider_fails_closed(iga_env):
    iga_env(
        {
            "https://vanity.example.invalid": {
                "audience": AUD,
                "algorithms": ["HS256"],
                "public_key_pem": SECRET,
            }
        }
    )
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(iss="https://vanity.example.invalid"))
    assert excinfo.value.reason == "iga_config_invalid"


def test_half_velocity_cap_in_group_map_fails_closed(iga_env):
    iga_env(_issuers(), {"ops": {"policy_id": "p1", "velocity_window_seconds": 60}})
    principal = parse_enterprise_token(_mint(extra={"groups": ["ops"]}))
    with pytest.raises(IGAError) as excinfo:
        resolve_policy_grants(principal)
    assert excinfo.value.reason == "iga_config_invalid"


# --- grant resolution and runtime enforcement ------------------------------------


def test_provider_inference(iga_env):
    assert oidc_iga._infer_provider("https://tenant.okta.com/oauth2/default") == "okta"
    assert (
        oidc_iga._infer_provider("https://login.microsoftonline.com/tenant/v2.0")
        == "entra"
    )
    assert oidc_iga._infer_provider("https://vanity.example.invalid") is None


async def test_no_matching_role_blocks(iga_env):
    iga_env(_issuers(), {"ops": {"policy_id": "p1"}})
    principal = parse_enterprise_token(_mint(extra={"groups": ["other"]}))
    assert resolve_policy_grants(principal) == []
    decision = await enforce_tool_call(principal, TOOL)
    assert decision.allowed is False
    assert decision.reason == "iga_no_matching_role"


async def test_allow_records_use_and_max_uses_blocks(iga_env, monkeypatch):
    iga_env(_issuers(), {"ops": {"policy_id": "p1", "max_uses": 1}})
    _allow_bundle(monkeypatch, tools=None)
    principal = parse_enterprise_token(_mint(extra={"groups": ["ops"]}))
    first = await enforce_tool_call(principal, TOOL)
    assert first.allowed is True
    assert first.details == {"used": 1}
    second = await enforce_tool_call(principal, TOOL)
    assert second.allowed is False
    assert second.reason == "iga_max_uses_exceeded"


async def test_velocity_window_blocks_then_ages_out(iga_env, monkeypatch):
    iga_env(
        _issuers(),
        {
            "ops": {
                "policy_id": "p1",
                "velocity_window_seconds": 10,
                "velocity_max_calls": 2,
            }
        },
    )
    _allow_bundle(monkeypatch, tools=None)
    clock = [1000.0]
    monkeypatch.setattr(oidc_iga, "_monotonic", lambda: clock[0])
    principal = parse_enterprise_token(_mint(extra={"groups": ["ops"]}))
    assert (await enforce_tool_call(principal, TOOL)).allowed is True
    assert (await enforce_tool_call(principal, TOOL)).allowed is True
    clock[0] += 1.0
    denied = await enforce_tool_call(principal, TOOL)
    assert denied.allowed is False
    assert denied.reason == "iga_velocity_exceeded"
    clock[0] += 60.0
    assert (await enforce_tool_call(principal, TOOL)).allowed is True


async def test_tool_outside_allowlist_blocked(iga_env, monkeypatch):
    iga_env(_issuers(), {"ops": {"policy_id": "p1"}})
    _allow_bundle(monkeypatch, tools=[TOOL])
    principal = parse_enterprise_token(_mint(extra={"groups": ["ops"]}))
    decision = await enforce_tool_call(principal, "other.tool")
    assert decision.allowed is False
    assert decision.reason == "iga_tool_not_allowed"


async def test_missing_and_inactive_bundles_deny(iga_env, monkeypatch):
    iga_env(
        _issuers(),
        {
            "g-gone": {"policy_id": "p-gone"},
            "g-off": {"policy_id": "p-off"},
        },
    )

    async def _bundle(policy_id: str):
        if policy_id == "p-gone":
            return None
        return SimpleNamespace(is_active=False, allowed_tools=None)

    monkeypatch.setattr(policies, "get_policy_bundle", _bundle)
    gone = parse_enterprise_token(_mint(extra={"groups": ["g-gone"]}))
    assert (await enforce_tool_call(gone, TOOL)).reason == "iga_policy_not_found"
    off = parse_enterprise_token(_mint(extra={"groups": ["g-off"]}))
    assert (await enforce_tool_call(off, TOOL)).reason == "iga_policy_inactive"


async def test_consume_false_checks_without_spending(iga_env, monkeypatch):
    iga_env(_issuers(), {"ops": {"policy_id": "p1", "max_uses": 1}})
    _allow_bundle(monkeypatch, tools=None)
    principal = parse_enterprise_token(_mint(extra={"groups": ["ops"]}))
    peek = await enforce_tool_call(principal, TOOL, consume=False)
    assert peek.allowed is True
    real = await enforce_tool_call(principal, TOOL)
    assert real.allowed is True
    assert real.details == {"used": 1}


async def test_release_restores_a_never_dispatched_use(iga_env, monkeypatch):
    iga_env(_issuers(), {"ops": {"policy_id": "p1", "max_uses": 1}})
    _allow_bundle(monkeypatch, tools=None)
    principal = parse_enterprise_token(_mint(extra={"groups": ["ops"]}))
    first = await enforce_tool_call(principal, TOOL)
    assert first.allowed is True
    await release_tool_use(
        principal, TOOL, group="ops", policy_id="p1", reservation=first.reservation
    )
    # Releasing twice, or with no reservation, changes nothing further.
    await release_tool_use(
        principal, TOOL, group="ops", policy_id="p1", reservation=first.reservation
    )
    await release_tool_use(principal, TOOL, group="ops", policy_id="p1")
    retry = await enforce_tool_call(principal, TOOL)
    assert retry.allowed is True
