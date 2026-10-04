"""Public projection of /health/dependencies (wedge posture).

With proof surfaces unmounted — the required production posture — the
unauthenticated dependency report must cover only what the trust-plane wedge
runs on: postgres, redis, signing key, upstream MCP, version + commit SHA,
and the resolved environment posture. Per-service simulation modes and the
dependencies only frozen surfaces consume (mqtt, stripe, llm, sentinel) are
not a public billboard; they stay in the startup log and on instances that
mount proof surfaces.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.health import (
    PUBLIC_DEPENDENCY_KEYS,
    build_public_dependency_report,
    gather_dependency_report,
)
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_public_payload_reports_wedge_dependencies_only(client):
    assert get_settings().ENABLE_PROOF_SURFACES is False
    resp = await client.get("/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()

    assert set(body["dependencies"].keys()) == set(PUBLIC_DEPENDENCY_KEYS)
    assert "simulation_modes" not in body
    assert "enable_dogfood_tool" not in body
    assert "enable_dogfood_second_tool" not in body
    # Nothing in the payload names a simulated proof-surface service.
    rendered = resp.text
    for service in (
        "agent_comms",
        "content_factory",
        "human_approval",
        "iot_bridge",
        "media_engine",
        "oracle",
        "red_team",
        "rtaas",
        "telemetry_pm",
    ):
        assert service not in rendered

    # Version + SHA + posture stay, so the payload still proves what runs.
    assert body["version"]
    assert "commit_sha" in body
    assert body["enable_proof_surfaces"] is False
    assert "production_like" in body


@pytest.mark.anyio
async def test_full_payload_returns_when_proof_surfaces_enabled(client):
    cfg = get_settings()
    previous = cfg.ENABLE_PROOF_SURFACES
    cfg.ENABLE_PROOF_SURFACES = True
    try:
        resp = await client.get("/health/dependencies")
    finally:
        cfg.ENABLE_PROOF_SURFACES = previous
    body = resp.json()
    assert "simulation_modes" in body
    assert {"mqtt", "stripe", "llm", "sentinel"} <= set(body["dependencies"].keys())


@pytest.mark.anyio
@pytest.mark.parametrize("hidden_dependency", ["llm", "sentinel"])
async def test_projection_recomputes_verdict_from_public_deps(hidden_dependency):
    """A hidden proof-surface dependency can never flip the public status."""
    full = await gather_dependency_report()
    full["dependencies"][hidden_dependency] = {"status": "down", "error": "boom"}
    full["status"] = "degraded"
    full["unhealthy"] = [hidden_dependency]

    public = build_public_dependency_report(full)
    assert public["status"] == "healthy"
    assert public["unhealthy"] == []
    assert hidden_dependency not in public["dependencies"]


@pytest.mark.anyio
async def test_projection_keeps_wedge_failures_visible():
    full = await gather_dependency_report()
    full["dependencies"]["signing_key"] = {"status": "down", "error": "invalid"}

    public = build_public_dependency_report(full)
    assert public["status"] == "degraded"
    assert public["unhealthy"] == ["signing_key"]


@pytest.mark.anyio
async def test_projection_reports_runtime_degradation():
    full = await gather_dependency_report()
    full["runtime_degradation"] = {"degraded": True, "reasons": ["db_pool"]}

    public = build_public_dependency_report(full)
    assert public["status"] == "degraded"
    assert "runtime_degradation" in public["unhealthy"]


@pytest.mark.anyio
async def test_ready_and_dependencies_agree_on_mqtt(client):
    """/health/ready derives mqtt from the same sim-aware check.

    The old ready endpoint reported mqtt "up, configured: true" whenever a
    broker URL was set, while /health/dependencies reported not_used for the
    same deployment (iot_bridge simulated). They must never disagree again.
    """
    ready = (await client.get("/health/ready")).json()
    assert ready["checks"]["mqtt"]["status"] == "not_used"
    assert "iot_bridge" in ready["checks"]["mqtt"]["reason"]

    full = await gather_dependency_report()
    assert full["dependencies"]["mqtt"]["status"] == "not_used"
    assert ready["checks"]["mqtt"]["status"] == full["dependencies"]["mqtt"]["status"]
