"""
Tests for app/core/health.py and the /health/dependencies endpoint.

Covers: default test-env report shape, per-check status codes,
simulation-mode gating (MQTT/LLM not probed when consumers simulated),
overall-status degradation, and timeout behavior.
"""

import asyncio
import base64
import json

import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import SecretStr

from app.core import health as health_module
from app.core.build_metadata import get_build_commit_sha
from app.core.config import get_settings
from app.core.health import (
    CHECK_TIMEOUT_SECONDS,
    _OK_STATUSES,
    _check_upstream_mcp,
    _run_check,
    gather_dependency_report,
)
from app.core.runtime_degradation import reset_runtime_degradation
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _restore_env():
    """Some tests mutate Settings; snapshot and restore."""
    settings = get_settings()
    fields = [
        "DATABASE_URL",
        "REDIS_URL",
        "MQTT_BROKER_URL",
        "STRIPE_SECRET_KEY",
        "LLM_API_KEY",
        "LLM_PROVIDER",
        "SIMULATION_MODE_IOT_BRIDGE",
        "SIMULATION_MODE_TELEMETRY_PM",
        "SIMULATION_MODE_HUMAN_APPROVAL",
        "SENTINEL_API_URL",
        "SENTINEL_API_KEY",
        "MCP_UPSTREAM_ENABLED",
        "MCP_UPSTREAM_PUBLIC_TOOL_ID",
        "MCP_UPSTREAM_BEARER_TOKEN",
        "TRUST_MODE_ENABLED",
        "TRUST_SIGNING_PRIVATE_KEY_B64",
        "BUILD_COMMIT_SHA",
    ]
    saved = {f: getattr(settings, f) for f in fields}
    reset_runtime_degradation()
    yield
    for f, v in saved.items():
        setattr(settings, f, v)
    reset_runtime_degradation()


@pytest.mark.anyio
async def test_upstream_health_exposes_payload_free_metrics(clean_database):
    settings = get_settings()
    settings.MCP_UPSTREAM_ENABLED = True
    settings.MCP_UPSTREAM_PUBLIC_TOOL_ID = "partner.lookup"
    settings.MCP_UPSTREAM_BEARER_TOKEN = SecretStr("never-report-this-token")
    registry = get_service_registry()
    registry.unregister_execution_backend("upstream_mcp")
    registry.register_upstream(
        service_id="partner.lookup",
        name="partner_lookup",
        description="Gateway-owned partner tool",
        category=ServiceCategory.PLATFORM_FEE,
        executor=object(),
        input_schema={"type": "object"},
        output_schema=None,
        credits_per_unit=1.0,
        upstream_tool_name="partner_lookup",
        upstream_origin="https://partner.example",
    )
    try:
        result = await _check_upstream_mcp()
    finally:
        registry.unregister_execution_backend("upstream_mcp")

    assert result["status"] == "up"
    assert "state_counts" in result["dispatch_metrics"]
    assert "reconciliation_backlog" in result["dispatch_metrics"]
    assert "latency_seconds_average" in result["call_metrics"]
    rendered = json.dumps(result)
    assert "never-report-this-token" not in rendered
    assert "result_json" not in rendered


# ---------------------------------------------------------------------------
# Default shape under test config
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_report_default_shape():
    """
    With the test env (sqlite DATABASE_URL, no Redis/Stripe/LLM, iot_bridge
    simulated), the report should be healthy: postgres up, the rest either
    not_configured or not_used.
    """
    report = await gather_dependency_report()

    assert report["status"] == "healthy"
    assert set(report["dependencies"].keys()) == {
        "postgres",
        "redis",
        "mqtt",
        "stripe",
        "llm",
        "upstream_mcp",
        "signing_key",
        "sentinel",
    }
    # Every check carries an error slot (None when healthy) and latency_ms.
    for res in report["dependencies"].values():
        assert "status" in res
        assert "latency_ms" in res
        assert "error" in res

    # In the default test env:
    assert report["dependencies"]["postgres"]["status"] == "up"
    assert report["dependencies"]["redis"]["status"] == "not_configured"
    assert report["dependencies"]["stripe"]["status"] == "not_configured"
    assert report["dependencies"]["llm"]["status"] == "not_configured"
    assert report["dependencies"]["upstream_mcp"]["status"] == "not_configured"
    assert report["dependencies"]["signing_key"]["status"] == "up"
    assert report["dependencies"]["signing_key"]["state"] == "ephemeral"

    # MQTT gated on iot_bridge simulation mode.
    assert report["dependencies"]["mqtt"]["status"] == "not_used"
    assert "iot_bridge" in report["dependencies"]["mqtt"].get("reason", "")

    # Sentinel gated on human_approval simulation mode.
    assert report["dependencies"]["sentinel"]["status"] == "not_used"
    assert "human_approval" in report["dependencies"]["sentinel"].get("reason", "")

    # Simulation modes are surfaced for operators.
    assert "simulation_modes" in report
    assert report["simulation_modes"]["iot_bridge"] is True


