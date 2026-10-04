"""Tests for the enterprise IGA bridge (app/core/oidc_iga.py).

Service-level: tokens are minted in-test with a locally generated RSA key,
issuer keys are pinned via IGA_TRUSTED_ISSUERS, and PolicyBundle/Wallet rows
are inserted directly through the session factory (mirroring how
tests/test_policy_bundles.py provisions wallets, minus the HTTP hop).
FastAPI dependency behavior is tested by calling the dependencies directly.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.exc import OperationalError
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.core.oidc_iga as oidc_iga
from app.core.auth import get_enterprise_principal, require_enterprise_tool_access
from app.core.config import get_settings
from app.core.oidc_iga import (
    IGAError,
    enforce_tool_call,
    parse_enterprise_token,
    reset_iga_counters,
    resolve_policy_grants,
)
from app.db.database import get_session_factory
from app.db.models import PolicyBundleModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry

from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


OKTA_ISS = "https://example.okta.com/oauth2/default"
ENTRA_ISS = (
    "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0"
)
AUDIENCE = "api://agent-middleware"
KID = "iga-test-kid"
TOOL = "demo.tool"


# --- key material / token helpers -------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    # 2048-bit generation is slow enough to share across the module; the key
    # never leaves the test process.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def wrong_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(private_key) -> bytes:
    return private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _b64url_uint(value: int) -> str:
    data = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwks(private_key, kid: str = KID) -> dict:
    numbers = private_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": kid,
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            }
        ]
    }


def _mint(
    private_key,
    *,
    iss: str = OKTA_ISS,
    aud: str = AUDIENCE,
    sub: str = "user-1",
    kid: str = KID,
    exp_delta: int = 300,
    extra: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now - timedelta(seconds=600),
        "exp": now + timedelta(seconds=exp_delta),
    }
    if extra:
        claims.update(extra)
    return jwt.encode(
        claims, _pem(private_key), algorithm="RS256", headers={"kid": kid}
    )


def _okta_issuers(private_key) -> dict:
    # provider omitted on purpose: inferred from the example.okta.com host.
    return {
        OKTA_ISS: {
            "audience": AUDIENCE,
            "algorithms": ["RS256"],
            "jwks": _jwks(private_key),
        }
    }


def _entra_issuers(private_key) -> dict:
    # provider omitted on purpose: inferred from login.microsoftonline.com.
    return {
        ENTRA_ISS: {
            "audience": AUDIENCE,
            "algorithms": ["RS256"],
            "jwks": _jwks(private_key),
        }
    }


# --- settings / counters fixtures --------------------------------------------


@pytest.fixture
def iga_config():
    """Apply IGA env config with get_settings.cache_clear(), restoring after.

    Env + cache_clear is the repo's settings-override idiom (see
    tests/test_billing.py); restoration happens here rather than via
    monkeypatch so the cache is cleared AFTER the env vars are removed.
    """

    def _apply(trusted: dict | str, group_map: dict | str = "") -> None:
        os.environ["IGA_TRUSTED_ISSUERS"] = (
            trusted if isinstance(trusted, str) else json.dumps(trusted)
        )
        os.environ["IGA_GROUP_POLICY_MAP"] = (
            group_map if isinstance(group_map, str) else json.dumps(group_map)
        )
        get_settings.cache_clear()

    yield _apply
    os.environ.pop("IGA_TRUSTED_ISSUERS", None)
    os.environ.pop("IGA_GROUP_POLICY_MAP", None)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_iga_counters():
    """Runtime cap counters are process-global; isolate every test."""
    reset_iga_counters()
    yield
    reset_iga_counters()


# --- DB row helpers -----------------------------------------------------------


async def _make_wallet() -> str:
    wallet_id = f"iga-w-{uuid.uuid4().hex[:10]}"
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            WalletModel(
                wallet_id=wallet_id,
                wallet_type="agent",
                balance=Decimal("1000"),
            )
        )
        await session.commit()
    return wallet_id


async def _make_bundle(
    wallet_id: str,
    *,
    allowed_tools: list[str] | None,
    is_active: bool = True,
) -> str:
    policy_id = f"polb-{uuid.uuid4().hex[:16]}"
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            PolicyBundleModel(
                policy_id=policy_id,
                wallet_id=wallet_id,
                name="IGA test bundle",
                allowed_tools_json=(
                    json.dumps(allowed_tools) if allowed_tools is not None else None
                ),
                is_active=is_active,
            )
        )
        await session.commit()
    return policy_id


# --- happy path / claim shapes ------------------------------------------------


async def test_okta_principal_with_mapped_active_bundle_allows(
    iga_config, clean_database, rsa_key
):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

    token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
    principal = parse_enterprise_token(token)
    assert principal.provider == "okta"
    assert principal.issuer == OKTA_ISS
    assert principal.subject == "user-1"
    assert principal.groups == ("payments-ops",)

    decision = await enforce_tool_call(principal, TOOL)
    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.group == "payments-ops"
    assert decision.policy_id == policy_id


async def test_tool_outside_bundle_allowlist_is_blocked(
    iga_config, clean_database, rsa_key
):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )
    decision = await enforce_tool_call(principal, "some.other.tool")
    assert decision.allowed is False
    assert decision.reason == "iga_tool_not_allowed"
    assert decision.policy_id == policy_id


@pytest.mark.parametrize("corrupt", ["not-json", '{"a": 1}', "null", f'"{TOOL}"'])
async def test_corrupt_bundle_allowlist_blocks_instead_of_allowing_everything(
    iga_config, clean_database, rsa_key, corrupt
):
    """A stored allowlist that is present but not a JSON array of strings must
    not read as an unset (unrestricted) one -- not even for the tool the
    bundle was written to allow. NULL keeps meaning "no restriction"."""
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(PolicyBundleModel, policy_id)
        assert row is not None
        row.allowed_tools_json = corrupt
        session.add(row)
        await session.commit()
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )
    for tool_name in (TOOL, "some.other.tool"):
        decision = await enforce_tool_call(principal, tool_name)
        assert decision.allowed is False, tool_name
        assert decision.reason == "iga_tool_not_allowed", tool_name
        assert decision.policy_id == policy_id


async def test_unauthorized_principal_without_required_role_is_blocked(
    iga_config, clean_database, rsa_key
):
    """Acceptance criterion: lacking the required Okta/Entra role blocks."""
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

    principal = parse_enterprise_token(
        _mint(rsa_key, sub="intruder", extra={"groups": ["random-team"]})
    )
    assert resolve_policy_grants(principal) == []

    decision = await enforce_tool_call(principal, TOOL)
    assert decision.allowed is False
    assert decision.reason == "iga_no_matching_role"


async def test_entra_roles_claim_allows(iga_config, clean_database, rsa_key):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(_entra_issuers(rsa_key), {"Payments.Operator": {"policy_id": policy_id}})

    token = _mint(
        rsa_key, iss=ENTRA_ISS, sub="entra-user", extra={"roles": ["Payments.Operator"]}
    )
    principal = parse_enterprise_token(token)
    assert principal.provider == "entra"
    assert principal.groups == ("Payments.Operator",)

    decision = await enforce_tool_call(principal, TOOL)
    assert decision.allowed is True
    assert decision.policy_id == policy_id


async def test_entra_groups_claim_fallback(iga_config, rsa_key):
    iga_config(_entra_issuers(rsa_key))
    token = _mint(
        rsa_key, iss=ENTRA_ISS, sub="entra-user", extra={"groups": ["entra-group-1"]}
    )
    principal = parse_enterprise_token(token)
    assert principal.provider == "entra"
    # No roles claim: group-claims configuration falls back to `groups`.
    assert principal.groups == ("entra-group-1",)


async def test_missing_group_claim_is_empty_not_error(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    principal = parse_enterprise_token(_mint(rsa_key))
    assert principal.groups == ()


# --- verification negative paths ---------------------------------------------


async def test_expired_token_rejected(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key, exp_delta=-60))
    assert excinfo.value.reason == "iga_token_expired"


async def test_wrong_audience_rejected(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key, aud="api://someone-else"))
    assert excinfo.value.reason == "iga_audience_mismatch"


async def test_unknown_issuer_rejected(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key, iss="https://evil.example.com"))
    assert excinfo.value.reason == "iga_issuer_not_trusted"


async def test_token_signed_with_wrong_key_rejected(iga_config, rsa_key, wrong_rsa_key):
    iga_config(_okta_issuers(rsa_key))
    # Same kid so key selection succeeds and the signature check itself fails.
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(wrong_rsa_key, kid=KID))
    assert excinfo.value.reason == "iga_signature_invalid"


async def test_unknown_kid_rejected(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key, kid="some-unpinned-kid"))
    assert excinfo.value.reason == "iga_signing_key_not_found"


async def test_hs256_token_rejected_when_only_rs256_allowed(iga_config, rsa_key):
    """Alg-confusion negative test: symmetric alg against an RS256 allowlist."""
    iga_config(_okta_issuers(rsa_key))
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": OKTA_ISS,
            "aud": AUDIENCE,
            "sub": "user-1",
            "iat": now,
            "exp": now + timedelta(seconds=300),
        },
        "shared-secret-material-of-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": KID},
    )
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(token)
    assert excinfo.value.reason == "iga_algorithm_not_allowed"


async def test_malformed_token_rejected(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token("not-a-jwt")
    assert excinfo.value.reason == "iga_token_malformed"


async def test_malformed_issuer_config_fails_closed_at_use_time(iga_config, rsa_key):
    iga_config("{this is not json", "")
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key))
    assert excinfo.value.reason == "iga_config_invalid"


async def test_malformed_issuer_config_disables_routing_loudly(
    iga_config, rsa_key, caplog
):
    """is_iga_issuer_token swallows a config IGAError (routing must fail
    closed to the internal-JWT verifier), but NOT silently: disabling the
    whole enterprise layer is logged at error level — reason only, never
    token material."""
    iga_config("{this is not json", "")
    token = _mint(rsa_key)
    with caplog.at_level(logging.ERROR, logger="app.core.oidc_iga"):
        assert oidc_iga.is_iga_issuer_token(token) is False
    messages = [record.getMessage() for record in caplog.records]
    assert any("iga_config_invalid" in message for message in messages), messages
    assert all(token not in message for message in messages)


async def test_malformed_group_map_fails_closed_at_use_time(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key), "{this is not json")
    principal = parse_enterprise_token(_mint(rsa_key, extra={"groups": ["x"]}))
    with pytest.raises(IGAError) as excinfo:
        resolve_policy_grants(principal)
    assert excinfo.value.reason == "iga_config_invalid"


@pytest.mark.parametrize(
    "make_entry",
    [
        pytest.param(
            lambda jwks: {
                "audience": AUDIENCE,
                "algorithms": ["RS256", "none"],
                "jwks": jwks,
            },
            id="algorithm-none-listed",
        ),
        pytest.param(
            lambda jwks: {
                "audience": AUDIENCE,
                "algorithms": ["RS256"],
                "jwks": jwks,
                "public_key_pem": "-----BEGIN PUBLIC KEY-----\nirrelevant",
            },
            id="both-key-sources",
        ),
        pytest.param(
            lambda jwks: {"audience": AUDIENCE, "algorithms": ["RS256"]},
            id="no-key-source",
        ),
        pytest.param(
            lambda jwks: {"algorithms": ["RS256"], "jwks": jwks},
            id="missing-audience",
        ),
        pytest.param(
            lambda jwks: {"audience": "   ", "algorithms": ["RS256"], "jwks": jwks},
            id="blank-audience",
        ),
    ],
)
async def test_structurally_invalid_issuer_config_fails_closed(
    iga_config, rsa_key, make_entry
):
    """Every malformed issuer entry poisons the whole roster (fail closed)."""
    iga_config({OKTA_ISS: make_entry(_jwks(rsa_key))})
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key))
    assert excinfo.value.reason == "iga_config_invalid"


async def test_unknown_issuer_host_without_explicit_provider_fails_closed(
    iga_config, rsa_key
):
    """A vanity/unknown IdP host needs an explicit provider; guessing is out."""
    vanity_iss = "https://idp.internal.example.com"
    iga_config(
        {
            vanity_iss: {
                "audience": AUDIENCE,
                "algorithms": ["RS256"],
                "jwks": _jwks(rsa_key),
            }
        }
    )
    with pytest.raises(IGAError) as excinfo:
        parse_enterprise_token(_mint(rsa_key, iss=vanity_iss))
    assert excinfo.value.reason == "iga_config_invalid"


@pytest.mark.parametrize(
    "group_entry",
    [
        pytest.param(
            {"policy_id": "polb-x", "velocity_window_seconds": 60},
            id="velocity-window-without-max-calls",
        ),
        pytest.param(
            {"policy_id": "polb-x", "velocity_max_calls": 3},
            id="velocity-max-calls-without-window",
        ),
        pytest.param({"policy_id": "polb-x", "max_uses": 0}, id="max-uses-zero"),
        pytest.param({"policy_id": "polb-x", "max_uses": -1}, id="max-uses-negative"),
        pytest.param({"policy_id": "polb-x", "max_uses": True}, id="max-uses-bool"),
    ],
)
async def test_structurally_invalid_group_map_fails_closed(
    iga_config, rsa_key, group_entry
):
    """Half-set velocity caps and non-positive/bool max_uses never enforce
    silently — the whole grant map fails closed at use time."""
    iga_config(_okta_issuers(rsa_key), {"payments-ops": group_entry})
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )
    with pytest.raises(IGAError) as excinfo:
        resolve_policy_grants(principal)
    assert excinfo.value.reason == "iga_config_invalid"


# --- runtime caps -------------------------------------------------------------


async def test_max_uses_exhausted_blocks_third_call(
    iga_config, clean_database, rsa_key
):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(
        _okta_issuers(rsa_key),
        {"payments-ops": {"policy_id": policy_id, "max_uses": 2}},
    )
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )

    first = await enforce_tool_call(principal, TOOL)
    second = await enforce_tool_call(principal, TOOL)
    assert first.allowed is True and second.allowed is True

    third = await enforce_tool_call(principal, TOOL)
    assert third.allowed is False
    assert third.reason == "iga_max_uses_exceeded"
    assert third.details == {"used": 2, "limit": 2}


async def test_velocity_window_blocks_burst_then_recovers(
    iga_config, clean_database, rsa_key, monkeypatch
):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(
        _okta_issuers(rsa_key),
        {
            "payments-ops": {
                "policy_id": policy_id,
                "velocity_window_seconds": 60,
                "velocity_max_calls": 3,
            }
        },
    )
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(oidc_iga, "_monotonic", lambda: clock["now"])

    for _ in range(3):
        assert (await enforce_tool_call(principal, TOOL)).allowed is True

    fourth = await enforce_tool_call(principal, TOOL)
    assert fourth.allowed is False
    assert fourth.reason == "iga_velocity_exceeded"
    assert fourth.details == {"window_seconds": 60, "calls_in_window": 3, "limit": 3}

    # Once the window passes, the burst has aged out and calls flow again.
    clock["now"] = 1061.0
    recovered = await enforce_tool_call(principal, TOOL)
    assert recovered.allowed is True


async def test_velocity_windows_are_tracked_per_grant(
    iga_config, clean_database, rsa_key, monkeypatch
):
    """Each grant's velocity history is its own (regression for the shared
    (issuer, subject, tool) counter key): the short-window grant's pruning
    must not erase the history the long-window grant still needs, and one
    grant's calls must not count against another's cap."""
    wallet_id = await _make_wallet()
    short_policy = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    long_policy = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(
        _okta_issuers(rsa_key),
        {
            "ops-short": {
                "policy_id": short_policy,
                "velocity_window_seconds": 60,
                "velocity_max_calls": 2,
            },
            "ops-long": {
                "policy_id": long_policy,
                "velocity_window_seconds": 3600,
                "velocity_max_calls": 2,
            },
        },
    )
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["ops-short", "ops-long"]})
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(oidc_iga, "_monotonic", lambda: clock["now"])

    async def _call_at(now: float):
        clock["now"] = now
        return await enforce_tool_call(principal, TOOL)

    # Burst: the short grant authorizes two calls, then the long grant takes
    # over for two more. Under the old shared deque the third call would have
    # been BLOCKED — the long grant counted the short grant's calls as its own.
    for now, expected_group in (
        (1000.0, "ops-short"),
        (1001.0, "ops-short"),
        (1002.0, "ops-long"),
        (1003.0, "ops-long"),
    ):
        decision = await _call_at(now)
        assert decision.allowed is True, (now, decision)
        assert decision.group == expected_group

    fifth = await _call_at(1004.0)
    assert fifth.allowed is False
    assert fifth.reason == "iga_velocity_exceeded"

    # The short window recovers at t=1100 — and its pruning at that moment is
    # exactly the operation that used to destroy the long grant's history.
    for now in (1100.0, 1101.0):
        decision = await _call_at(now)
        assert decision.allowed is True
        assert decision.group == "ops-short"

    # No empty deques may linger after pruning (idle-principal eviction).
    assert all(window for window in oidc_iga._window_calls.values())

    # The long grant still remembers the two calls IT authorized at
    # t=1002/1003 — they are within its hour, so with the short grant capped
    # again the call is blocked rather than slipping through ungoverned.
    blocked = await _call_at(1102.0)
    assert blocked.allowed is False
    assert blocked.reason == "iga_velocity_exceeded"
    long_key = (OKTA_ISS, "user-1", TOOL, "ops-long", long_policy)
    assert [use.recorded_at for use in oidc_iga._window_calls[long_key]] == [
        1002.0,
        1003.0,
    ]


async def test_max_uses_are_tracked_per_grant(iga_config, clean_database, rsa_key):
    """Two grants of max_uses=2 yield 2 uses EACH, drawn down independently."""
    wallet_id = await _make_wallet()
    policy_a = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    policy_b = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(
        _okta_issuers(rsa_key),
        {
            "ops-a": {"policy_id": policy_a, "max_uses": 2},
            "ops-b": {"policy_id": policy_b, "max_uses": 2},
        },
    )
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["ops-a", "ops-b"]})
    )

    groups = []
    for _ in range(4):
        decision = await enforce_tool_call(principal, TOOL)
        assert decision.allowed is True
        groups.append(decision.group)
    # Under the old shared lifetime counter the third call would have been
    # blocked (2 total instead of 2 each).
    assert groups == ["ops-a", "ops-a", "ops-b", "ops-b"]

    fifth = await enforce_tool_call(principal, TOOL)
    assert fifth.allowed is False
    assert fifth.reason == "iga_max_uses_exceeded"
    assert fifth.details == {"used": 2, "limit": 2}


async def test_release_tool_use_compensates_exactly_and_clamps_at_zero(
    iga_config, clean_database, rsa_key
):
    """release_tool_use hands back exactly one recorded use for the exact
    grant, and an over-release is a no-op (counters never go negative)."""
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(
        _okta_issuers(rsa_key),
        {"payments-ops": {"policy_id": policy_id, "max_uses": 2}},
    )
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )

    first = await enforce_tool_call(principal, TOOL)
    assert first.allowed is True
    await oidc_iga.release_tool_use(
        principal,
        TOOL,
        group="payments-ops",
        policy_id=policy_id,
        reservation=first.reservation,
    )
    assert oidc_iga._lifetime_uses == {}
    # Over-release must not create negative budget.
    await oidc_iga.release_tool_use(
        principal,
        TOOL,
        group="payments-ops",
        policy_id=policy_id,
        reservation=first.reservation,
    )
    assert oidc_iga._lifetime_uses == {}

    # The full max_uses budget is available again — and only that budget.
    assert (await enforce_tool_call(principal, TOOL)).allowed is True
    assert (await enforce_tool_call(principal, TOOL)).allowed is True
    blocked = await enforce_tool_call(principal, TOOL)
    assert blocked.allowed is False
    assert blocked.reason == "iga_max_uses_exceeded"


async def test_inactive_policy_bundle_blocks(iga_config, clean_database, rsa_key):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL], is_active=False)
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )
    decision = await enforce_tool_call(principal, TOOL)
    assert decision.allowed is False
    assert decision.reason == "iga_policy_inactive"


async def test_denial_prefers_tool_not_allowed_over_inactive(
    iga_config, clean_database, rsa_key
):
    wallet_id = await _make_wallet()
    inactive_id = await _make_bundle(wallet_id, allowed_tools=[TOOL], is_active=False)
    wrong_tool_id = await _make_bundle(wallet_id, allowed_tools=["other.tool"])
    iga_config(
        _okta_issuers(rsa_key),
        {
            "ops-a": {"policy_id": inactive_id},
            "ops-b": {"policy_id": wrong_tool_id},
        },
    )
    principal = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["ops-a", "ops-b"]})
    )
    decision = await enforce_tool_call(principal, TOOL)
    assert decision.allowed is False
    # tool-not-allowed proves a live grant existed — more informative than
    # a stale/inactive mapping.
    assert decision.reason == "iga_tool_not_allowed"


# --- FastAPI dependency wiring (called directly) ------------------------------


async def test_get_enterprise_principal_none_when_disabled_or_headerless(rsa_key):
    # IGA disabled (no IGA_TRUSTED_ISSUERS): even a Bearer header yields None.
    assert not get_settings().IGA_TRUSTED_ISSUERS
    assert await get_enterprise_principal(authorization=None) is None
    assert (
        await get_enterprise_principal(authorization=f"Bearer {_mint(rsa_key)}") is None
    )


async def test_get_enterprise_principal_ignores_internal_issuer_tokens(
    iga_config, rsa_key
):
    iga_config(_okta_issuers(rsa_key))
    assert await get_enterprise_principal(authorization=None) is None
    assert await get_enterprise_principal(authorization="Basic abc123") is None
    # The internal EdDSA flow's issuer is not IGA-trusted: fall through (None)
    # so get_auth_context keeps owning those tokens.
    internal_token = _mint(rsa_key, iss="agent-middleware-api")
    assert (
        await get_enterprise_principal(authorization=f"Bearer {internal_token}") is None
    )


async def test_get_enterprise_principal_401_on_bad_enterprise_token(
    iga_config, rsa_key
):
    iga_config(_okta_issuers(rsa_key))
    with pytest.raises(HTTPException) as excinfo:
        await get_enterprise_principal(
            authorization=f"Bearer {_mint(rsa_key, exp_delta=-60)}"
        )
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail["error"] == "iga_token_expired"


async def test_get_enterprise_principal_returns_verified_principal(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key))
    token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
    principal = await get_enterprise_principal(authorization=f"Bearer {token}")
    assert principal is not None
    assert principal.subject == "user-1"
    assert principal.groups == ("payments-ops",)


async def test_require_enterprise_tool_access_403_with_decision_reason(
    iga_config, clean_database, rsa_key
):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

    dependency = require_enterprise_tool_access(TOOL)

    # No enterprise principal: the layer is optional and enforces nothing.
    assert await dependency(principal=None) is None

    unauthorized = parse_enterprise_token(
        _mint(rsa_key, sub="intruder", extra={"groups": ["random-team"]})
    )
    with pytest.raises(HTTPException) as excinfo:
        await dependency(principal=unauthorized)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["error"] == "iga_no_matching_role"
    assert TOOL in excinfo.value.detail["message"]

    authorized = parse_enterprise_token(
        _mint(rsa_key, extra={"groups": ["payments-ops"]})
    )
    decision = await dependency(principal=authorized)
    assert decision is not None
    assert decision.allowed is True
    assert decision.policy_id == policy_id


# --- End-to-end enforcement on the real governed tool-call path ---------------
#
# These drive POST /mcp/messages (a CORE route) over the real app: wallet +
# key + permit are provisioned via the canonical helpers, a throwaway local
# tool is registered, and the enterprise bearer rides the Authorization
# header alongside the X-API-Key — the exact shape the auth fallthrough in
# get_auth_context and the IGA gate in _execute_registered_tool exist for.


E2E_TOOL = "iga-e2e-echo"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _register_e2e_tool(calls: list[dict]):
    """Register the throwaway tool; returns the registry for unregistering."""

    def _e2e_echo(message: str = "ok") -> dict:
        calls.append({"message": message})
        return {"message": message}

    registry = get_service_registry()
    registry.register_local(
        service_id=E2E_TOOL,
        name="IGA E2E Echo",
        description="Throwaway tool for IGA enforcement tests",
        category=ServiceCategory.AGENT_COMMS,
        func=_e2e_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    return registry


E2E_UPSTREAM_TOOL = "iga-e2e-upstream"


def _register_e2e_upstream_tool():
    """Register a remote-backed tool so a prepared dispatch attempt exists.

    The executor asserts rather than returning: every test using this drives a
    refusal that happens before dispatch, so reaching it means the gateway sent
    a call it had already decided to refuse.
    """

    class _NeverDispatched:
        async def call_tool(self, arguments, **_kwargs):
            raise AssertionError("upstream dispatched despite a pre-dispatch refusal")

    registry = get_service_registry()
    registry.register_upstream(
        service_id=E2E_UPSTREAM_TOOL,
        name="IGA E2E Upstream",
        description="Throwaway remote tool for IGA compensation tests",
        category=ServiceCategory.AGENT_COMMS,
        executor=_NeverDispatched(),
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        credits_per_unit=2.0,
        upstream_tool_name="partner.echo",
        upstream_origin="https://partner.example",
    )
    return registry


async def _invoke_tool_call(
    client: AsyncClient,
    *,
    wallet_id: str,
    permit_id: str,
    idem_key: str,
    headers: dict[str, str],
    tool_name: str = E2E_TOOL,
) -> dict:
    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": f"iga-{idem_key}",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"message": "hello"},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idem_key,
                },
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()


async def _provision_for_e2e(
    client: AsyncClient, *, idem_key: str, tool_name: str = E2E_TOOL
) -> dict:
    """Wallet + wallet-scoped key + permit for the tool, via the real routes."""
    setup = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=setup["agent_wallet_id"],
        key_id=setup["key_id"],
        tool_name=tool_name,
        idem_key=idem_key,
    )
    return {**setup, "permit_id": permit["permit_id"]}


async def test_enterprise_bearer_with_required_group_allows_governed_call(
    iga_config, clean_database, rsa_key, client
):
    """API-key auth falls through with the enterprise bearer; IGA allows."""
    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-allow-permit")
        # The bundle backing the IGA grant lives on its own wallet so the
        # agent wallet's own policy evaluation stays unconstrained.
        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_TOOL])
        iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

        token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
        payload = await _invoke_tool_call(
            client,
            wallet_id=setup["agent_wallet_id"],
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-allow-1",
            headers={
                **setup["agent_headers"],
                "Authorization": f"Bearer {token}",
            },
        )
        assert "result" in payload, payload
        assert payload["result"]["isError"] is False
        receipt = payload["result"]["receipt"]
        assert receipt["outcome"] == "success"
        assert receipt["permit_id"] == setup["permit_id"]
        assert calls == [{"message": "hello"}]
    finally:
        registry.unregister_local(E2E_TOOL)


async def test_enterprise_bearer_without_required_group_is_blocked_on_wire(
    iga_config, clean_database, rsa_key, client
):
    """Acceptance criterion on the REAL path: the human principal lacks the
    required Okta/Entra role, so the agent tool call is blocked outright —
    same denial envelope and signed denied receipt as permit denials."""
    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-deny-permit")
        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_TOOL])
        iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

        token = _mint(rsa_key, sub="intruder", extra={"groups": ["random-team"]})
        payload = await _invoke_tool_call(
            client,
            wallet_id=setup["agent_wallet_id"],
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-deny-1",
            headers={
                **setup["agent_headers"],
                "Authorization": f"Bearer {token}",
            },
        )
        assert "error" in payload, payload
        assert payload["error"]["message"] == "iga_no_matching_role"
        assert payload["error"]["code"] == -32003
        denied_receipt = payload["error"]["data"]["receipt"]
        assert denied_receipt["outcome"] == "denied"
        assert denied_receipt["credits_charged"] == "0"
        assert denied_receipt["reason_code"] == "iga_no_matching_role"
        # The tool itself must never have executed.
        assert calls == []
    finally:
        registry.unregister_local(E2E_TOOL)


async def test_enterprise_bearer_with_invalid_signature_is_denied_not_dispatched(
    iga_config, clean_database, rsa_key, wrong_rsa_key, client
):
    """A bearer FROM a pinned issuer that fails verification fails closed."""
    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-badsig-permit")
        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_TOOL])
        iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

        # Same iss/kid so the token routes to the IGA layer and key selection
        # succeeds; the signature check itself must fail.
        token = _mint(wrong_rsa_key, kid=KID, extra={"groups": ["payments-ops"]})
        payload = await _invoke_tool_call(
            client,
            wallet_id=setup["agent_wallet_id"],
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-badsig-1",
            headers={
                **setup["agent_headers"],
                "Authorization": f"Bearer {token}",
            },
        )
        assert "error" in payload, payload
        assert payload["error"]["message"] == "iga_signature_invalid"
        assert calls == []
    finally:
        registry.unregister_local(E2E_TOOL)


async def test_api_key_only_governed_call_unaffected_by_iga_config(
    iga_config, clean_database, rsa_key, client
):
    """Regression guard: no bearer at all — the governed loop is untouched."""
    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-nobearer-permit")
        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_TOOL])
        iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

        payload = await _invoke_tool_call(
            client,
            wallet_id=setup["agent_wallet_id"],
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-nobearer-1",
            headers=setup["agent_headers"],
        )
        assert "result" in payload, payload
        assert payload["result"]["isError"] is False
        assert payload["result"]["receipt"]["outcome"] == "success"
        assert calls == [{"message": "hello"}]
    finally:
        registry.unregister_local(E2E_TOOL)


async def test_insufficient_funds_denial_releases_iga_use(
    iga_config, clean_database, rsa_key, client
):
    """A pre-dispatch insufficient-funds refusal charges nothing and
    dispatches nothing, so the IGA use recorded at the gate is handed back:
    a max_uses=1 principal is NOT locked out forever by an under-funded
    wallet, and a properly funded retry succeeds."""
    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        # Provision funded (permit creation requires the subject wallet to
        # cover max_credits), then drain the wallet below the tool's cost.
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-funds-permit")
        wallet_id = setup["agent_wallet_id"]
        agent_headers = setup["agent_headers"]
        permit = {"permit_id": setup["permit_id"]}

        async def _set_balance(amount: str) -> None:
            factory = get_session_factory()
            async with factory() as session:
                wallet = await session.get(WalletModel, wallet_id)
                assert wallet is not None
                wallet.balance = Decimal(amount)
                session.add(wallet)
                await session.commit()

        # The tool costs 2 credits; a balance of 1 cannot cover it.
        await _set_balance("1")

        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_TOOL])
        iga_config(
            _okta_issuers(rsa_key),
            {"payments-ops": {"policy_id": policy_id, "max_uses": 1}},
        )
        token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
        headers = {**agent_headers, "Authorization": f"Bearer {token}"}

        payload = await _invoke_tool_call(
            client,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            idem_key="iga-e2e-funds-1",
            headers=headers,
        )
        assert "error" in payload, payload
        assert payload["error"]["message"] == "insufficient_funds"
        assert calls == []
        # The recorded use was compensated: nothing dispatched, nothing kept.
        assert oidc_iga._lifetime_uses == {}
        assert oidc_iga._window_calls == {}

        # Fund the wallet; the same max_uses=1 principal can now act.
        await _set_balance("100")

        payload = await _invoke_tool_call(
            client,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            idem_key="iga-e2e-funds-2",
            headers=headers,
        )
        assert "result" in payload, payload
        assert payload["result"]["receipt"]["outcome"] == "success"
        assert calls == [{"message": "hello"}]
        # The dispatched call keeps its committed use.
        assert sum(oidc_iga._lifetime_uses.values()) == 1
    finally:
        registry.unregister_local(E2E_TOOL)


async def test_ledger_contention_releases_iga_use(
    iga_config, clean_database, rsa_key, client, monkeypatch
):
    """A lost write conflict is the same kind of refusal as insufficient funds.

    The charge exhausted its retries, so nothing was charged and nothing was
    dispatched -- and unlike the funds denial, this one explicitly tells the
    caller to retry the same key. A ``max_uses=1`` principal whose use stayed
    consumed would find that advised retry refused ``iga_max_uses_exceeded``
    for a call the gateway never made.
    """
    from sqlalchemy.exc import OperationalError

    from app.services.billing_engine import BillingEngine

    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-contend-permit")
        wallet_id = setup["agent_wallet_id"]
        agent_headers = setup["agent_headers"]

        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_TOOL])
        iga_config(
            _okta_issuers(rsa_key),
            {"payments-ops": {"policy_id": policy_id, "max_uses": 1}},
        )
        token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
        headers = {**agent_headers, "Authorization": f"Bearer {token}"}

        async def _always_contended(self, **_kwargs):
            raise OperationalError(
                "UPDATE wallets ...", {}, Exception("database is locked")
            )

        monkeypatch.setattr(BillingEngine, "_charge_once", _always_contended)
        payload = await _invoke_tool_call(
            client,
            wallet_id=wallet_id,
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-contend-1",
            headers=headers,
        )
        monkeypatch.undo()

        assert "error" in payload, payload
        assert payload["error"]["message"] == "ledger_write_contended", payload
        assert calls == []
        # The recorded use was compensated: nothing charged, nothing dispatched.
        assert oidc_iga._lifetime_uses == {}
        assert oidc_iga._window_calls == {}

        # The retry this refusal advertises actually runs -- and on the SAME
        # key it refused, which is the key the caller was told to reuse. A fresh
        # key would prove the IGA release alone; reusing this one also proves
        # the record was abandoned rather than left in progress, since
        # idem.begin runs ahead of the enterprise gate and would refuse first.
        payload = await _invoke_tool_call(
            client,
            wallet_id=wallet_id,
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-contend-1",
            headers=headers,
        )
        assert "result" in payload, payload
        assert payload["result"]["receipt"]["outcome"] == "success"
        assert calls == [{"message": "hello"}]
        # And the call that did dispatch keeps its use.
        assert sum(oidc_iga._lifetime_uses.values()) == 1
    finally:
        registry.unregister_local(E2E_TOOL)


async def test_a_contended_remote_charge_keeps_the_use_it_cannot_prove_unspent(
    iga_config, clean_database, rsa_key, client, monkeypatch
):
    """When cleanup cannot prove the attempt effect-free, the use stays spent.

    ``abandon_effect_free_prepared_attempt`` raises a dispatch conflict when the
    prepared attempt carries a claim, a dispatch, a debit or a charge -- meaning
    another durable owner may still go on to send it. Handing the enterprise use
    back at that moment would return the ``max_uses`` and velocity budget for an
    invocation that then happens, letting it escape its cap entirely.

    So the ordering is the point: the release must sit after the abandon, not
    before it. This asserts the conservative half -- on the conflict path the
    counter is left alone, and the caller gets the retryable in-progress
    envelope while reconciliation owns the outcome.
    """
    from app.services.billing_engine import BillingEngine
    from app.services.mcp_dispatch_attempts import (
        DispatchClaimUnavailableError,
        McpDispatchAttemptService,
    )

    registry = _register_e2e_upstream_tool()
    try:
        setup = await _provision_for_e2e(
            client,
            idem_key="iga-e2e-conflict-permit",
            tool_name=E2E_UPSTREAM_TOOL,
        )
        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_UPSTREAM_TOOL])
        iga_config(
            _okta_issuers(rsa_key),
            {"payments-ops": {"policy_id": policy_id, "max_uses": 1}},
        )
        token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
        headers = {**setup["agent_headers"], "Authorization": f"Bearer {token}"}

        async def _always_contended(self, **_kwargs):
            raise OperationalError(
                "UPDATE wallets ...", {}, Exception("database is locked")
            )

        async def _conflicted_abandon(self, **_kwargs):
            raise DispatchClaimUnavailableError("dispatch_attempt_advanced")

        monkeypatch.setattr(BillingEngine, "_charge_once", _always_contended)
        monkeypatch.setattr(
            McpDispatchAttemptService,
            "abandon_effect_free_prepared_attempt",
            _conflicted_abandon,
        )
        payload = await _invoke_tool_call(
            client,
            wallet_id=setup["agent_wallet_id"],
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-conflict-1",
            headers=headers,
            tool_name=E2E_UPSTREAM_TOOL,
        )
        monkeypatch.undo()

        # Reconciliation owns the outcome from here, so the caller is told the
        # operation is still in flight rather than given a verdict.
        assert "error" in payload, payload
        assert payload["error"]["message"] == "idempotency_in_progress", payload
        # And the use is NOT handed back: another owner may still spend it.
        assert sum(oidc_iga._lifetime_uses.values()) == 1, oidc_iga._lifetime_uses
    finally:
        registry.unregister_local(E2E_UPSTREAM_TOOL)


async def test_remote_insufficient_funds_releases_the_use_before_the_fallible_budget_cleanup(
    iga_config, clean_database, rsa_key, client, monkeypatch
):
    """The use goes back as soon as the refusal is proven effect-free -- not
    after a later cleanup step that can fail on its own.

    On the remote path an insufficient-funds denial proves the attempt never
    dispatched by driving it terminal through ``complete_pre_dispatch_failure``.
    That is the whole proof the release needs. ``release_dispatch_budget_once``
    runs next, is independently fallible (retry exhaustion, a permit or attempt
    row gone missing), and its failure is answered by reconciliation. The
    enterprise counters are process-local: nothing downstream can repair them,
    so a use left spent here denies a ``max_uses=1`` principal until restart for
    a call that provably never ran.
    """
    from sqlmodel import select

    from app.db.models import McpDispatchAttemptModel
    from app.services.permits import PermitService, PermitWriteContendedError

    registry = _register_e2e_upstream_tool()
    try:
        setup = await _provision_for_e2e(
            client,
            idem_key="iga-e2e-remote-funds-permit",
            tool_name=E2E_UPSTREAM_TOOL,
        )
        wallet_id = setup["agent_wallet_id"]

        # The tool costs 2 credits; a balance of 1 cannot cover it.
        factory = get_session_factory()
        async with factory() as session:
            wallet = await session.get(WalletModel, wallet_id)
            assert wallet is not None
            wallet.balance = Decimal("1")
            session.add(wallet)
            await session.commit()

        bundle_wallet = await _make_wallet()
        policy_id = await _make_bundle(bundle_wallet, allowed_tools=[E2E_UPSTREAM_TOOL])
        iga_config(
            _okta_issuers(rsa_key),
            {"payments-ops": {"policy_id": policy_id, "max_uses": 1}},
        )
        token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
        headers = {**setup["agent_headers"], "Authorization": f"Bearer {token}"}

        async def _exhausted(self, attempt_id):
            raise PermitWriteContendedError()

        monkeypatch.setattr(PermitService, "release_dispatch_budget_once", _exhausted)
        payload = await _invoke_tool_call(
            client,
            wallet_id=wallet_id,
            permit_id=setup["permit_id"],
            idem_key="iga-e2e-remote-funds-1",
            headers=headers,
            tool_name=E2E_UPSTREAM_TOOL,
        )
        monkeypatch.undo()

        assert "error" in payload, payload
        # Classified, not buried. Nothing ran and nothing was charged, so the
        # contended cleanup earns the retryable code, and the unwind released
        # the governed record -- which is what makes the retry that reaches
        # the real insufficient_funds answer actually possible. Before
        # PermitWriteContendedError was classified on this surface it was
        # -32603 internal_error, indistinguishable from an unclassified fault.
        assert payload["error"]["code"] == -32005, payload
        assert payload["error"]["message"] == "permit_write_contended", payload
        # The proof happened: the attempt is terminal, and it was never sent.
        async with factory() as session:
            attempts = (
                (
                    await session.execute(
                        select(McpDispatchAttemptModel).where(
                            McpDispatchAttemptModel.wallet_id == wallet_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(attempts) == 1, attempts
        assert attempts[0].state == "returned_error", attempts[0].state
        assert attempts[0].dispatched_at is None
        # The budget cleanup genuinely failed -- reconciliation owns that --
        # and yet the use is back, because the release no longer waits on it.
        assert attempts[0].budget_released_at is None
        assert oidc_iga._lifetime_uses == {}, oidc_iga._lifetime_uses
        assert oidc_iga._window_calls == {}, oidc_iga._window_calls
    finally:
        registry.unregister_local(E2E_UPSTREAM_TOOL)


async def test_enterprise_shaped_bearer_still_401s_when_iga_disabled(
    clean_database, rsa_key, client
):
    """Regression guard for today's behavior: with IGA unconfigured, a bearer
    from an enterprise-shaped issuer is handled as an internal JWT and 401s
    even when a valid X-API-Key accompanies it."""
    calls: list[dict] = []
    registry = _register_e2e_tool(calls)
    try:
        assert not get_settings().IGA_TRUSTED_ISSUERS
        setup = await _provision_for_e2e(client, idem_key="iga-e2e-disabled-permit")

        token = _mint(rsa_key, extra={"groups": ["payments-ops"]})
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "iga-disabled-1",
                "method": "tools/call",
                "params": {
                    "name": E2E_TOOL,
                    "arguments": {"message": "hello"},
                    "mcpContext": {
                        "wallet_id": setup["agent_wallet_id"],
                        "permit_id": setup["permit_id"],
                        "idempotency_key": "iga-e2e-disabled-1",
                    },
                },
            },
            headers={
                **setup["agent_headers"],
                "Authorization": f"Bearer {token}",
            },
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error"] == "invalid_token"
        assert calls == []
    finally:
        registry.unregister_local(E2E_TOOL)
