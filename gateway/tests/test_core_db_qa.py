"""Deep QA for gateway/app/core and gateway/app/db.

Covers untested behavior in the trust-adjacent core (money validation,
auth helpers, guardrails, URL safety) and the DB layer (converters,
bind normalization, schema gates), with regression tests for four small
fixes: blob backend warning honesty, per-call settings in dependencies,
max_attempts guards in resilience, and SQLITE_URL form handling.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.credits import credit_amount_fits_storage
from app.core.db_urls import (
    as_asyncpg_url,
    as_sqlalchemy_url,
    is_postgres_url,
    is_sqlite_url,
    sqlite_path_from_url,
)
from app.core.product_positioning import POSITIONING_SEMANTICS, get_product_positioning
from app.core.public_contact import validated_public_contact
from app.core.runtime_degradation import (
    get_runtime_degradation,
    mark_durable_state_fell_back,
    mark_rate_limiter_memory_fallback,
    reset_runtime_degradation,
)
from app.core.runtime_mode import (
    UnknownServiceError,
    get_simulation_modes,
    is_simulation,
    require_simulation,
)
from app.core.sentinel_target import (
    SentinelTargetError,
    normalize_sentinel_origin,
    sentinel_api_key_is_valid,
    sentinel_health_url,
)
from app.core.time import to_naive_utc, utc_now
from app.core.trust_mode import (
    TrustModeGuardrailError,
    is_production_like_environment,
    normalize_environment,
    require_explicit_environment_on_hosted_runtime,
    validate_trust_mode_config,
)

VALID_PROD_SIGNING_KEY = base64.b64encode(bytes(range(32))).decode()
VALID_PROD_DATABASE_URL = "postgresql+asyncpg://user:pw@db.internal:5432/trust"


def _prod_config(**overrides):
    base = {
        "environment": "production",
        "trust_mode_enabled": True,
        "signing_private_key_b64": VALID_PROD_SIGNING_KEY,
        "allow_legacy_unpermitted_mcp": False,
        "enable_proof_surfaces": False,
        "database_url": VALID_PROD_DATABASE_URL,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# credits: money storage contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("0"), True),
        (Decimal("1"), True),
        (Decimal("1.12345678"), True),
        (Decimal("0.00000001"), True),
        (Decimal("999999999999.99999999"), False),  # float roundtrip loses it
        (Decimal("1000000000000"), False),  # exclusive upper bound
        (Decimal("-1"), False),
        (Decimal("-0.00000001"), False),
        (Decimal("1.123456789"), False),  # ninth decimal never survives
        (Decimal("0.000000001"), False),  # rounds to zero on readback
        (Decimal("NaN"), False),
        (Decimal("Infinity"), False),
        (Decimal("-Infinity"), False),
    ],
)
def test_credit_amount_fits_storage_boundaries(amount, expected):
    assert credit_amount_fits_storage(amount) is expected


def test_credit_amount_boundary_just_inside_limit():
    assert credit_amount_fits_storage(Decimal("999999999999")) is True


# ---------------------------------------------------------------------------
# db_urls
# ---------------------------------------------------------------------------


def test_sqlite_path_rejects_in_memory_spellings():
    assert sqlite_path_from_url("sqlite:///:memory:") == ""
    assert sqlite_path_from_url("sqlite+aiosqlite:///:memory:") == ""
    assert sqlite_path_from_url("sqlite:///file:" + ":memory:") == ""
    assert sqlite_path_from_url("sqlite:///file:cache=shared?mode=memory") == ""


def test_sqlite_path_keeps_durable_lookalikes():
    # A substring test on "mode=memory" would misroute this durable file.
    assert sqlite_path_from_url("sqlite:///./mode=memory.db") == "./mode=memory.db"
    assert sqlite_path_from_url("  SQLITE:///./X.DB  ") == "./X.DB"
    assert sqlite_path_from_url("sqlite+aiosqlite:///./s.db") == "./s.db"
    assert sqlite_path_from_url("sqlite:////abs/s.db") == "/abs/s.db"


def test_sqlite_url_detection_covers_memory():
    assert is_sqlite_url("sqlite+aiosqlite:///:memory:") is True
    assert is_sqlite_url("SQLITE:///./x.db") is True
    assert is_sqlite_url("postgresql://h/db") is False
    assert is_sqlite_url("") is False


def test_converters_leave_credentials_untouched():
    upper = "POSTGRESQL://Us3R:PaSs@Host/DB"
    assert as_asyncpg_url(upper) == "postgresql://Us3R:PaSs@Host/DB"
    assert as_sqlalchemy_url(upper) == "postgresql+asyncpg://Us3R:PaSs@Host/DB"
    assert is_postgres_url("  postgres://u:p@h/db  ") is True
    assert as_sqlalchemy_url("") == ""
    assert as_asyncpg_url("") == ""


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def test_to_naive_utc_preserves_instant():
    aware = datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    assert to_naive_utc(aware) == datetime(2026, 1, 1, 21, 34, 5)
    assert to_naive_utc(aware).tzinfo is None


def test_to_naive_utc_leaves_naive_untouched():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert to_naive_utc(naive) == naive
    assert utc_now().tzinfo is None


# ---------------------------------------------------------------------------
# trust_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env", ["production", "PROD", "prod-us", "staging-1", "qa", "  Stage  ", "PRE_PROD"]
)
def test_production_like_classifier_true(env):
    assert is_production_like_environment(env) is True


@pytest.mark.parametrize(
    "env", ["", "local", "dev", "development", "test", "testing", "ci", "localhost"]
)
def test_production_like_classifier_false(env):
    assert is_production_like_environment(env) is False


def test_normalize_environment_underscores_and_case():
    assert normalize_environment("PRE_PROD") == "pre-prod"
    assert normalize_environment(None) == ""


def test_prod_guardrails_accept_minimal_strict_config():
    validate_trust_mode_config(**_prod_config())


def test_prod_guardrails_reject_sqlite_database_url():
    with pytest.raises(TrustModeGuardrailError, match="must not be SQLite"):
        validate_trust_mode_config(
            **_prod_config(database_url="sqlite+aiosqlite:///./prod.db")
        )
    with pytest.raises(TrustModeGuardrailError, match="must be set"):
        validate_trust_mode_config(**_prod_config(database_url=""))


def test_prod_guardrails_reject_unsafe_sandbox_backend():
    with pytest.raises(TrustModeGuardrailError, match="BEHAVIORAL_SANDBOX"):
        validate_trust_mode_config(
            **_prod_config(behavioral_sandbox_python_backend="unsafe_host")
        )
    with pytest.raises(TrustModeGuardrailError, match="ALLOW_UNSAFE_HOST"):
        validate_trust_mode_config(
            **_prod_config(allow_unsafe_host_python_sandbox=True)
        )


def test_hosted_runtime_requires_explicit_environment():
    with pytest.raises(TrustModeGuardrailError, match="ENVIRONMENT must be set"):
        require_explicit_environment_on_hosted_runtime(
            None, runtime_env={"RAILWAY_ENVIRONMENT_ID": "abc"}
        )
    require_explicit_environment_on_hosted_runtime(
        "dev", runtime_env={"RAILWAY_ENVIRONMENT_ID": "abc"}
    )
    require_explicit_environment_on_hosted_runtime(None, runtime_env={})


# ---------------------------------------------------------------------------
# sentinel_target
# ---------------------------------------------------------------------------


def test_sentinel_origin_accepts_public_https_and_strips_default_port():
    assert (
        normalize_sentinel_origin("https://approvals.example.com:443")
        == "https://approvals.example.com"
    )
    assert (
        sentinel_health_url("https://approvals.example.com")
        == "https://approvals.example.com/health"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://approvals.example.com",  # remote http is never allowed
        "https://user:pass@approvals.example.com",  # embedded credentials
        "https://approvals.example.com/some/path",  # must be a root origin
        "https://approvals.example.com?x=1",
        "https://127.0.0.1",  # loopback without the local opt-in
        "http://localhost:3000",
        "https://169.254.169.254",  # cloud metadata
        "https://10.0.0.5",  # RFC1918
        "https://metadata.google.internal",
        "https://approvals.internal",
        "ftp://approvals.example.com",
        "https://2130706433",  # legacy ambiguous IPv4 form
        "  https://approvals.example.com  ",
        "https://approvals.example.com\n",
    ],
)
def test_sentinel_origin_rejects_unsafe_targets(url):
    with pytest.raises(SentinelTargetError):
        normalize_sentinel_origin(url)


def test_sentinel_origin_allows_loopback_only_when_opted_in():
    assert (
        normalize_sentinel_origin("http://127.0.0.1:3000", allow_loopback=True)
        == "http://127.0.0.1:3000"
    )
    with pytest.raises(SentinelTargetError):
        normalize_sentinel_origin("http://127.0.0.1:3000", allow_loopback=False)


def test_sentinel_api_key_validation():
    assert sentinel_api_key_is_valid("live-key-123") is True
    assert sentinel_api_key_is_valid("") is False
    assert sentinel_api_key_is_valid("  padded  ") is False
    assert sentinel_api_key_is_valid("has space") is False
    assert sentinel_api_key_is_valid("uni\x00code") is False


# ---------------------------------------------------------------------------
# public_contact
# ---------------------------------------------------------------------------


class _Contact:
    def __init__(self, name="", email="", url=""):
        self.PUBLIC_CONTACT_NAME = name
        self.PUBLIC_CONTACT_EMAIL = email
        self.PUBLIC_CONTACT_URL = url


def test_public_contact_none_when_unset():
    assert validated_public_contact(_Contact()) is None


def test_public_contact_partial_config_fails():
    with pytest.raises(ValueError, match="together"):
        validated_public_contact(_Contact(name="Acme Ops"))


def test_public_contact_valid_triple():
    out = validated_public_contact(
        _Contact(
            name="Acme Operations",
            email="ops@acme-corp.com",
            url="https://acme-corp.com/contact",
        )
    )
    assert out == {
        "name": "Acme Operations",
        "email": "ops@acme-corp.com",
        "url": "https://acme-corp.com/contact",
    }


@pytest.mark.parametrize(
    "contact",
    [
        _Contact(name="x", email="ops@acme-corp.com", url="https://acme-corp.com/c"),
        _Contact(
            name="Test API Team",
            email="ops@acme-corp.com",
            url="https://acme-corp.com/c",
        ),
        _Contact(
            name="Acme Operations",
            email="ops@example.com",
            url="https://acme-corp.com/c",
        ),
        _Contact(
            name="Acme Operations",
            email="ops@b.test",
            url="https://acme-corp.com/c",
        ),
        _Contact(
            name="Acme Operations",
            email="not-an-email",
            url="https://acme-corp.com/c",
        ),
        _Contact(
            name="Acme Operations",
            email="ops@acme-corp.com",
            url="http://acme-corp.com/c",
        ),
        _Contact(
            name="Acme Operations",
            email="ops@acme-corp.com",
            url="https://api.thisisatest.tech/book",
        ),
        _Contact(
            name="Agent Middleware API",
            email="ops@acme-corp.com",
            url="https://acme-corp.com/c",
        ),
    ],
)
def test_public_contact_rejects_placeholders(contact):
    with pytest.raises(ValueError):
        validated_public_contact(contact)


# ---------------------------------------------------------------------------
# product_positioning
# ---------------------------------------------------------------------------


def test_positioning_returns_fresh_copy():
    first = get_product_positioning()
    assert first["semantics"] == list(POSITIONING_SEMANTICS)
    first["semantics"].append("mutant")
    first["canonical_loop"].clear()
    second = get_product_positioning()
    assert "mutant" not in second["semantics"]
    assert len(second["canonical_loop"]) > 0


# ---------------------------------------------------------------------------
# runtime_mode and runtime_degradation
# ---------------------------------------------------------------------------


def test_runtime_mode_unknown_service_raises():
    with pytest.raises(UnknownServiceError):
        is_simulation("nope")
    with pytest.raises(UnknownServiceError):
        require_simulation("nope")


def test_runtime_mode_snapshot_covers_every_service(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "SIMULATION_MODE_ORACLE", False)
    assert is_simulation("oracle") is False
    with pytest.raises(NotImplementedError):
        require_simulation("oracle")
    modes = get_simulation_modes()
    assert set(modes) >= {"oracle", "human_approval"}
    assert modes["oracle"] is False


def test_runtime_degradation_flags_and_reset():
    reset_runtime_degradation()
    try:
        assert get_runtime_degradation()["degraded"] is False
        mark_rate_limiter_memory_fallback()
        degraded = get_runtime_degradation()
        assert degraded["degraded"] is True
        assert degraded["rate_limiter"]["using_memory_fallback"] is True
        mark_durable_state_fell_back("redis")
        assert get_runtime_degradation()["durable_state"]["intended_backend"] == "redis"
    finally:
        reset_runtime_degradation()
    assert get_runtime_degradation()["degraded"] is False


# ---------------------------------------------------------------------------
# blob backend selection honesty (fix: no silent "fallback to local")
# ---------------------------------------------------------------------------


def test_unimplemented_backends_fail_loudly_not_local(monkeypatch, caplog):
    from app.core.blob import _UnimplementedBlob, get_blob_backend

    get_blob_backend.cache_clear()
    try:
        for name in ("s3", "vercel"):
            monkeypatch.setenv("BLOB_BACKEND", name)
            get_blob_backend.cache_clear()
            with caplog.at_level(logging.WARNING):
                backend = get_blob_backend()
            assert isinstance(backend, _UnimplementedBlob)
            with pytest.raises(NotImplementedError):
                import anyio

                anyio.run(backend.put, "k", b"v")
        assert "falling back to local" not in caplog.text
        assert "fail loudly" in caplog.text
    finally:
        get_blob_backend.cache_clear()


# ---------------------------------------------------------------------------
# auth: bearer strictness, static-dev prefix, wallet isolation
# ---------------------------------------------------------------------------

from fastapi import HTTPException  # noqa: E402

from app.core.auth import (  # noqa: E402
    AuthContext,
    _parse_bearer_authorization,
    _parse_static_dev_keys,
    _resolve_auth_context,
)


def test_parse_bearer_accepts_exact_shape_only():
    scheme = "Bearer" + " "
    good = scheme + "abc" + "." + "def" + "." + "ghi"
    assert _parse_bearer_authorization(good) == "abc.def.ghi"
    for bad in [
        "Token abc",
        "bearer" + " " + "abc",
        "Bearer" + " ",
        "Bearer",
        "Bearer" + "  " + "abc",
        "Bearer" + " " + "abc" + " ",
        "Bearer" + " " + "a" + " " + "b",
        "Bearer\tabc",
    ]:
        with pytest.raises(HTTPException) as exc_info:
            _parse_bearer_authorization(bad)
        assert exc_info.value.status_code == 401


def test_parse_static_dev_keys_honors_prefix_only():
    assert _parse_static_dev_keys("amw_dev_a, amw_live_b, , amw_dev_c ") == [
        "amw_dev_a",
        "amw_dev_c",
    ]
    assert _parse_static_dev_keys("") == []
    assert _parse_static_dev_keys("amw_live_only") == []


def test_require_wallet_access_matrix():
    admin = AuthContext(source="env", raw_key="k", is_bootstrap_admin=True)
    admin.require_wallet_access("other-wallet")
    admin.require_wallet_access(None)

    owner = AuthContext(source="db", raw_key="k", wallet_id="w1")
    owner.require_wallet_access("w1")

    with pytest.raises(HTTPException) as exc_info:
        owner.require_wallet_access("w2")
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "wallet_access_denied"

    # Ownerless resources stay admin-only.
    with pytest.raises(HTTPException):
        owner.require_wallet_access(None)

    keyless = AuthContext(source="db", raw_key="k", wallet_id=None)
    with pytest.raises(HTTPException):
        keyless.require_wallet_access(None)


def test_require_bootstrap_admin():
    AuthContext(
        source="env", raw_key="k", is_bootstrap_admin=True
    ).require_bootstrap_admin()
    with pytest.raises(HTTPException) as exc_info:
        AuthContext(source="db", raw_key="k", wallet_id="w1").require_bootstrap_admin()
    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_resolve_missing_and_short_keys_401(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENABLE_DEV_KEY_SELF_PROVISION", False)
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_auth_context(None, None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "missing_credentials"
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_auth_context("  short ", None)
    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_resolve_unknown_key_403_with_reject_marker(monkeypatch):
    from app.core.auth import CREDENTIAL_REJECTED_HEADER

    settings = get_settings()
    monkeypatch.setattr(settings, "VALID_API_KEYS", "test-key")
    monkeypatch.setattr(settings, "STATIC_DEV_API_KEYS", "")
    monkeypatch.setattr(settings, "DEBUG", False)
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_auth_context("unknown-key-123", None)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "invalid_api_key"
    assert exc_info.value.headers[CREDENTIAL_REJECTED_HEADER] == "1"


@pytest.mark.anyio
async def test_resolve_static_dev_key_needs_prefix_and_local_env(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "VALID_API_KEYS", "")
    monkeypatch.setattr(settings, "STATIC_DEV_API_KEYS", "amw_dev_good,amw_live_leaked")
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "DEBUG", False)
    ctx = await _resolve_auth_context("amw_dev_good", None)
    assert ctx.source == "static-dev"
    assert ctx.is_bootstrap_admin is True
    # A live key pasted into the dev variable never authenticates that path.
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_auth_context("amw_live_leaked", None)
    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_resolve_static_dev_keys_dead_in_production(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "VALID_API_KEYS", "")
    monkeypatch.setattr(settings, "STATIC_DEV_API_KEYS", "amw_dev_good")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "DEBUG", False)
    with pytest.raises(HTTPException):
        await _resolve_auth_context("amw_dev_good", None)


@pytest.mark.anyio
async def test_enterprise_bearer_rides_alongside_api_key(monkeypatch):
    import jwt as pyjwt

    settings = get_settings()
    monkeypatch.setattr(settings, "VALID_API_KEYS", "test-key")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(
        settings,
        "IGA_TRUSTED_ISSUERS",
        '{"https://example.okta.com": {"audience": "api://x", '
        '"algorithms": ["RS256"], "provider": "okta", "jwks": {"keys": []}}}',
    )
    monkeypatch.setattr(settings, "STATIC_DEV_API_KEYS", "")
    token = pyjwt.encode(
        {"iss": "https://example.okta.com", "sub": "human"},
        "routing-peek-only-dummy-secret-32bytes",
        algorithm="HS256",
    )
    ctx = await _resolve_auth_context("test-key", f"Bearer {token}")
    assert ctx.source == "env"
    assert ctx.is_bootstrap_admin is True
    assert ctx.enterprise_bearer_token == token


@pytest.mark.anyio
async def test_jwt_without_live_key_fails_closed(monkeypatch):
    from app.core.jwt import get_jwt_service

    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU=",
    )
    monkeypatch.setattr(settings, "VALID_API_KEYS", "")
    monkeypatch.setattr(settings, "STATIC_DEV_API_KEYS", "")
    monkeypatch.setattr(settings, "DEBUG", False)
    token = get_jwt_service().create_access_token("w1", "key-1", ["mcp:invoke"])
    with pytest.raises(HTTPException) as exc_info:
        await _resolve_auth_context(token, None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "no_active_api_key"


# ---------------------------------------------------------------------------
# scopes
# ---------------------------------------------------------------------------

from app.core.scopes import require_scope  # noqa: E402


def _ctx(source, scopes=None):
    return AuthContext(source=source, raw_key="k", wallet_id="w1", scopes=scopes)


@pytest.mark.anyio
async def test_require_scope_bypass_and_jwt_paths():
    @require_scope("billing:charge")
    async def handler(auth: AuthContext):
        return "ok"

    for source in ("db", "env", "static-dev"):
        assert await handler(auth=_ctx(source, scopes=[])) == "ok"
    assert await handler(auth=_ctx("jwt", scopes=["billing:charge"])) == "ok"
    with pytest.raises(HTTPException) as exc_info:
        await handler(auth=_ctx("jwt", scopes=["other"]))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "insufficient_scope"
    assert exc_info.value.detail["provided"] == ["other"]
    with pytest.raises(HTTPException) as exc_info:
        await handler()
    assert exc_info.value.status_code == 401


def test_auth_context_omitted_scopes_normalize_to_empty_list():
    omitted = AuthContext(source="env", raw_key="k")
    explicit_none = AuthContext(source="jwt", raw_key="k", scopes=None)
    assert omitted.scopes == []
    assert explicit_none.scopes == []


@pytest.mark.anyio
async def test_require_scope_treats_unset_scopes_as_empty():
    """A context that skipped constructor None-coercion must not raise TypeError."""

    @require_scope("billing:charge")
    async def handler(auth: AuthContext):
        return "ok"

    auth = AuthContext(
        source="jwt", raw_key="k", wallet_id="w1", scopes=["billing:charge"]
    )
    object.__setattr__(auth, "scopes", None)
    with pytest.raises(HTTPException) as exc_info:
        await handler(auth=auth)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "insufficient_scope"
    assert exc_info.value.detail["provided"] == []


# ---------------------------------------------------------------------------
# jwt service
# ---------------------------------------------------------------------------

JWT_DUMMY_KEY = "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="


@pytest.fixture
def jwt_keyed(monkeypatch):
    monkeypatch.setattr(get_settings(), "TRUST_SIGNING_PRIVATE_KEY_B64", JWT_DUMMY_KEY)
    from app.core.jwt import get_jwt_service

    return get_jwt_service()


def _mint_raw(payload):
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(JWT_DUMMY_KEY))
    return pyjwt.encode(payload, private, algorithm="EdDSA")


def _access_payload(**overrides):
    now = datetime.now(timezone.utc)
    base = {
        "sub": "w1",
        "key_id": "key-1",
        "scopes": ["mcp:invoke"],
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "iss": "agent-middleware-api",
        "aud": "agent-middleware-api",
        "jti": "jti-1",
        "type": "access",
    }
    base.update(overrides)
    return base


def test_jwt_access_roundtrip(jwt_keyed):
    token = jwt_keyed.create_access_token("w1", "key-1", ["mcp:invoke"])
    payload = jwt_keyed.verify_access_token(token)
    assert (payload.sub, payload.key_id, payload.scopes) == (
        "w1",
        "key-1",
        ["mcp:invoke"],
    )


def test_jwt_rejects_wrong_type_and_bad_scopes(jwt_keyed):
    refresh = jwt_keyed.create_refresh_token("w1")
    with pytest.raises(Exception, match="token_type_mismatch"):
        jwt_keyed.verify_access_token(refresh)
    with pytest.raises(Exception, match="invalid_or_missing_scopes"):
        jwt_keyed.verify_access_token(_mint_raw(_access_payload(scopes="admin")))
    with pytest.raises(Exception, match="token_expired"):
        jwt_keyed.verify_access_token(
            _mint_raw(
                _access_payload(exp=datetime.now(timezone.utc) - timedelta(seconds=5))
            )
        )


def test_jwt_requires_signing_key(monkeypatch):
    from app.core.jwt import JWTError, get_jwt_service

    monkeypatch.setattr(get_settings(), "TRUST_SIGNING_PRIVATE_KEY_B64", "")
    with pytest.raises(JWTError, match="signing_key_not_configured"):
        get_jwt_service().create_access_token("w1", "key-1", [])


# ---------------------------------------------------------------------------
# oidc_iga fail-closed config and routing
# ---------------------------------------------------------------------------

from app.core.oidc_iga import (  # noqa: E402
    EnterprisePrincipal,
    IGAError,
    _infer_provider,
    _trusted_issuers,
    enforce_tool_call,
    is_iga_issuer_token,
    parse_enterprise_token,
    release_tool_use,
    reset_iga_counters,
    resolve_policy_grants,
    token_issuer_is_trusted,
)


def _iga_config(monkeypatch, issuers=True, mapping="{}"):
    settings = get_settings()
    if issuers:
        monkeypatch.setattr(
            settings,
            "IGA_TRUSTED_ISSUERS",
            '{"https://example.okta.com": {"audience": "api://x", '
            '"algorithms": ["RS256"], "provider": "okta", '
            '"jwks": {"keys": []}}}',
        )
    else:
        monkeypatch.setattr(settings, "IGA_TRUSTED_ISSUERS", "")
    monkeypatch.setattr(settings, "IGA_GROUP_POLICY_MAP", mapping)
    reset_iga_counters()


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[1, 2]",
        '{"https://x": {"audience": "a", "algorithms": ["none"], '
        '"provider": "okta", "jwks": {"keys": []}}}',
        '{"https://x": {"audience": "a", "algorithms": ["RS256"], "provider": "okta"}}',
        '{"https://x": {"audience": "a", "algorithms": ["RS256"], '
        '"provider": "okta", "jwks": {"keys": []}, "public_key_pem": "k"}}',
        '{"https://x": {"audience": "", "algorithms": ["RS256"], '
        '"provider": "okta", "jwks": {"keys": []}}}',
        '{"https://x": {"audience": "a", "algorithms": ["RS256"], '
        '"provider": "saml", "jwks": {"keys": []}}}',
    ],
)
def test_iga_issuer_config_fails_closed(monkeypatch, raw):
    monkeypatch.setattr(get_settings(), "IGA_TRUSTED_ISSUERS", raw)
    with pytest.raises(IGAError) as exc_info:
        _trusted_issuers()
    # The stable snake_case reason is the contract; str(exc) is the human
    # message and never carries it.
    assert exc_info.value.reason == "iga_config_invalid"


@pytest.mark.parametrize(
    "mapping",
    [
        '{"g": {"policy_id": "", "max_uses": 1}}',
        '{"g": {"policy_id": "p", "max_uses": true}}',
        '{"g": {"policy_id": "p", "velocity_window_seconds": 60}}',
        '{"g": {"policy_id": "p", "velocity_max_calls": 5}}',
        "not-json",
    ],
)
def test_iga_group_map_fails_closed(monkeypatch, mapping):
    _iga_config(monkeypatch, mapping=mapping)
    principal = EnterprisePrincipal(
        subject="u", provider="okta", issuer="https://example.okta.com", groups=("g",)
    )
    with pytest.raises(IGAError) as exc_info:
        resolve_policy_grants(principal)
    assert exc_info.value.reason == "iga_config_invalid"


def test_iga_provider_inference():
    assert _infer_provider("https://acme.okta.com/oauth2/default") == "okta"
    assert _infer_provider("https://login.microsoftonline.com/tenant/v2.0") == "entra"
    assert _infer_provider("https://idp.acme-corp.com/realms/x") is None


def test_iga_routing_peek_never_authenticates(monkeypatch):
    _iga_config(monkeypatch, issuers=False)
    assert token_issuer_is_trusted("garbage") is False
    assert is_iga_issuer_token("garbage") is False
    _iga_config(monkeypatch)
    assert token_issuer_is_trusted("garbage") is False
    assert is_iga_issuer_token("garbage") is False
    with pytest.raises(IGAError):
        parse_enterprise_token("garbage")
    import jwt as pyjwt

    foreign = pyjwt.encode(
        {"iss": "https://unknown.example.com", "sub": "u"},
        "routing-peek-only-dummy-secret-32bytes",
        algorithm="HS256",
    )
    assert token_issuer_is_trusted(foreign) is False
    assert is_iga_issuer_token(foreign) is False
    with pytest.raises(IGAError) as exc_info:
        parse_enterprise_token(foreign)
    assert exc_info.value.reason == "iga_issuer_not_trusted"


@pytest.mark.anyio
async def test_iga_blocks_principal_without_matching_role(monkeypatch):
    _iga_config(monkeypatch)
    principal = EnterprisePrincipal(
        subject="u",
        provider="okta",
        issuer="https://example.okta.com",
        groups=("other",),
    )
    decision = await enforce_tool_call(principal, "partner.echo")
    assert decision.allowed is False
    assert decision.reason == "iga_no_matching_role"
    await release_tool_use(
        principal, "partner.echo", group="g", policy_id="p", reservation=None
    )


# --- rate_limiter: bucket hygiene, marker stripping, discovery ---

from app.core.rate_limiter import (  # noqa: E402
    _api_key_bucket,
    _client_id,
    _credentials_were_rejected,
    rate_limit_discovery,
)


def test_api_key_buckets_are_digests_not_plaintext():
    bucket = _api_key_bucket("live-secret-key")
    assert "live-secret-key" not in bucket
    assert _api_key_bucket("live-secret-key") == bucket
    assert _api_key_bucket("other-key") != bucket
    assert _api_key_bucket(None) == "anonymous"
    assert _api_key_bucket("key") == _api_key_bucket("key".strip())


def test_credentials_rejected_strips_marker_and_detects_401():
    from fastapi import Response

    from app.core.auth import CREDENTIAL_REJECTED_HEADER

    marked = Response("x", status_code=403, headers={CREDENTIAL_REJECTED_HEADER: "1"})
    assert _credentials_were_rejected(marked) is True
    assert CREDENTIAL_REJECTED_HEADER not in marked.headers
    assert _credentials_were_rejected(Response("x", status_code=401)) is True
    assert _credentials_were_rejected(Response("x", status_code=200)) is False


def test_client_id_uses_peer_without_railway_marker(monkeypatch):
    from fastapi import Request

    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("1.2.3.4", 5000),
        }
    )
    assert _client_id(req) == "1.2.3.4"


def test_rate_limit_discovery_shape():
    disc = rate_limit_discovery()
    assert disc["requests_per_minute"] == get_settings().RATE_LIMIT_PER_MINUTE
    assert disc["window_seconds"] == 60
    assert disc["burst_allowance"] == 0
    assert disc["per_partner_override"] is False


# --- health: sanitized errors, public projection, not_probed honesty ---

from app.core.health import (  # noqa: E402
    _OK_STATUSES,
    _check_llm,
    _run_check,
    build_public_dependency_report,
)


@pytest.mark.anyio
async def test_run_check_sanitizes_errors_and_times():
    async def boom():
        raise RuntimeError("secret-host-marker-123")

    result = await _run_check("x", boom)
    assert result["status"] == "down"
    assert result["error"] == "RuntimeError"
    assert "secret-host-marker-123" not in str(result)
    assert result["latency_ms"] >= 0


@pytest.mark.anyio
async def test_unknown_llm_provider_is_not_probed_and_not_ok(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "LLM_API_KEY", "k")
    monkeypatch.setattr(settings, "SIMULATION_MODE_TELEMETRY_PM", False)
    result = await _check_llm({"telemetry_pm": False})
    assert result["status"] == "not_probed"
    assert "not_probed" not in _OK_STATUSES  # pins: degrades overall health


def _full_report(**overrides):
    report = {
        "status": "healthy",
        "version": "1.3.0",
        "commit_sha": None,
        "build_provenance": "unstamped",
        "environment": "local",
        "production_like": False,
        "dependencies": {
            "postgres": {"status": "up"},
            "redis": {"status": "up"},
            "signing_key": {"status": "up"},
            "upstream_mcp": {"status": "up"},
            "llm": {"status": "down", "error": "X"},
        },
        "enable_proof_surfaces": False,
        "runtime_degradation": {"degraded": False},
        "metric_scopes": {},
        "unhealthy": ["llm"],
    }
    report.update(overrides)
    return report


def test_public_projection_hides_non_wedge_deps():
    public = build_public_dependency_report(_full_report())
    assert set(public["dependencies"]) == {
        "postgres",
        "redis",
        "signing_key",
        "upstream_mcp",
    }
    assert public["unhealthy"] == []
    assert public["status"] == "healthy"


def test_public_projection_keeps_wedge_failures():
    report = _full_report(
        dependencies={
            "postgres": {"status": "up"},
            "redis": {"status": "up"},
            "signing_key": {"status": "down", "error": "X"},
            "upstream_mcp": {"status": "up"},
        }
    )
    public = build_public_dependency_report(report)
    assert public["unhealthy"] == ["signing_key"]
    assert public["status"] == "degraded"
    degraded = build_public_dependency_report(
        _full_report(runtime_degradation={"degraded": True})
    )
    assert degraded["status"] == "degraded"
    assert "runtime_degradation" in degraded["unhealthy"]


# --- durable_state: backend resolution, sqlite form fix, roundtrip ---

from app.core.durable_state import (  # noqa: E402
    DurableStateConfigError,
    DurableStateStore,
    _json_default,
)


def _durable(monkeypatch, **attrs):
    settings = get_settings()
    base = {
        "STATE_BACKEND": "auto",
        "STATE_NAMESPACE": "qa",
        "DATABASE_URL": "",
        "REDIS_URL": "",
        "SQLITE_URL": "",
        "ENVIRONMENT": "test",
    }
    base.update(attrs)
    for key, value in base.items():
        monkeypatch.setattr(settings, key, value)
    return DurableStateStore()


def test_durable_explicit_backend_missing_fails_closed_in_prod(monkeypatch):
    with pytest.raises(DurableStateConfigError):
        _durable(
            monkeypatch, STATE_BACKEND="postgres", ENVIRONMENT="production"
        )._resolve_backend()
    with pytest.raises(DurableStateConfigError):
        _durable(
            monkeypatch, STATE_BACKEND="sqlite", ENVIRONMENT="production"
        )._resolve_backend()
    with pytest.raises(DurableStateConfigError):
        _durable(monkeypatch, ENVIRONMENT="production")._resolve_backend()


def test_durable_explicit_backend_missing_falls_back_in_dev(monkeypatch):
    from app.core.runtime_degradation import (
        get_runtime_degradation as snap,
        reset_runtime_degradation as reset,
    )

    reset()
    try:
        store = _durable(monkeypatch, STATE_BACKEND="postgres")
        assert store._resolve_backend() == "memory"
        assert snap()["durable_state"]["fell_back_to_memory"] is True
    finally:
        reset()


def test_durable_auto_resolution_matrix(monkeypatch):
    assert (
        _durable(
            monkeypatch, DATABASE_URL="sqlite+aiosqlite:///./x.db"
        )._resolve_backend()
        == "sqlite"
    )
    assert (
        _durable(monkeypatch, REDIS_URL="redis://localhost:6379")._resolve_backend()
        == "redis"
    )
    assert _durable(monkeypatch)._resolve_backend() == "memory"


def test_durable_sqlite_url_accepts_url_and_path_forms(monkeypatch, tmp_path):
    as_url = f"sqlite+aiosqlite:///{tmp_path}/s.db"
    assert _durable(monkeypatch, SQLITE_URL=as_url)._sqlite_url == f"{tmp_path}/s.db"
    raw = str(tmp_path / "r.db")
    assert _durable(monkeypatch, SQLITE_URL=raw)._sqlite_url == raw
    assert _durable(monkeypatch, SQLITE_URL=":memory:")._sqlite_url == ":memory:"
    assert _durable(monkeypatch, SQLITE_URL="sqlite:///:memory:")._sqlite_url == ""


@pytest.mark.anyio
async def test_durable_sqlite_roundtrip_and_memory_noop(monkeypatch, tmp_path):
    store = _durable(
        monkeypatch, STATE_BACKEND="sqlite", SQLITE_URL=str(tmp_path / "s.db")
    )
    try:
        assert await store.save_json("a:1", {"n": 1}) is True
        assert await store.load_json("a:1") == {"n": 1}
        assert await store.list_keys("a:") == ["a:1"]
        assert await store.delete("a:1") is True
        assert await store.load_json("a:1") is None
        assert (await store.health_report())["backend"] == "sqlite"
    finally:
        await store.close()

    mem = _durable(monkeypatch)
    try:
        assert await mem.save_json("k", {}) is False
        assert await mem.load_json("k") is None
        assert await mem.list_keys() == []
        assert await mem.delete("k") is False
    finally:
        await mem.close()


def test_durable_json_default_handles_types():
    assert _json_default(Decimal("1.5")) == 1.5
    assert _json_default(datetime(2026, 1, 1, 12)) == "2026-01-01T12:00:00"
    assert sorted(_json_default({"b", "a"})) == ["a", "b"]
    with pytest.raises(TypeError):
        _json_default(object())


# --- database: create_all gate, bind normalization, schema verify ---

from app.db.database import (  # noqa: E402
    SchemaInitError,
    _normalize_bind_parameters,
    allows_metadata_create_all,
    verify_required_schema,
)


def test_allows_create_all_only_for_ephemeral_sqlite(monkeypatch):
    monkeypatch.delenv("ALLOW_METADATA_CREATE_ALL", raising=False)
    assert allows_metadata_create_all(dialect_name="sqlite", environment="test") is True
    assert (
        allows_metadata_create_all(dialect_name="sqlite", environment="prod") is False
    )
    assert (
        allows_metadata_create_all(dialect_name="postgresql", environment="test")
        is False
    )
    monkeypatch.setenv("ALLOW_METADATA_CREATE_ALL", "true")
    assert allows_metadata_create_all(dialect_name="sqlite", environment="prod") is True
    assert (
        allows_metadata_create_all(dialect_name="postgresql", environment="prod")
        is False
    )


def test_normalize_bind_parameters_coerces_aware():
    aware = datetime(2026, 1, 2, 3, tzinfo=timezone.utc)
    assert _normalize_bind_parameters({"ts": aware}) == {"ts": datetime(2026, 1, 2, 3)}
    naive = datetime(2026, 1, 2, 3)
    params = {"ts": naive, "n": 1}
    assert _normalize_bind_parameters(params) is params
    assert _normalize_bind_parameters([aware])[0] == naive
    assert _normalize_bind_parameters((aware,))[0] == naive
    assert _normalize_bind_parameters("plain") == "plain"


@pytest.mark.anyio
async def test_verify_required_schema_gate():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    import app.db.models  # noqa: F401  (register tables)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        with pytest.raises(SchemaInitError, match="Required database tables missing"):
            await verify_required_schema(engine)
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        await verify_required_schema(engine)
    finally:
        await engine.dispose()


# --- converters: money precision, lenient metadata, strict enums ---

from app.db.converters import (  # noqa: E402
    ledger_entry_model_to_schema,
    metadata_dict_to_json,
    parse_metadata_json,
    telemetry_event_model_to_schema,
    telemetry_event_to_model,
    wallet_model_to_response,
)
from app.db.models import LedgerEntryModel, WalletModel  # noqa: E402


def test_wallet_converter_keeps_exact_decimals():
    row = WalletModel(
        wallet_id="w1",
        wallet_type="sponsor",
        balance=Decimal("10.5"),
        lifetime_credits=Decimal("100"),
        lifetime_debits=Decimal("89.5"),
    )
    out = wallet_model_to_response(row)
    assert out.balance == 10.5
    assert out.balance_exact == "10.5"
    assert out.lifetime_debits_exact == "89.5"


def test_wallet_converter_lenient_metadata_and_kyc_but_strict_type():
    row = WalletModel(
        wallet_id="w1",
        wallet_type="sponsor",
        metadata_json="{broken",
        kyc_status="mystery",
    )
    out = wallet_model_to_response(row)
    assert out.metadata == {}
    # Display-only leniency: enforcement reads the model row, not this schema.
    assert out.kyc_status.value == "not_required"
    with pytest.raises(ValueError):
        wallet_model_to_response(WalletModel(wallet_id="w1", wallet_type="bogus"))


def test_ledger_converter_preserves_eight_places():
    row = LedgerEntryModel(
        entry_id="e1",
        wallet_id="w1",
        action="credit",
        amount=Decimal("1.12345678"),
        balance_after=Decimal("11.12345678"),
    )
    out = ledger_entry_model_to_schema(row)
    assert out.amount_exact == "1.12345678"
    assert out.balance_after_exact == "11.12345678"
    with pytest.raises(ValueError):
        ledger_entry_model_to_schema(
            LedgerEntryModel(
                entry_id="e2",
                wallet_id="w1",
                action="bogus",
                amount=Decimal("1"),
                balance_after=Decimal("1"),
            )
        )


def test_metadata_helpers_roundtrip():
    assert metadata_dict_to_json({}) is None
    assert metadata_dict_to_json(None) is None
    assert parse_metadata_json(metadata_dict_to_json({"a": 1})) == {"a": 1}
    assert parse_metadata_json("{broken") == {}
    assert parse_metadata_json(None) == {}


def test_telemetry_converter_normalizes_aware_timestamps():
    from app.schemas.telemetry import TelemetryEvent, TelemetryEventType

    aware = datetime(2026, 1, 2, 3, tzinfo=timezone.utc)
    event = TelemetryEvent(
        event_type=TelemetryEventType.ERROR,
        source="s",
        message="m",
        metadata={"k": 1},
        timestamp=aware,
    )
    row = telemetry_event_to_model("ev1", "b1", event, aware)
    assert row.event_timestamp == datetime(2026, 1, 2, 3)
    back = telemetry_event_model_to_schema(row)
    assert back.timestamp == datetime(2026, 1, 2, 3)
    assert back.metadata == {"k": 1}


# --- resilience: retry, breaker, write-conflict restart ---

from sqlalchemy.exc import OperationalError  # noqa: E402

from app.core.resilience import (  # noqa: E402
    CircuitBreaker,
    CircuitBreakerOpen,
    is_retryable_write_conflict,
    retry_with_backoff,
    run_with_write_conflict_retry,
)


def _conflict(message="database is locked"):
    return OperationalError("SELECT 1", {}, Exception(message))


@pytest.mark.anyio
async def test_retry_succeeds_after_failures_then_exhausts():
    calls = 0

    @retry_with_backoff(max_attempts=3, base_delay=0)
    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("boom")
        return "ok"

    assert await flaky() == "ok"

    @retry_with_backoff(max_attempts=2, base_delay=0)
    async def down():
        raise RuntimeError("still down")

    with pytest.raises(RuntimeError, match="still down"):
        await down()


def test_retry_guards_reject_zero_attempts():
    with pytest.raises(ValueError):
        retry_with_backoff(max_attempts=0)


@pytest.mark.anyio
async def test_write_conflict_retry_restarts_and_maps_exhaustion():
    attempts = 0

    async def op():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _conflict()
        return "done"

    assert await run_with_write_conflict_retry(op, on_exhausted=lambda e: e) == "done"

    async def stuck():
        raise _conflict()

    with pytest.raises(RuntimeError, match="busy"):
        await run_with_write_conflict_retry(
            stuck, on_exhausted=lambda e: RuntimeError("busy"), max_attempts=2
        )


@pytest.mark.anyio
async def test_write_conflict_retry_passes_through_real_errors():
    async def broken():
        raise _conflict("no such table: wallets")

    with pytest.raises(OperationalError):
        await run_with_write_conflict_retry(broken, on_exhausted=lambda e: e)

    seen = 0

    async def stale_then_ok():
        nonlocal seen
        seen += 1
        if seen == 1:
            raise ValueError("stale row, restart me")
        return "ok"

    assert (
        await run_with_write_conflict_retry(
            stale_then_ok,
            on_exhausted=lambda e: e,
            restart_on=lambda e: isinstance(e, ValueError) and "stale" in str(e),
        )
        == "ok"
    )


@pytest.mark.anyio
async def test_write_conflict_guard_rejects_zero_attempts():
    async def op():
        return 1

    with pytest.raises(ValueError):
        await run_with_write_conflict_retry(
            op, on_exhausted=lambda e: e, max_attempts=0
        )


def test_write_conflict_classifier():
    assert is_retryable_write_conflict(_conflict("database is locked")) is True
    assert is_retryable_write_conflict(_conflict("snapshot conflict")) is True
    assert is_retryable_write_conflict(_conflict("no such table")) is False


def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(
        failure_threshold=2, recovery_timeout=30, half_open_max_calls=1
    )
    breaker.record_failure()
    assert breaker.is_allowed() is True
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.is_allowed() is False
    with pytest.raises(CircuitBreakerOpen):
        import anyio

        anyio.run(breaker.call, _sync_boom)

    recovering = CircuitBreaker(
        failure_threshold=1, recovery_timeout=0, half_open_max_calls=1
    )
    recovering.record_failure()
    # recovery_timeout=0 expires immediately, so the next read is half_open.
    assert recovering.state == "half_open"
    assert recovering.is_allowed() is True
    recovering.record_success()
    assert recovering.state == "closed"


def _sync_boom():
    raise RuntimeError("nope")


# --- sql_expressions and db types ---


def test_clamped_decrement_never_goes_negative():
    from sqlalchemy import Column, MetaData, Numeric, Table, create_engine, select

    from app.db.sql_expressions import clamped_decrement

    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    table = Table(
        "t", meta, Column("id", Numeric, primary_key=True), Column("balance", Numeric)
    )
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(id=1, balance=Decimal("5")))
        conn.execute(
            table.update().values(
                balance=clamped_decrement(table.c.balance, Decimal("3"))
            )
        )
        assert conn.execute(select(table.c.balance)).scalar() == Decimal("2")
        conn.execute(
            table.update().values(
                balance=clamped_decrement(table.c.balance, Decimal("10"))
            )
        )
        assert conn.execute(select(table.c.balance)).scalar() == Decimal("0")


def test_naive_utc_type_coerces_binds():
    from app.db.types import NaiveUTCDateTime

    aware = datetime(2026, 1, 2, 3, tzinfo=timezone.utc)
    assert NaiveUTCDateTime().process_bind_param(aware, None) == datetime(2026, 1, 2, 3)
    assert NaiveUTCDateTime().process_bind_param(None, None) is None


# --- dependencies: singletons must read current settings (fix) ---


def test_singletons_read_current_settings(monkeypatch):
    from datetime import timedelta as _td

    from app.core import dependencies as deps

    monkeypatch.setenv("MQTT_BROKER_URL", "mqtt://custom:1883")
    monkeypatch.setenv("TELEMETRY_RETENTION_HOURS", "99")
    get_settings.cache_clear()
    deps.get_iot_bridge.cache_clear()
    deps.get_autonomous_pm.cache_clear()
    try:
        assert deps.get_iot_bridge().mqtt.broker_url == "mqtt://custom:1883"
        assert deps.get_autonomous_pm().event_store._retention == _td(hours=99)
    finally:
        deps.get_iot_bridge.cache_clear()
        deps.get_autonomous_pm.cache_clear()
        get_settings.cache_clear()