# ---------------------------------------------------------------------------
# Sentinel observability probe
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sentinel_health_probe_is_single_and_unauthenticated(monkeypatch):
    settings = get_settings()
    settings.SENTINEL_API_URL = "https://SENTINEL.example:443/"
    settings.SENTINEL_API_KEY = "never-send-this-key"
    import httpx

    calls = []
    real_client = httpx.AsyncClient

    async def handler(request):
        calls.append(
            (
                request.method,
                str(request.url),
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    result = await health_module._check_sentinel({"human_approval": False})

    assert result == {"status": "up"}
    assert calls == [("GET", "https://sentinel.example/health", None)]
    assert "never-send-this-key" not in json.dumps(calls)


@pytest.mark.anyio
async def test_simulated_sentinel_does_not_create_a_client(monkeypatch):
    settings = get_settings()
    settings.SENTINEL_API_URL = "https://sentinel.example"
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: pytest.fail(
            "a simulated approval dependency must not create a client"
        ),
    )

    result = await health_module._check_sentinel({"human_approval": True})

    assert result == {
        "status": "not_used",
        "reason": "human_approval in simulation mode",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sentinel_url",
    [
        "https://user:secret@sentinel.example",
        "https://sentinel.example?tenant=private",
        "https://sentinel.example#private",
        "https://sentinel.example/api",
        "http://169.254.169.254/latest/meta-data",
        "ftp://sentinel.example",
        "https://sentinel.example:",
        "https://sentinel.example:0",
        "https://[v1.sentinel.example]",
        " https://sentinel.example",
        "https://sentinel.example/\n",
    ],
    ids=[
        "credentials",
        "query",
        "fragment",
        "path",
        "remote_cleartext",
        "scheme",
        "empty_port",
        "zero_port",
        "ipvfuture",
        "leading_whitespace",
        "control_character",
    ],
)
async def test_invalid_sentinel_health_origin_fails_without_network(
    monkeypatch,
    sentinel_url,
):
    settings = get_settings()
    settings.SENTINEL_API_URL = sentinel_url
    settings.SENTINEL_API_KEY = "sk_test_safe"
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: pytest.fail(
            "an invalid Sentinel origin must not create a client"
        ),
    )

    result = await health_module._check_sentinel({"human_approval": False})

    assert result == {
        "status": "down",
        "reason": "human_approval_unavailable",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sentinel_url", "expected_health_url"),
    [
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/health"),
        ("http://[::1]:8000", "http://[::1]:8000/health"),
    ],
    ids=["ipv4_loopback", "ipv6_loopback"],
)
async def test_loopback_sentinel_health_origin_is_allowed(
    monkeypatch,
    sentinel_url,
    expected_health_url,
):
    settings = get_settings()
    settings.SENTINEL_API_URL = sentinel_url
    settings.SENTINEL_API_KEY = "sk_test_safe"
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            calls.append(url)
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    result = await health_module._check_sentinel({"human_approval": False})

    assert result == {"status": "up"}
    assert calls == [expected_health_url]


@pytest.mark.anyio
async def test_sentinel_health_failure_is_sanitized_in_full_report(monkeypatch):
    settings = get_settings()
    private_url = "https://private-sentinel.example"
    private_key = "sentinel-key-that-must-not-leak"
    settings.SIMULATION_MODE_HUMAN_APPROVAL = False
    settings.SENTINEL_API_URL = private_url
    settings.SENTINEL_API_KEY = private_key

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            raise RuntimeError(f"cannot reach {private_url} with {private_key}")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    report = await gather_dependency_report()
    sentinel = report["dependencies"]["sentinel"]
    rendered = json.dumps(report)

    assert sentinel["status"] == "down"
    assert sentinel["reason"] == "human_approval_unavailable"
    assert sentinel["error"] is None
    assert private_url not in rendered
    assert private_key not in rendered


# ---------------------------------------------------------------------------
# _run_check utility
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_check_captures_error(caplog):
    async def broken():
        raise ValueError("boom")

    with caplog.at_level("WARNING", logger=health_module.logger.name):
        result = await _run_check("t", broken)
    assert result["status"] == "down"
    # Only the exception class reaches the (unauthenticated) report; the
    # message text stays in the operator log.
    assert result["error"] == "ValueError"
    assert "boom" not in json.dumps(result)
    assert result["latency_ms"] >= 0
    assert "boom" in caplog.text


# Driver messages a real outage produces: they name internal hosts, ports, and
# database roles that an anonymous caller must not learn.
_INTERNAL_REDIS_ERROR = "Error 111 connecting to redis.internal.example:6379"
_INTERNAL_POSTGRES_ERROR = (
    'connection to server at "10.0.4.17", port 5432 failed: FATAL: '
    'password authentication failed for user "gateway_owner"'
)


