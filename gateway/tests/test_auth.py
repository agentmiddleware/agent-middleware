"""Focused coverage for app.core.auth: credential validation and AuthContext.

No focused test file for this module existed; neighboring suites (JWT auth,
static dev keys, API keys) each cover one path. This file covers the main
public behavior in one fast place: env-key auth, missing/short/unknown
credentials, bearer [REDACTED] precedence rules, AuthContext guards, and the
credential-acceptance signal. All JWT failures use unverifiable fake tokens,
so no network or real signing keys are needed.
"""

import pytest
from fastapi import HTTPException

from app.core.auth import (
    CREDENTIAL_ACCEPTANCE,
    CREDENTIAL_REJECTED_HEADER,
    AuthContext,
    CredentialAcceptance,
    _parse_bearer_authorization,
    _parse_static_dev_keys,
    get_auth_context,
    get_enterprise_principal,
    reject_anonymous_production_catalog,
    require_enterprise_tool_access,
    verify_api_key,
)
from app.core.config import get_settings

ENV_KEY = "test-key"


@pytest.fixture()
def auth_settings():
    """Snapshot and restore the cached settings fields these tests mutate."""
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in (
            "ENVIRONMENT",
            "DEBUG",
            "VALID_API_KEYS",
            "STATIC_DEV_API_KEYS",
            "IGA_TRUSTED_ISSUERS",
        )
    }
    settings.ENVIRONMENT = "local"
    settings.DEBUG = False
    settings.VALID_API_KEYS = ENV_KEY
    settings.STATIC_DEV_API_KEYS = ""
    settings.IGA_TRUSTED_ISSUERS = ""
    CREDENTIAL_ACCEPTANCE.set(None)
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
        CREDENTIAL_ACCEPTANCE.set(None)


def test_scopes_default_to_empty_list():
    context = AuthContext(source="env", raw_key=ENV_KEY, is_bootstrap_admin=True)
    assert context.scopes == []


async def test_env_key_authenticates_as_bootstrap_admin(auth_settings):
    context = await get_auth_context(api_key=ENV_KEY)
    assert context.source == "env"
    assert context.raw_key == ENV_KEY
    assert context.is_bootstrap_admin is True
    context.require_bootstrap_admin()  # must not raise


async def test_missing_credentials_raises_401(auth_settings):
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=None)
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "missing_credentials"


@pytest.mark.parametrize("bad_key", ["", "   ", "short"])
async def test_short_or_blank_key_raises_401(auth_settings, bad_key):
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=bad_key)
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "invalid_api_key"


async def test_unknown_key_fails_closed_with_rejected_marker(auth_settings):
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key="unknown-key-value")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "invalid_api_key"
    assert exc.value.headers[CREDENTIAL_REJECTED_HEADER] == "1"


async def test_debug_open_mode_without_keys_allows_unknown_key(auth_settings):
    auth_settings.DEBUG = True
    auth_settings.VALID_API_KEYS = ""
    context = await get_auth_context(api_key="some-unconfigured-key")
    assert context.is_bootstrap_admin is True
    assert context.source == "env"


async def test_malformed_authorization_never_falls_back_to_api_key(auth_settings):
    """A presented Authorization header is authoritative: even a valid API
    key alongside it must not authenticate when the Bearer [REDACTED] malformed."""
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=ENV_KEY, authorization="not-bearer-shaped")
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "invalid_token"


async def test_bearer_token_with_unknown_signature_raises_401(auth_settings):
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=None, authorization="Bearer fake.token.value")
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "invalid_token"


async def test_jwt_shaped_raw_key_verifies_as_token_not_key(auth_settings):
    """A raw JWT-shaped X-API-Key value goes to the JWT verifier, which
    rejects the fake with 401 instead of treating it as an unknown key."""
    fake_jwt = "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    with pytest.raises(HTTPException) as exc:
        await get_auth_context(api_key=fake_jwt)
    assert exc.value.status_code == 401
    assert exc.value.detail["error"] == "invalid_token"


async def test_successful_auth_marks_credential_acceptance(auth_settings):
    acceptance = CredentialAcceptance()
    token = CREDENTIAL_ACCEPTANCE.set(acceptance)
    try:
        await get_auth_context(api_key=ENV_KEY)
    finally:
        CREDENTIAL_ACCEPTANCE.reset(token)
    assert acceptance.accepted is True


async def test_verify_api_key_returns_raw_key(auth_settings):
    assert await verify_api_key(ENV_KEY) == ENV_KEY


def test_parse_bearer_accepts_exact_shape_only():
    assert _parse_bearer_authorization("Bearer abc123") == "abc123"
    for bad in (
        "Basic abc123",
        "Bearer ",
        "Bearer  abc",
        "Bearer abc def",
        "Bearer abc ",
        "bearer abc123",
    ):
        with pytest.raises(HTTPException) as exc:
            _parse_bearer_authorization(bad)
        assert exc.value.status_code == 401


def test_parse_static_dev_keys_keeps_prefixed_entries_only():
    assert _parse_static_dev_keys(" amw_dev_one ,amw_dev_two ") == [
        "amw_dev_one",
        "amw_dev_two",
    ]
    assert _parse_static_dev_keys("amw_live_pasted-in-wrong-place") == []
    assert _parse_static_dev_keys("") == []


def test_require_wallet_access_allows_owner_and_admin():
    admin = AuthContext(source="env", raw_key=ENV_KEY, is_bootstrap_admin=True)
    admin.require_wallet_access("other-wallet")
    admin.require_wallet_access(None)

    owner = AuthContext(source="db", raw_key="k", wallet_id="w-1")
    owner.require_wallet_access("w-1")

    stranger = AuthContext(source="db", raw_key="k", wallet_id="w-2")
    with pytest.raises(HTTPException) as exc:
        stranger.require_wallet_access("w-1")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        stranger.require_wallet_access(None)
    assert exc.value.status_code == 403


def test_require_bootstrap_admin_denies_non_admin():
    context = AuthContext(source="db", raw_key="k", wallet_id="w-1")
    with pytest.raises(HTTPException) as exc:
        context.require_bootstrap_admin()
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "admin_access_denied"


async def test_enterprise_principal_absent_when_iga_disabled(auth_settings):
    assert await get_enterprise_principal(authorization=None) is None
    assert await get_enterprise_principal(authorization="X-Key abc") is None
    assert await get_enterprise_principal(authorization="Bearer fake") is None


async def test_enterprise_tool_access_is_optional_without_principal():
    guard = require_enterprise_tool_access("some.tool")
    assert await guard(principal=None) is None


async def test_reject_anonymous_catalog_allows_local_without_credentials(
    auth_settings,
):
    from starlette.requests import Request

    request = Request({"type": "http", "headers": []})
    assert await reject_anonymous_production_catalog(request) is None
