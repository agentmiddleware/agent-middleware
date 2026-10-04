"""Cross-tenant isolation regression tests for routers that previously
trusted a client-supplied wallet/tenant id without an ownership check.

Covers confirmed IDOR / missing-auth findings:
  - telemetry_scope: pipelines are wallet-scoped and must not be readable,
    mutable, or enumerable across tenants.
  - planner/optimize: must authenticate and enforce wallet ownership before
    reading policy bundles or writing signed audit events.
  - sandbox/behavioral get/destroy/execute: must require auth + env ownership.
  - iot: devices belong to the registering wallet; another wallet must not
    read, message, subscribe to, deregister, or enumerate them.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.iot_bridge import DeviceRegistry
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _optimizer_body(wallet_id: str) -> dict:
    return {
        "state": {
            "wallet_id": wallet_id,
            "agent_id": "a1",
            "task_id": "t1",
            "request_id": "r1",
            "wallet_balance": 100,
            "daily_spend_used": 0,
            "daily_limit": 100,
            "rate_limit_headroom": 1.0,
            "service_health": {"svc1": "healthy"},
            "simulation_flags": {"svc1": False},
            "auth_scope": ["invoke"],
            "task_context": {"tier": "medium"},
            "remaining_budget": 20,
            "slo_window_seconds": 2,
        }
    }


# --------------------------------------------------------------------------
# telemetry-scope
# --------------------------------------------------------------------------


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_pipeline_not_readable_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    create = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={
            "tenant_id": a["agent_wallet_id"],
            "service_name": "svc",
            "git_repo_url": "https://github.com/a/secret",
            "webhook_url": "https://a.example/hook",
        },
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    pipeline_id = create.json()["pipeline_id"]

    # Wallet B may not read, ingest into, or trigger auto-PR on A's pipeline.
    assert (
        await client.get(
            f"/v1/telemetry-scope/pipelines/{pipeline_id}", headers=b["agent_headers"]
        )
    ).status_code == 403
    assert (
        await client.get(
            f"/v1/telemetry-scope/pipelines/{pipeline_id}/stats",
            headers=b["agent_headers"],
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/v1/telemetry-scope/pipelines/{pipeline_id}/events",
            json={"events": [{"latency_ms": 1}]},
            headers=b["agent_headers"],
        )
    ).status_code == 403

    # Owner still has access.
    assert (
        await client.get(
            f"/v1/telemetry-scope/pipelines/{pipeline_id}", headers=a["agent_headers"]
        )
    ).status_code == 200


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_list_pipelines_scoped_to_caller(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    await client.post(
        "/v1/telemetry-scope/pipelines",
        json={"tenant_id": a["agent_wallet_id"], "service_name": "svc-a"},
        headers=a["agent_headers"],
    )

    # B lists (even trying to spoof A's tenant_id via query) and sees nothing of A's.
    resp = await client.get(
        f"/v1/telemetry-scope/pipelines?tenant_id={a['agent_wallet_id']}",
        headers=b["agent_headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.proof
@pytest.mark.anyio
async def test_telemetry_create_pipeline_for_other_wallet_denied(
    client, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/telemetry-scope/pipelines",
        json={"tenant_id": a["agent_wallet_id"], "service_name": "svc"},
        headers=b["agent_headers"],
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# planner/optimize
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_planner_optimize_requires_auth(client, clean_database):
    resp = await client.post("/v1/planner/optimize", json=_optimizer_body("w-victim"))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_planner_optimize_rejects_cross_wallet(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    # B authenticates but names A's wallet in the request state.
    resp = await client.post(
        "/v1/planner/optimize",
        json=_optimizer_body(a["agent_wallet_id"]),
        headers=b["agent_headers"],
    )
    assert resp.status_code == 403

    # B optimizing for its own wallet works.
    ok = await client.post(
        "/v1/planner/optimize",
        json=_optimizer_body(b["agent_wallet_id"]),
        headers=b["agent_headers"],
    )
    assert ok.status_code == 200


# --------------------------------------------------------------------------
# sandbox/behavioral
# --------------------------------------------------------------------------


@pytest.mark.proof
@pytest.mark.anyio
async def test_sandbox_env_not_accessible_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)

    create = await client.post(
        "/v1/sandbox/behavioral/environments",
        json={
            "name": "a-env",
            "environment_type": "mcp_sandbox",
            "wallet_id": a["agent_wallet_id"],
        },
        headers=a["agent_headers"],
    )
    assert create.status_code == 201
    env_id = create.json()["env_id"]

    # Unauthenticated read is rejected outright.
    assert (
        await client.get(f"/v1/sandbox/behavioral/environments/{env_id}")
    ).status_code == 401

    # Wallet B cannot read or destroy A's environment.
    assert (
        await client.get(
            f"/v1/sandbox/behavioral/environments/{env_id}", headers=b["agent_headers"]
        )
    ).status_code == 403
    assert (
        await client.delete(
            f"/v1/sandbox/behavioral/environments/{env_id}", headers=b["agent_headers"]
        )
    ).status_code == 403

    # Owner can read.
    assert (
        await client.get(
            f"/v1/sandbox/behavioral/environments/{env_id}", headers=a["agent_headers"]
        )
    ).status_code == 200


# --------------------------------------------------------------------------
# iot
# --------------------------------------------------------------------------


def _iot_device(device_id: str, **extra) -> dict:
    return {
        "device_id": device_id,
        "protocol": "mqtt",
        "topic_acl": {
            f"device/{device_id}/telemetry": "read",
            f"device/{device_id}/command": "write",
        },
        **extra,
    }


def _iot_id(label: str) -> str:
    # iot_devices is not reset by clean_database; keep ids unique per test.
    return f"iot-{label}-{uuid.uuid4().hex[:12]}"


async def _iot_event_types(device_id: str) -> list[str]:
    events = await DeviceRegistry().recent_events(limit=100)
    return [e["event"] for e in events if e["device_id"] == device_id]


@pytest.mark.proof
@pytest.mark.anyio
async def test_iot_device_not_accessible_across_tenants(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    device_id = _iot_id("a")
    missing_id = _iot_id("missing")

    create = await client.post(
        "/v1/iot/devices", json=_iot_device(device_id), headers=a["agent_headers"]
    )
    assert create.status_code == 201
    assert a["agent_wallet_id"] not in create.text

    base = f"/v1/iot/devices/{device_id}"
    message = {"topic": f"device/{device_id}/command", "payload": {"cmd": "unlock"}}
    subscribe = {"topic": f"device/{device_id}/telemetry"}

    # Wallet B can neither read, message, subscribe to, nor deregister A's
    # device. Delete goes last so an unfixed router cannot mask the others.
    denied = [
        await client.get(base, headers=b["agent_headers"]),
        await client.post(f"{base}/messages", json=message, headers=b["agent_headers"]),
        await client.post(
            f"{base}/subscribe", params=subscribe, headers=b["agent_headers"]
        ),
        await client.delete(base, headers=b["agent_headers"]),
    ]
    missing = await client.get(
        f"/v1/iot/devices/{missing_id}", headers=b["agent_headers"]
    )
    assert missing.status_code == 404
    for resp in denied:
        # Same 404 a nonexistent device gets: no existence oracle, and the
        # owner's wallet id is never echoed.
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "device_not_found"
        assert resp.json()["detail"]["message"].replace(device_id, "<id>") == (
            missing.json()["detail"]["message"].replace(missing_id, "<id>")
        )
        assert a["agent_wallet_id"] not in resp.text

    # Nothing B attempted reached the device or its audit trail.
    assert await _iot_event_types(device_id) == ["register"]

    # A's device survived B's delete attempt and the owner keeps full access.
    assert (await client.get(base, headers=a["agent_headers"])).status_code == 200
    sent = await client.post(
        f"{base}/messages", json=message, headers=a["agent_headers"]
    )
    assert sent.status_code == 200
    subscribed = await client.post(
        f"{base}/subscribe", params=subscribe, headers=a["agent_headers"]
    )
    assert subscribed.status_code == 200

    # Bootstrap admins keep cross-tenant access.
    assert (await client.get(base, headers=BOOTSTRAP_HEADERS)).status_code == 200

    # The owner can deregister its own device.
    assert (await client.delete(base, headers=a["agent_headers"])).status_code == 204
    assert (await client.get(base, headers=a["agent_headers"])).status_code == 404


@pytest.mark.proof
@pytest.mark.anyio
async def test_iot_list_devices_scoped_to_caller(client, clean_database):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    a_dev, b_dev, admin_dev = _iot_id("a"), _iot_id("b"), _iot_id("admin")

    for device_id, headers in (
        (a_dev, a["agent_headers"]),
        (b_dev, b["agent_headers"]),
        (admin_dev, BOOTSTRAP_HEADERS),
    ):
        resp = await client.post(
            "/v1/iot/devices", json=_iot_device(device_id), headers=headers
        )
        assert resp.status_code == 201

    for caller, own, other in ((a, a_dev, b), (b, b_dev, a)):
        listing = await client.get(
            "/v1/iot/devices?per_page=200", headers=caller["agent_headers"]
        )
        assert listing.status_code == 200
        body = listing.json()
        # Only the caller's own device: never the other wallet's, never the
        # ownerless admin-registered one, and the total does not leak a count.
        assert [d["device_id"] for d in body["devices"]] == [own]
        assert body["total"] == 1
        assert other["agent_wallet_id"] not in listing.text

    # An ownerless (admin-registered) device is invisible to wallet keys...
    assert (
        await client.get(f"/v1/iot/devices/{admin_dev}", headers=a["agent_headers"])
    ).status_code == 404
    # ...while bootstrap admins still see every tenant's devices.
    admin_listing = await client.get(
        "/v1/iot/devices?per_page=200", headers=BOOTSTRAP_HEADERS
    )
    assert admin_listing.status_code == 200
    assert admin_listing.json()["total"] >= 3


@pytest.mark.proof
@pytest.mark.anyio
async def test_iot_registration_metadata_cannot_assign_ownership(
    client, clean_database
):
    a = await provision_agent_wallet(client)
    b = await provision_agent_wallet(client)
    device_id = _iot_id("spoof")

    # B registers a device while naming A as owner in client-controlled
    # metadata. Ownership comes from the authenticated key, never the body.
    spoof = {
        key: a["agent_wallet_id"]
        for key in (
            "owner_wallet_id",
            "_owner_wallet_id",
            "__owner_wallet_id",
            "wallet_id",
            "owner",
        )
    }
    create = await client.post(
        "/v1/iot/devices",
        json=_iot_device(device_id, metadata=spoof),
        headers=b["agent_headers"],
    )
    assert create.status_code == 201

    base = f"/v1/iot/devices/{device_id}"
    assert (await client.get(base, headers=a["agent_headers"])).status_code == 404
    assert (await client.delete(base, headers=a["agent_headers"])).status_code == 404
    listing = await client.get("/v1/iot/devices", headers=a["agent_headers"])
    assert device_id not in listing.text
    assert (await client.get(base, headers=b["agent_headers"])).status_code == 200


@pytest.mark.anyio
async def test_bootstrap_admin_key_matches_via_constant_time_compare(clean_database):
    """A configured bootstrap key is accepted as admin; a key that merely
    shares a prefix must not match.

    Locks in the constant-time comparison against VALID_API_KEYS -- the match
    result must be identical to the previous membership test."""
    from fastapi import HTTPException

    from app.core.auth import get_auth_context

    admin = await get_auth_context("test-key")
    assert admin.is_bootstrap_admin is True
    assert admin.source == "env"

    # A key that shares a prefix with the valid key must NOT match; it falls
    # through to the DB registry and, absent a record, is rejected.
    with pytest.raises(HTTPException) as excinfo:
        await get_auth_context("test-key-but-longer")
    assert excinfo.value.status_code == 403