def _break_redis_and_postgres(monkeypatch):
    async def redis_down():
        raise ConnectionError(_INTERNAL_REDIS_ERROR)

    async def postgres_down():
        raise OSError(_INTERNAL_POSTGRES_ERROR)

    monkeypatch.setattr(health_module, "_check_redis", redis_down)
    monkeypatch.setattr(health_module, "_check_postgres", postgres_down)


@pytest.mark.anyio
async def test_public_dependency_report_does_not_echo_exception_text(
    client, monkeypatch, caplog
):
    """The unauthenticated wedge projection reports status + exception class
    only; driver text (internal hostnames, IPs, DB roles) is logged, not served."""
    assert get_settings().ENABLE_PROOF_SURFACES is False
    _break_redis_and_postgres(monkeypatch)

    with caplog.at_level("WARNING", logger=health_module.logger.name):
        resp = await client.get("/health/dependencies")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert {"postgres", "redis"} <= set(body["unhealthy"])
    assert body["dependencies"]["redis"]["status"] == "down"
    assert body["dependencies"]["redis"]["error"] == "ConnectionError"
    assert body["dependencies"]["postgres"]["status"] == "down"
    assert body["dependencies"]["postgres"]["error"] == "OSError"
    for leaked in ("internal.example", "6379", "10.0.4.17", "gateway_owner"):
        assert leaked not in resp.text
    # Operators still get the full driver message server-side.
    assert "redis.internal.example:6379" in caplog.text
    assert "gateway_owner" in caplog.text


@pytest.mark.anyio
async def test_full_dependency_report_does_not_echo_exception_text(client, monkeypatch):
    """With proof surfaces mounted the full report is served instead, and it is
    just as unauthenticated — it must not carry the driver text either."""
    monkeypatch.setattr(get_settings(), "ENABLE_PROOF_SURFACES", True)
    _break_redis_and_postgres(monkeypatch)

    resp = await client.get("/health/dependencies")

    assert resp.status_code == 200
    body = resp.json()
    assert "simulation_modes" in body
    assert body["dependencies"]["redis"]["error"] == "ConnectionError"
    assert body["dependencies"]["postgres"]["error"] == "OSError"
    for leaked in ("internal.example", "6379", "10.0.4.17", "gateway_owner"):
        assert leaked not in resp.text


@pytest.mark.anyio
async def test_run_check_enforces_timeout():
    async def slow():
        await asyncio.sleep(CHECK_TIMEOUT_SECONDS + 1)
        return {"status": "up"}

    result = await _run_check("t", slow)
    assert result["status"] == "down"
    assert "timeout" in result["error"]


@pytest.mark.anyio
async def test_run_check_ok_fills_latency_and_nulls_error():
    async def ok():
        return {"status": "up"}

    result = await _run_check("t", ok)
    assert result["status"] == "up"
    assert result["error"] is None
    assert isinstance(result["latency_ms"], (int, float))


# ---------------------------------------------------------------------------
# Simulation-mode gating
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mqtt_probed_when_iot_bridge_real(monkeypatch):
    """Turning iot_bridge off forces MQTT to actually try to connect."""
    settings = get_settings()
    settings.SIMULATION_MODE_IOT_BRIDGE = False

    async def fake_mqtt(_sim_modes):
        return {"status": "up", "host": "localhost", "port": 1883}

    monkeypatch.setattr(health_module, "_check_mqtt", fake_mqtt)

    report = await gather_dependency_report()
    assert report["dependencies"]["mqtt"]["status"] == "up"


