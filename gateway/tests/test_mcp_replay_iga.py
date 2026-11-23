# ruff: noqa: F811  (pytest fixtures imported from test_iga_policy)
"""SEL-484 / Codex blocking 2: the IGA gate runs before any stored-result replay.

Reuses the signing-key, issuer and bundle fixtures from test_iga_policy; no
recorded data.
"""

from __future__ import annotations

import pytest

from app.core.auth import AuthContext
from app.routers.mcp import ToolPermissionDenied, _assert_replay_iga_access
from tests.test_iga_policy import (  # noqa: F401  (fixtures)
    TOOL,
    _make_bundle,
    _make_wallet,
    _mint,
    _okta_issuers,
    _reset_iga_counters,
    iga_config,
    rsa_key,
)


def _auth(wallet_id: str, token: str | None) -> AuthContext:
    return AuthContext(
        source="api_key",
        raw_key="",
        key_id="k",
        wallet_id=wallet_id,
        enterprise_bearer_token=token,
    )


async def test_replay_denied_for_principal_without_role(
    iga_config, clean_database, rsa_key
):
    wallet_id = await _make_wallet()
    policy_id = await _make_bundle(wallet_id, allowed_tools=[TOOL])
    iga_config(_okta_issuers(rsa_key), {"payments-ops": {"policy_id": policy_id}})

    allowed = _mint(rsa_key, extra={"groups": ["payments-ops"]})
    await _assert_replay_iga_access(auth=_auth(wallet_id, allowed), tool_name=TOOL)

    intruder = _mint(rsa_key, sub="intruder", extra={"groups": ["random-team"]})
    with pytest.raises(ToolPermissionDenied):
        await _assert_replay_iga_access(auth=_auth(wallet_id, intruder), tool_name=TOOL)


async def test_replay_denied_for_unverifiable_bearer(iga_config, rsa_key):
    iga_config(_okta_issuers(rsa_key), {})
    expired = _mint(rsa_key, exp_delta=-60)
    with pytest.raises(ToolPermissionDenied):
        await _assert_replay_iga_access(auth=_auth("w", expired), tool_name=TOOL)


async def test_replay_without_enterprise_bearer_is_unchanged():
    await _assert_replay_iga_access(auth=_auth("w", None), tool_name=TOOL)
