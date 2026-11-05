"""The public dependency route must not start probes whose verdicts it discards.

With proof surfaces unmounted, ``GET /health/dependencies`` reports only the
wedge dependencies. Stripe, the LLM provider, the MQTT broker and Sentinel are
projected out of that payload, so an anonymous caller must not be able to make
the gateway call them. Calls are counted by patching the Stripe SDK class and
the HTTP client class, never a singleton instance.
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace

import httpx
import pytest
import stripe
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.health import (
    PUBLIC_DEPENDENCY_KEYS,
    build_public_dependency_report,
    gather_dependency_report,
)
from app.main import app

SENTINEL_ORIGIN = "http://127.0.0.1:9"
SENTINEL_HEALTH_URL = f"{SENTINEL_ORIGIN}/health"


@pytest.fixture
async def client(clean_database):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def probe_counts(monkeypatch):
    """Configure Stripe and Sentinel as reachable and count calls to each."""
    counts = {"stripe": 0, "sentinel": 0}

    def fake_retrieve(*_args, **_kwargs):
        counts["stripe"] += 1
        return SimpleNamespace(livemode=False)

    real_get = httpx.AsyncClient.get

    async def counting_get(self, url, *args, **kwargs):
        if str(url) == SENTINEL_HEALTH_URL:
            counts["sentinel"] += 1
            return httpx.Response(200, request=httpx.Request("GET", str(url)))
        return await real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(stripe.Balance, "retrieve", staticmethod(fake_retrieve))
    monkeypatch.setattr(httpx.AsyncClient, "get", counting_get)

    cfg = get_settings()
    names = (
        "STRIPE_SECRET_KEY",
        "SENTINEL_API_URL",
        "SENTINEL_API_KEY",
        "SIMULATION_MODE_HUMAN_APPROVAL",
        "ENABLE_PROOF_SURFACES",
    )
    previous = {name: getattr(cfg, name) for name in names}
    cfg.STRIPE_SECRET_KEY = f"sk_test_{secrets.token_urlsafe(24)}"
    cfg.SENTINEL_API_URL = SENTINEL_ORIGIN
    cfg.SENTINEL_API_KEY = secrets.token_urlsafe(24)
    cfg.SIMULATION_MODE_HUMAN_APPROVAL = False
    cfg.ENABLE_PROOF_SURFACES = False
    try:
        yield counts
    finally:
        for name, value in previous.items():
            setattr(cfg, name, value)


def _without_latency(payload):
    if isinstance(payload, dict):
        return {k: _without_latency(v) for k, v in payload.items() if k != "latency_ms"}
    return payload


@pytest.mark.anyio
async def test_anonymous_public_request_starts_no_stripe_or_sentinel_probe(
    client, probe_counts
):
    resp = await client.get("/health/dependencies")

    assert resp.status_code == 200
    assert set(resp.json()["dependencies"]) == set(PUBLIC_DEPENDENCY_KEYS)
    assert probe_counts == {"stripe": 0, "sentinel": 0}


@pytest.mark.anyio
async def test_public_body_is_what_the_full_report_projects_to(client, probe_counts):
    resp = await client.get("/health/dependencies")
    full = await gather_dependency_report()
    expected = build_public_dependency_report(full)

    assert _without_latency(resp.json()) == _without_latency(expected)


@pytest.mark.anyio
async def test_mounted_proof_surfaces_still_run_every_probe(client, probe_counts):
    get_settings().ENABLE_PROOF_SURFACES = True

    resp = await client.get("/health/dependencies")

    body = resp.json()
    assert {"mqtt", "stripe", "llm", "sentinel"} <= set(body["dependencies"])
    assert probe_counts == {"stripe": 1, "sentinel": 1}


@pytest.mark.anyio
async def test_only_limits_the_probes_started_and_the_entries_reported(probe_counts):
    report = await gather_dependency_report(only=frozenset({"postgres", "redis"}))

    assert list(report["dependencies"]) == ["postgres", "redis"]
    assert probe_counts == {"stripe": 0, "sentinel": 0}


@pytest.mark.anyio
async def test_gather_without_only_runs_every_probe(probe_counts):
    report = await gather_dependency_report()

    assert set(report["dependencies"]) == {
        "postgres",
        "redis",
        "mqtt",
        "stripe",
        "llm",
        "upstream_mcp",
        "signing_key",
        "sentinel",
    }
    assert probe_counts == {"stripe": 1, "sentinel": 1}
