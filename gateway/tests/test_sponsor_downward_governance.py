"""A sponsor governs its descendants' permits and keys, and nobody else gains.

Covers #281: list and revoke permits, list, revoke and emergency-revoke keys,
and read rotation logs, for a strict ancestor at any depth. Every denial below
is the same 403 an unrelated tenant already gets at an exact-wallet route.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db.database import get_session_factory
from app.main import app
from app.schemas.trust import PermitCreateRequest
from app.core.config import get_settings
from app.core.jwt import get_jwt_service
from app.services.agent_money import get_agent_money
from app.services.ownership import CallerIdentity
from app.services.permits import get_permit_service
from app.services.wallet_hierarchy import (
    wallet_is_or_descends_from,
    wallet_strictly_governs,
)
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _key(client: AsyncClient, wallet_id: str, name: str) -> dict:
    resp = await client.post(
        "/v1/api-keys",
        json={"wallet_id": wallet_id, "key_name": name, "expires_in_days": 30},
        headers=BOOTSTRAP_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return {"headers": {"X-API-Key": body["api_key"]}, "key_id": body["key_id"]}


async def _agent_under(client: AsyncClient, sponsor_id: str, name: str) -> dict:
    resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_id,
            "agent_id": name,
            "budget_credits": 500,
            "daily_limit": 200,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    wallet_id = resp.json()["wallet_id"]
    return {"wallet_id": wallet_id, **await _key(client, wallet_id, name)}


async def _permit(issuer: str, subject: str, key_id: str | None, tool: str):
    return await get_permit_service().create_permit(
        PermitCreateRequest(
            issuer_wallet_id=issuer,
            subject_wallet_id=subject,
            subject_key_id=key_id,
            allowed_tools=[tool],
            scopes=[f"tool:{tool}:invoke"],
            max_credits=Decimal("10"),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).replace(
                tzinfo=None
            ),
        )
    )


@pytest.fixture
async def world(client, clean_database):
    """Sponsor S -> agent A -> grandchild G, sibling B under S, unrelated U."""
    base = await provision_agent_wallet(client)
    sponsor_id = base["sponsor_wallet_id"]
    sponsor = {"wallet_id": sponsor_id, **await _key(client, sponsor_id, "sponsor")}
    agent = {
        "wallet_id": base["agent_wallet_id"],
        "headers": base["agent_headers"],
        "key_id": base["key_id"],
    }
    sibling = await _agent_under(client, sponsor_id, "sibling")
    # The child-wallet route is a dormant surface; the service is what it calls.
    child = await get_agent_money().create_child_wallet(
        parent_wallet_id=agent["wallet_id"],
        child_agent_id="grandchild",
        budget_credits=Decimal("50"),
        max_spend=Decimal("50"),
        caller=CallerIdentity.for_wallet(agent["wallet_id"]),
    )
    grandchild_id = child.wallet_id
    grandchild = {
        "wallet_id": grandchild_id,
        **await _key(client, grandchild_id, "grandchild"),
    }
    other = await provision_agent_wallet(client)
    unrelated = {
        "wallet_id": other["agent_wallet_id"],
        "headers": other["agent_headers"],
        "key_id": other["key_id"],
    }
    return {
        "sponsor": sponsor,
        "agent": agent,
        "sibling": sibling,
        "grandchild": grandchild,
        "unrelated": unrelated,
    }


async def _key_statuses(client: AsyncClient, wallet_id: str) -> set[str]:
    resp = await client.get(f"/v1/api-keys/{wallet_id}", headers=BOOTSTRAP_HEADERS)
    assert resp.status_code == 200, resp.text
    return {k["status"] for k in resp.json()["keys"]}


def _emergency(client, wallet_id, headers, create_new_key=False):
    return client.post(
        "/v1/api-keys/emergency-revoke",
        json={
            "wallet_id": wallet_id,
            "reason": "descendant compromised",
            "create_new_key": create_new_key,
        },
        headers=headers,
    )


async def _all_routes(client, w, caller: dict, target: dict, permit_id: str):
    """Status codes of every changed route, caller against target."""
    target_id = target["wallet_id"]
    headers = caller["headers"]
    return {
        "list_permits": (
            await client.get(f"/v1/permits?wallet_id={target_id}", headers=headers)
        ).status_code,
        "revoke_permit": (
            await client.post(f"/v1/permits/{permit_id}/revoke", headers=headers)
        ).status_code,
        "list_keys": (
            await client.get(f"/v1/api-keys/{target_id}", headers=headers)
        ).status_code,
        "rotation_logs": (
            await client.get(f"/v1/api-keys/{target_id}/logs", headers=headers)
        ).status_code,
        "revoke_key": (
            await client.delete(
                f"/v1/api-keys/{target_id}/{target['key_id']}", headers=headers
            )
        ).status_code,
        "emergency": (await _emergency(client, target_id, headers)).status_code,
    }


# --- positive: the sponsor governs downward ---------------------------------


@pytest.mark.anyio
async def test_sponsor_lists_and_revokes_a_self_issued_descendant_permit(
    client, world, caplog
):
    w = world
    permit = await _permit(
        w["agent"]["wallet_id"], w["agent"]["wallet_id"], w["agent"]["key_id"], "t1"
    )
    sponsor = w["sponsor"]["headers"]

    listed = await client.get(
        f"/v1/permits?wallet_id={w['agent']['wallet_id']}", headers=sponsor
    )
    assert listed.status_code == 200, listed.text
    assert permit.permit_id in {p["permit_id"] for p in listed.json()["permits"]}

    with caplog.at_level(logging.WARNING, logger="app.routers.permits"):
        revoked = await client.post(
            f"/v1/permits/{permit.permit_id}/revoke", headers=sponsor
        )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    # Same stored record an owner revoke leaves, with the actor in the log.
    stored = await get_permit_service().get_permit(permit.permit_id)
    assert stored.status == "revoked" and stored.revoked_at is not None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert w["sponsor"]["key_id"] in logged
    assert w["agent"]["wallet_id"] in logged
    assert w["sponsor"]["headers"]["X-API-Key"] not in logged


@pytest.mark.anyio
async def test_sponsor_lists_and_revokes_a_descendant_key(client, world, caplog):
    w = world
    sponsor = w["sponsor"]["headers"]
    agent_id = w["agent"]["wallet_id"]

    listed = await client.get(f"/v1/api-keys/{agent_id}", headers=sponsor)
    assert listed.status_code == 200, listed.text
    assert w["agent"]["key_id"] in {k["key_id"] for k in listed.json()["keys"]}
    assert w["agent"]["headers"]["X-API-Key"] not in listed.text

    logs = await client.get(f"/v1/api-keys/{agent_id}/logs", headers=sponsor)
    assert logs.status_code == 200, logs.text

    with caplog.at_level(logging.WARNING, logger="app.routers.api_keys"):
        revoked = await client.delete(
            f"/v1/api-keys/{agent_id}/{w['agent']['key_id']}", headers=sponsor
        )
    assert revoked.status_code == 204, revoked.text
    assert w["sponsor"]["key_id"] in "\n".join(r.getMessage() for r in caplog.records)

    # The revoked agent key no longer authenticates; the sponsor's still does.
    dead = await client.get(f"/v1/api-keys/{agent_id}", headers=w["agent"]["headers"])
    assert dead.status_code in (401, 403)
    assert dead.json()["detail"]["error"] != "wallet_access_denied"
    assert (
        await client.get(f"/v1/api-keys/{agent_id}", headers=sponsor)
    ).status_code == 200


@pytest.mark.anyio
async def test_sponsor_emergency_revokes_descendant_and_keeps_its_own_keys(
    client, world
):
    w = world
    sponsor = w["sponsor"]["headers"]
    agent_id = w["agent"]["wallet_id"]

    resp = await _emergency(client, agent_id, sponsor, create_new_key=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["wallet_id"] == agent_id
    assert w["agent"]["key_id"] in body["revoked_keys"]
    # An ancestor is not a key of the descendant, so no replacement secret is
    # minted for it (the bounding rule fails closed).
    assert body["new_key"] is None

    dead = await client.get(f"/v1/api-keys/{agent_id}", headers=w["agent"]["headers"])
    assert dead.status_code in (401, 403)
    own = await client.get(f"/v1/api-keys/{w['sponsor']['wallet_id']}", headers=sponsor)
    assert own.status_code == 200
    assert {k["status"] for k in own.json()["keys"]} == {"active"}


@pytest.mark.anyio
async def test_grandparent_governs_a_grandchild(client, world):
    w = world
    sponsor = w["sponsor"]["headers"]
    gid = w["grandchild"]["wallet_id"]
    permit = await _permit(gid, gid, w["grandchild"]["key_id"], "deep")

    routes = await _all_routes(
        client, w, w["sponsor"], w["grandchild"], permit.permit_id
    )
    assert routes == {
        "list_permits": 200,
        "revoke_permit": 200,
        "list_keys": 200,
        "rotation_logs": 200,
        "revoke_key": 204,
        "emergency": 200,
    }
    # The intermediate agent governs its own child the same way.
    again = await _emergency(client, gid, w["agent"]["headers"])
    assert again.status_code == 200
    assert again.json()["revoked_keys"] == []  # already revoked by the sponsor
    own = await client.get(f"/v1/api-keys/{w['sponsor']['wallet_id']}", headers=sponsor)
    assert own.status_code == 200
    assert {k["status"] for k in own.json()["keys"]} == {"active"}


@pytest.mark.anyio
async def test_equal_wallet_and_bootstrap_admin_keep_working(client, world):
    w = world
    agent = w["agent"]
    permit = await _permit(agent["wallet_id"], agent["wallet_id"], agent["key_id"], "e")
    own = await client.get(
        f"/v1/permits?wallet_id={agent['wallet_id']}", headers=agent["headers"]
    )
    assert own.status_code == 200
    assert (
        await client.get(f"/v1/api-keys/{agent['wallet_id']}", headers=agent["headers"])
    ).status_code == 200
    admin = await client.post(
        f"/v1/permits/{permit.permit_id}/revoke", headers=BOOTSTRAP_HEADERS
    )
    assert admin.status_code == 200
    assert (
        await client.get(
            f"/v1/api-keys/{agent['wallet_id']}/logs", headers=BOOTSTRAP_HEADERS
        )
    ).status_code == 200


# --- negative: nobody else gains anything ------------------------------------


@pytest.mark.anyio
async def test_descendant_gets_403_against_its_ancestor_on_every_route(client, world):
    w = world
    permit = await _permit(
        w["sponsor"]["wallet_id"], w["sponsor"]["wallet_id"], None, "up"
    )
    routes = await _all_routes(client, w, w["agent"], w["sponsor"], permit.permit_id)
    assert set(routes.values()) == {403}, routes
    # Two levels up as well.
    routes = await _all_routes(
        client, w, w["grandchild"], w["sponsor"], permit.permit_id
    )
    assert set(routes.values()) == {403}, routes
    stored = await get_permit_service().get_permit(permit.permit_id)
    assert stored.status == "active"
    sponsor_keys = await client.get(
        f"/v1/api-keys/{w['sponsor']['wallet_id']}", headers=w["sponsor"]["headers"]
    )
    assert sponsor_keys.status_code == 200


@pytest.mark.anyio
async def test_sibling_gets_403_on_every_route(client, world):
    w = world
    permit = await _permit(
        w["agent"]["wallet_id"], w["agent"]["wallet_id"], w["agent"]["key_id"], "sib"
    )
    routes = await _all_routes(client, w, w["sibling"], w["agent"], permit.permit_id)
    assert set(routes.values()) == {403}, routes
    stored = await get_permit_service().get_permit(permit.permit_id)
    assert stored.status == "active"
    # The denied emergency revoke and key revoke left the target's keys alone.
    assert await _key_statuses(client, w["agent"]["wallet_id"]) == {"active"}


@pytest.mark.anyio
async def test_unrelated_tenant_gets_403_and_cannot_tell_wallets_apart(client, world):
    w = world
    permit = await _permit(
        w["agent"]["wallet_id"], w["agent"]["wallet_id"], w["agent"]["key_id"], "un"
    )
    routes = await _all_routes(client, w, w["unrelated"], w["agent"], permit.permit_id)
    assert set(routes.values()) == {403}, routes
    assert await _key_statuses(client, w["agent"]["wallet_id"]) == {"active"}
    stored = await get_permit_service().get_permit(permit.permit_id)
    assert stored.status == "active"

    # A foreign wallet and a wallet that does not exist answer identically.
    headers = w["unrelated"]["headers"]
    foreign = await client.get(
        f"/v1/api-keys/{w['agent']['wallet_id']}", headers=headers
    )
    missing = await client.get("/v1/api-keys/wal-does-not-exist", headers=headers)
    assert foreign.status_code == missing.status_code == 403
    assert foreign.json()["detail"]["error"] == missing.json()["detail"]["error"]
    foreign = await _emergency(client, w["agent"]["wallet_id"], headers)
    missing = await _emergency(client, "wal-does-not-exist", headers)
    assert foreign.status_code == missing.status_code == 403


@pytest.mark.anyio
async def test_ancestor_cannot_reach_a_permit_it_does_not_govern_the_issuer_of(
    client, world
):
    """Authority follows the issuer wallet, not the subject wallet."""
    w = world
    # Issued by the sibling, encumbering the sibling only: the agent cannot
    # revoke it, and the sponsor can because the sponsor governs the sibling.
    permit = await _permit(
        w["sibling"]["wallet_id"], w["sibling"]["wallet_id"], None, "iss"
    )
    denied = await client.post(
        f"/v1/permits/{permit.permit_id}/revoke", headers=w["agent"]["headers"]
    )
    assert denied.status_code == 403
    ok = await client.post(
        f"/v1/permits/{permit.permit_id}/revoke", headers=w["sponsor"]["headers"]
    )
    assert ok.status_code == 200


@pytest.mark.anyio
async def test_revoked_sponsor_key_gains_nothing(client, world):
    w = world
    permit = await _permit(
        w["agent"]["wallet_id"], w["agent"]["wallet_id"], w["agent"]["key_id"], "rv"
    )
    revoked = await client.delete(
        f"/v1/api-keys/{w['sponsor']['wallet_id']}/{w['sponsor']['key_id']}",
        headers=BOOTSTRAP_HEADERS,
    )
    assert revoked.status_code == 204
    routes = await _all_routes(client, w, w["sponsor"], w["agent"], permit.permit_id)
    assert set(routes.values()) <= {401, 403}, routes
    assert (await get_permit_service().get_permit(permit.permit_id)).status == "active"


@pytest.mark.anyio
async def test_service_layer_refuses_ancestor_without_the_route_opt_in(client, world):
    """The service re-checks; a plain caller call never gains ancestry."""
    from app.services.permits import PermitAccessDeniedError

    w = world
    permit = await _permit(
        w["agent"]["wallet_id"], w["agent"]["wallet_id"], w["agent"]["key_id"], "svc"
    )
    sponsor_caller = CallerIdentity(
        wallet_id=w["sponsor"]["wallet_id"], key_id=w["sponsor"]["key_id"]
    )
    service = get_permit_service()
    with pytest.raises(PermitAccessDeniedError):
        await service.revoke_permit(permit.permit_id, caller=sponsor_caller)
    sibling_caller = CallerIdentity(wallet_id=w["sibling"]["wallet_id"])
    with pytest.raises(PermitAccessDeniedError):
        await service.revoke_permit(
            permit.permit_id, caller=sibling_caller, allow_ancestor=True
        )
    done = await service.revoke_permit(
        permit.permit_id, caller=sponsor_caller, allow_ancestor=True
    )
    assert done.status == "revoked"


# --- the hierarchy rule fails closed on a malformed chain --------------------


async def _set_parent(
    wallet_id: str, parent: str | None, *, foreign_keys: bool = True
) -> None:
    async with get_session_factory()() as session:
        if not foreign_keys and session.bind.dialect.name != "sqlite":
            pytest.skip("foreign key suspension is sqlite only")
        try:
            if not foreign_keys:
                # Simulates a row the foreign key would normally refuse.
                await session.execute(text("PRAGMA foreign_keys=OFF"))
            await session.execute(
                text("UPDATE wallets SET parent_wallet_id = :p WHERE wallet_id = :w"),
                {"p": parent, "w": wallet_id},
            )
            await session.commit()
        finally:
            if not foreign_keys:
                # The pragma is per connection and the pool reuses connections,
                # so put enforcement back before the connection is returned.
                await session.rollback()
                await session.execute(text("PRAGMA foreign_keys=ON"))
                await session.commit()


@pytest.mark.anyio
async def test_hierarchy_rule_fails_closed_on_cycles_and_missing_wallets(client, world):
    w = world
    s, a, g, b = (
        w["sponsor"]["wallet_id"],
        w["agent"]["wallet_id"],
        w["grandchild"]["wallet_id"],
        w["sibling"]["wallet_id"],
    )
    money = get_agent_money()
    assert await money.wallet_strictly_governs(s, a)
    assert await money.wallet_strictly_governs(s, g)
    assert await money.wallet_strictly_governs(a, g)
    assert not await money.wallet_strictly_governs(a, s)  # reverse
    assert not await money.wallet_strictly_governs(g, s)
    assert not await money.wallet_strictly_governs(b, a)  # sibling
    assert not await money.wallet_strictly_governs(a, a)  # equal is exact access
    assert not await money.wallet_strictly_governs(s, "wal-missing")
    assert not await money.wallet_strictly_governs("", a)

    # Tampered rows (not reachable through the API, which never re-parents).
    await _set_parent(a, g)  # cycle a -> g -> a
    assert not await money.wallet_strictly_governs(s, g)
    assert not await money.wallet_strictly_governs(s, a)
    routes = await client.get(f"/v1/api-keys/{g}", headers=w["sponsor"]["headers"])
    assert routes.status_code == 403
    # Permit creation keeps its own, unchanged rule for an equal wallet.
    assert await money.is_wallet_or_descendant(a, a)


@pytest.mark.anyio
async def test_hierarchy_walk_respects_the_depth_cap(client, world):
    w = world
    async with get_session_factory()() as session:
        assert not await wallet_strictly_governs(
            session,
            w["sponsor"]["wallet_id"],
            w["grandchild"]["wallet_id"],
            max_depth=2,
        )
        assert await wallet_strictly_governs(
            session,
            w["sponsor"]["wallet_id"],
            w["grandchild"]["wallet_id"],
            max_depth=3,
        )


# --- follow-up: issuer != subject, pinned cycles, depth cap ------------------


@pytest.mark.anyio
async def test_permit_revoke_follows_the_issuer_wallet_not_the_subject(client, world):
    """A permit issued by S to A (or G) is revocable by S only.

    The subject is encumbered, but revoke authority is the issuer's. A route
    that authorized on the subject wallet would let A or G revoke it.
    """
    w = world
    s_id = w["sponsor"]["wallet_id"]
    to_agent = await _permit(s_id, w["agent"]["wallet_id"], None, "s2a")
    to_grandchild = await _permit(s_id, w["grandchild"]["wallet_id"], None, "s2g")

    for permit in (to_agent, to_grandchild):
        for caller in ("agent", "grandchild", "sibling", "unrelated"):
            denied = await client.post(
                f"/v1/permits/{permit.permit_id}/revoke",
                headers=w[caller]["headers"],
            )
            assert denied.status_code == 403, (caller, denied.text)
        assert (await get_permit_service().get_permit(permit.permit_id)).status == (
            "active"
        )
        ok = await client.post(
            f"/v1/permits/{permit.permit_id}/revoke", headers=w["sponsor"]["headers"]
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "revoked"


@pytest.mark.anyio
async def test_sponsor_inside_a_cycle_and_a_missing_row_above_the_match(client, world):
    """The strict rule needs a sound chain; the creation rule stops at a match."""
    w = world
    s, a, g = (
        w["sponsor"]["wallet_id"],
        w["agent"]["wallet_id"],
        w["grandchild"]["wallet_id"],
    )
    money = get_agent_money()

    # Cycle that contains the sponsor: G -> A -> S -> G.
    await _set_parent(s, g)
    assert not await money.wallet_strictly_governs(s, g)
    assert not await money.wallet_strictly_governs(s, a)
    assert not await money.wallet_strictly_governs(a, g)
    # Permit creation keeps its first-match rule for the same chain.
    assert await money.is_wallet_or_descendant(g, s)
    assert await money.is_wallet_or_descendant(a, s)
    # The routes fail closed for the sponsor key on the cyclic chain.
    for path in (f"/v1/api-keys/{g}", f"/v1/api-keys/{g}/logs"):
        denied = await client.get(path, headers=w["sponsor"]["headers"])
        assert denied.status_code == 403, denied.text
    assert (await _emergency(client, g, w["sponsor"]["headers"])).status_code == 403
    assert await _key_statuses(client, g) == {"active"}

    # Missing row above the match: S points at a wallet row that is absent.
    await _set_parent(s, None)
    await _set_parent(s, "wal-missing-above", foreign_keys=False)
    assert not await money.wallet_strictly_governs(s, g)
    assert not await money.wallet_strictly_governs(s, a)
    assert await money.is_wallet_or_descendant(g, s)
    assert await money.is_wallet_or_descendant(a, s)
    denied = await client.get(f"/v1/api-keys/{g}", headers=w["sponsor"]["headers"])
    assert denied.status_code == 403


class _FakeWallet:
    def __init__(self, parent: str | None):
        self.parent_wallet_id = parent


class _ChainSession:
    """Just enough of AsyncSession for the walk: ``w0 -> w1 -> ... -> wN``."""

    def __init__(self, length: int):
        self._length = length

    async def get(self, _model, wallet_id: str):
        index = int(wallet_id[1:])
        if index > self._length:
            return None
        return _FakeWallet(f"w{index + 1}" if index < self._length else None)


@pytest.mark.anyio
async def test_default_depth_cap_is_pinned_for_both_rules():
    # Permit-creation rule, default cap 32: an ancestor 32 hops up counts, 33 not.
    session = _ChainSession(40)
    assert await wallet_is_or_descends_from(session, "w0", "w32")
    assert not await wallet_is_or_descends_from(session, "w0", "w33")

    # Strict rule, default cap 32. The chain must also reach its root inside the
    # cap, so it is one hop shallower than the creation rule: the nearest
    # ancestor at hop 1 and the root at hop 31 pass, a root at hop 32 does not.
    assert await wallet_strictly_governs(_ChainSession(31), "w31", "w0")
    assert await wallet_strictly_governs(_ChainSession(31), "w1", "w0")
    assert not await wallet_strictly_governs(_ChainSession(32), "w32", "w0")
    # A chain that goes on past the cap grants nothing, even to a near ancestor.
    assert not await wallet_strictly_governs(_ChainSession(40), "w1", "w0")


# --- follow-up: other caller kinds behave as at exact-wallet access ---------


@pytest.fixture
def signing_key(monkeypatch):
    # 32 raw bytes, strict base64; the same non-secret test material CI uses.
    monkeypatch.setenv(
        "TRUST_SIGNING_PRIVATE_KEY_B64", "dGVzdC1zaWduaW5nLWtleS1tYXRlcmlhbC0zMmJ5dGU="
    )
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _bearer(wallet: dict) -> dict:
    """The token /v1/auth/token mints for this key (that route is dormant)."""
    token = get_jwt_service().create_access_token(
        wallet_id=wallet["wallet_id"],
        key_id=wallet["key_id"],
        scopes=["billing:charge", "tool:invoke"],
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_jwt_from_the_sponsor_key_governs_down_but_never_mints(
    client, world, signing_key
):
    w = world
    sponsor_jwt = _bearer(w["sponsor"])
    agent_id = w["agent"]["wallet_id"]
    permit = await _permit(agent_id, agent_id, w["agent"]["key_id"], "jwt")

    # Same answers as the sponsor's own key gets.
    assert (
        await client.get(f"/v1/api-keys/{agent_id}", headers=sponsor_jwt)
    ).status_code == 200
    assert (
        await client.get(f"/v1/permits?wallet_id={agent_id}", headers=sponsor_jwt)
    ).status_code == 200
    # A JWT is still refused a replacement key on the ancestor path.
    minted = await _emergency(client, agent_id, sponsor_jwt, create_new_key=True)
    assert minted.status_code == 403, minted.text
    assert minted.json()["detail"]["error"] == "jwt_cannot_mint_api_key"
    assert await _key_statuses(client, agent_id) == {"active"}
    revoked = await client.post(
        f"/v1/permits/{permit.permit_id}/revoke", headers=sponsor_jwt
    )
    assert revoked.status_code == 200

    # A JWT minted from a descendant, sibling or unrelated key gets nothing up.
    sponsor_permit = await _permit(
        w["sponsor"]["wallet_id"], w["sponsor"]["wallet_id"], None, "jwt-up"
    )
    for name in ("agent", "grandchild", "sibling", "unrelated"):
        caller = {"headers": _bearer(w[name])}
        routes = await _all_routes(
            client, w, caller, w["sponsor"], sponsor_permit.permit_id
        )
        assert set(routes.values()) == {403}, (name, routes)
    assert await _key_statuses(client, w["sponsor"]["wallet_id"]) == {"active"}


@pytest.mark.anyio
async def test_enterprise_bearer_rider_changes_nothing(client, world, monkeypatch):
    """An enterprise bearer next to X-API-Key is attribution, not authority."""
    monkeypatch.setattr("app.core.auth.is_iga_issuer_token", lambda _token: True)
    w = world
    rider = {"Authorization": "Bearer enterprise-token-not-an-api-credential"}
    agent_id = w["agent"]["wallet_id"]

    as_sponsor = {**w["sponsor"]["headers"], **rider}
    plain = await client.get(
        f"/v1/api-keys/{agent_id}", headers=w["sponsor"]["headers"]
    )
    ridden = await client.get(f"/v1/api-keys/{agent_id}", headers=as_sponsor)
    assert plain.status_code == ridden.status_code == 200

    sponsor_permit = await _permit(
        w["sponsor"]["wallet_id"], w["sponsor"]["wallet_id"], None, "ent"
    )
    for name in ("agent", "sibling", "unrelated"):
        caller = {"headers": {**w[name]["headers"], **rider}}
        routes = await _all_routes(
            client, w, caller, w["sponsor"], sponsor_permit.permit_id
        )
        assert set(routes.values()) == {403}, (name, routes)
    # The bearer alone, with no API key, is still not a credential.
    alone = await client.get(f"/v1/api-keys/{agent_id}", headers=rider)
    assert alone.status_code in (401, 403)
    assert await _key_statuses(client, w["sponsor"]["wallet_id"]) == {"active"}


@pytest.fixture
def demo_enabled():
    from app.services.demo_tenant import _memory_counters

    names = (
        "ENVIRONMENT",
        "REDIS_URL",
        "ENABLE_DEMO_TENANT",
        "DEMO_ALLOWED_ORIGINS",
        "DEMO_ISSUE_PER_IP_PER_DAY",
        "DEMO_ISSUE_GLOBAL_PER_HOUR",
        "DEMO_ISSUE_GLOBAL_PER_DAY",
        "DEMO_MAX_LIVE_KEYS",
    )
    settings = get_settings()
    saved = {name: getattr(settings, name) for name in names}
    _memory_counters.clear()
    settings.ENVIRONMENT = "local"
    settings.REDIS_URL = ""
    settings.ENABLE_DEMO_TENANT = True
    settings.DEMO_ALLOWED_ORIGINS = "https://agentmiddleware.dev"
    settings.DEMO_ISSUE_PER_IP_PER_DAY = 10
    settings.DEMO_ISSUE_GLOBAL_PER_HOUR = 100
    settings.DEMO_ISSUE_GLOBAL_PER_DAY = 100
    settings.DEMO_MAX_LIVE_KEYS = 100
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
        _memory_counters.clear()


@pytest.mark.anyio
async def test_demo_key_gets_the_same_answers_at_a_descendant_as_at_its_own_wallet(
    client, world, demo_enabled
):
    """Demo confinement runs before the route, so ancestry adds no demo power."""
    w = world
    issued = await client.post("/v1/demo/keys", json={})
    assert issued.status_code == 201, issued.text
    demo_wallet = issued.json()["wallet_id"]
    demo_headers = {"X-API-Key": issued.json()["api_key"]}
    # Not reachable through the API; tamper so the demo wallet has a descendant.
    agent_id = w["agent"]["wallet_id"]
    await _set_parent(agent_id, demo_wallet)
    assert await get_agent_money().wallet_strictly_governs(demo_wallet, agent_id)

    # Credential routes are denied to a demo key at either wallet, identically.
    for target in (demo_wallet, agent_id):
        for method, path in (
            ("GET", f"/v1/api-keys/{target}"),
            ("GET", f"/v1/api-keys/{target}/logs"),
            ("DELETE", f"/v1/api-keys/{target}/key-x"),
        ):
            resp = await client.request(method, path, headers=demo_headers)
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["detail"]["error"] == "demo_key_route_forbidden"
        resp = await _emergency(client, target, demo_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "demo_key_route_forbidden"
    assert await _key_statuses(client, agent_id) == {"active"}

    # Permit routes stay open to a demo key as before; the descendant answer
    # equals the own-wallet answer (no extra demo power, no extra denial).
    own = await client.get(f"/v1/permits?wallet_id={demo_wallet}", headers=demo_headers)
    below = await client.get(f"/v1/permits?wallet_id={agent_id}", headers=demo_headers)
    assert own.status_code == 200, own.text
    assert below.status_code == 200, below.text
    # A descendant key reaching up at the demo wallet gets nothing.
    up = await client.get(
        f"/v1/permits?wallet_id={demo_wallet}", headers=w["agent"]["headers"]
    )
    assert up.status_code == 403


# --- follow-up #284: the permit-creation rule stops at the first match -------


class _CountingSession:
    """A parent map standing in for the wallets table; counts every lookup."""

    def __init__(self, parents: dict[str, str | None]):
        self._parents = parents
        self.gets = 0

    async def get(self, _model, wallet_id: str):
        self.gets += 1
        if wallet_id not in self._parents:
            return None
        return _FakeWallet(self._parents[wallet_id])


@pytest.mark.anyio
async def test_permit_creation_rule_stops_at_the_first_match():
    # Direct child: the first parent is the ancestor, one lookup.
    session = _CountingSession({"c": "p", "p": None})
    assert await wallet_is_or_descends_from(session, "c", "p")
    assert session.gets == 1

    # Ancestor at hop 1, a link above it points at a row that does not exist:
    # still True after the first lookup, while the strict rule is False.
    session = _CountingSession({"c": "p", "p": "ghost"})
    assert await wallet_is_or_descends_from(session, "c", "p")
    assert session.gets == 1
    strict = _CountingSession({"c": "p", "p": "ghost"})
    assert not await wallet_strictly_governs(strict, "p", "c")

    # A cycle above the match does not change the creation answer either.
    session = _CountingSession({"c": "p", "p": "q", "q": "p"})
    assert await wallet_is_or_descends_from(session, "c", "p")
    assert session.gets == 1
    assert not await wallet_strictly_governs(
        _CountingSession({"c": "p", "p": "q", "q": "p"}), "p", "c"
    )

    # Ancestor at hop 2: two lookups, not the whole chain.
    session = _CountingSession({"c": "m", "m": "p", "p": "r", "r": None})
    assert await wallet_is_or_descends_from(session, "c", "p")
    assert session.gets == 2

    # No match walks to the root and answers False; equal ids need no lookup.
    session = _CountingSession({"c": "p", "p": None})
    assert not await wallet_is_or_descends_from(session, "c", "x")
    assert session.gets == 2
    session = _CountingSession({"c": "p", "p": None})
    assert await wallet_is_or_descends_from(session, "c", "c")
    assert session.gets == 0