@pytest.mark.anyio
async def test_llm_not_used_when_telemetry_pm_simulated():
    """LLM probe skipped if its primary consumer is simulated — even when
    a key is configured — to avoid needlessly burning API quota."""
    settings = get_settings()
    settings.LLM_API_KEY = "sk-fake-but-set"
    settings.LLM_PROVIDER = "openai"
    # telemetry_pm already defaults to simulation=True

    report = await gather_dependency_report()
    assert report["dependencies"]["llm"]["status"] == "not_used"
    assert report["dependencies"]["llm"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# Overall verdict
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_overall_degrades_on_single_down_dep(monkeypatch):
    async def failing_postgres():
        return {"status": "down", "error": "connection refused"}

    monkeypatch.setattr(health_module, "_check_postgres", failing_postgres)

    report = await gather_dependency_report()
    assert report["status"] == "degraded"
    assert "postgres" in report["unhealthy"]


@pytest.mark.anyio
async def test_not_configured_and_not_used_do_not_degrade_status():
    """Verify the _OK_STATUSES contract — these keep overall healthy."""
    for status in _OK_STATUSES:
        assert status in {"up", "not_configured", "not_used"}


@pytest.mark.anyio
async def test_valid_signing_key_is_reported_as_loaded():
    settings = get_settings()
    settings.TRUST_MODE_ENABLED = True
    settings.TRUST_SIGNING_PRIVATE_KEY_B64 = base64.b64encode(bytes(range(32))).decode()

    report = await gather_dependency_report()

    assert report["dependencies"]["signing_key"]["status"] == "up"
    assert report["dependencies"]["signing_key"]["state"] == "loaded"
    assert "signing_key" not in report["unhealthy"]


@pytest.mark.anyio
async def test_invalid_signing_key_degrades_without_leaking_configured_value(client):
    settings = get_settings()
    secret_sentinel = f"{base64.b64encode(bytes(range(32))).decode()}!DO_NOT_LEAK"
    settings.TRUST_MODE_ENABLED = True
    settings.TRUST_SIGNING_PRIVATE_KEY_B64 = secret_sentinel

    resp = await client.get("/health/dependencies")
    report = resp.json()
    signing_key = report["dependencies"]["signing_key"]

    assert resp.status_code == 200
    assert report["status"] == "degraded"
    assert "signing_key" in report["unhealthy"]
    assert signing_key["status"] == "down"
    assert signing_key["state"] == "invalid"
    assert signing_key["error"] == "invalid_trust_signing_private_key"
    assert secret_sentinel not in json.dumps(report)
    assert secret_sentinel not in resp.text


@pytest.mark.anyio
async def test_missing_signing_key_degrades_when_trust_mode_is_enabled():
    settings = get_settings()
    settings.TRUST_MODE_ENABLED = True
    settings.TRUST_SIGNING_PRIVATE_KEY_B64 = ""

    report = await gather_dependency_report()
    signing_key = report["dependencies"]["signing_key"]

    assert report["status"] == "degraded"
    assert "signing_key" in report["unhealthy"]
    assert signing_key["status"] == "down"
    assert signing_key["state"] == "invalid"
    assert signing_key["error"] == "trust_signing_private_key_required"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_dependencies_endpoint_returns_report(client):
    """The endpoint serves the public (wedge) projection when proof surfaces
    are unmounted — the suite default and the required production posture.

    Full-report shape and the proof-surface branch are covered by
    test_report_default_shape above and test_health_public_projection.py.
    """
    resp = await client.get("/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert "dependencies" in body
    assert set(body["dependencies"].keys()) == {
        "postgres",
        "redis",
        "upstream_mcp",
        "signing_key",
    }
    assert "simulation_modes" not in body
    assert body["version"] == "1.3.0"
    assert "commit_sha" in body
    assert body["metric_scopes"] == {
        "upstream_mcp.call_metrics": {
            "scope": "process_local",
            "durable": False,
            "reset_on": "process_restart",
            "description": "In-memory counters for this API process only.",
        },
        "upstream_mcp.dispatch_metrics": {
            "scope": "durable",
            "durable": True,
            "source": "mcp_dispatch_attempts",
            "description": (
                "Dispatch history summarized from the durable state backend."
            ),
        },
    }
    assert "unhealthy" in body


@pytest.mark.anyio
async def test_health_surfaces_report_validated_build_commit(client, monkeypatch):
    """Health reports Railway git commit env (not BUILD_COMMIT_SHA env)."""
    test_sha = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", test_sha)
    get_settings.cache_clear()

    liveness = await client.get("/health")
    dependencies = await client.get("/health/dependencies")

    assert liveness.status_code == 200
    assert dependencies.status_code == 200
    assert liveness.json()["commit_sha"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert dependencies.json()["commit_sha"] == (
        "abcdef0123456789abcdef0123456789abcdef01"
    )

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_health_does_not_echo_invalid_build_metadata(client):
    settings = get_settings()
    invalid = "not-a-git-sha; do-not-reflect"
    settings.BUILD_COMMIT_SHA = invalid

    liveness = await client.get("/health")
    dependencies = await client.get("/health/dependencies")

    assert liveness.json()["commit_sha"] is None
    assert dependencies.json()["commit_sha"] is None
    assert invalid not in liveness.text
    assert invalid not in dependencies.text


def test_build_commit_falls_back_to_railway_deployment_metadata(monkeypatch):
    settings = get_settings()
    settings.BUILD_COMMIT_SHA = ""
    monkeypatch.setenv(
        "RAILWAY_GIT_COMMIT_SHA",
        "0123456789ABCDEF0123456789ABCDEF01234567",
    )

    assert get_build_commit_sha() == "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.anyio
async def test_health_dependencies_is_public_no_auth_required(client):
    """Health endpoints must work without the X-API-Key header — monitors
    like Kubernetes liveness probes don't carry authentication."""
    resp = await client.get("/health/dependencies")
    assert resp.status_code == 200
