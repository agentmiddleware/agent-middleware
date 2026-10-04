"""
Dependency health probes for /health/dependencies.

Each check returns a structured result so operators can tell at a glance
whether a specific external dependency is reachable. Runs in parallel
with a short per-check timeout — health endpoints must not hang on a
slow dependency.

Checks that are gated on simulation_mode return ``status="not_used"`` to
distinguish \"intentionally bypassed\" from \"broken\". Unconfigured deps
return ``status="not_configured"``. Neither counts as unhealthy.

See issue #27.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .build_metadata import get_build_commit_sha, get_build_provenance
from .config import get_settings
from .runtime_mode import get_simulation_modes
from .runtime_degradation import get_runtime_degradation
from .sentinel_target import (
    SentinelTargetError,
    sentinel_api_key_is_valid,
    sentinel_health_url,
)
from .trust_mode import is_production_like_environment
from ..services.signing_keys import (
    SigningKeyError,
    validate_signing_key_configuration,
)

logger = logging.getLogger(__name__)


CHECK_TIMEOUT_SECONDS: float = 2.0

# Statuses that do not degrade the overall health verdict.
_OK_STATUSES = {"up", "not_configured", "not_used"}

# Metrics exposed by the upstream MCP health check do not all share the same
# durability boundary. Keep this metadata present even when no upstream tool is
# configured so monitoring clients never have to infer semantics from values.
_METRIC_SCOPES = {
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
        "description": "Dispatch history summarized from the durable state backend.",
    },
}


async def _run_check(
    name: str,
    check: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run a dependency check, capturing latency and errors uniformly."""
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        result = {
            "status": "down",
            "error": f"timeout after {CHECK_TIMEOUT_SECONDS}s",
        }
    except Exception as exc:
        # Driver messages name internal hosts, ports, and database roles, and
        # /health/dependencies is unauthenticated. The report carries only the
        # exception class; the full text goes to the operator log.
        logger.warning("dependency check '%s' raised", name, exc_info=True)
        result = {"status": "down", "error": type(exc).__name__}

    result.setdefault("error", None)
    result["latency_ms"] = round((time.monotonic() - start) * 1000, 2)
    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


async def _check_postgres() -> dict[str, Any]:
    settings = get_settings()
    if not settings.DATABASE_URL:
        return {"status": "not_configured"}

    from ..db.database import get_engine

    engine = get_engine()
    if engine is None:
        return {"status": "not_configured"}

    async with engine.connect() as conn:
        from sqlalchemy import text

        await conn.execute(text("SELECT 1"))
    return {"status": "up"}


async def _check_redis() -> dict[str, Any]:
    settings = get_settings()
    redis_url = (settings.REDIS_URL or "").strip()
    if not redis_url:
        return {"status": "not_configured"}

    import redis.asyncio as redis

    client = redis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
        return {"status": "up"}
    finally:
        await client.aclose()


async def _check_mqtt(simulation_modes: dict[str, bool]) -> dict[str, Any]:
    # iot_bridge is the sole MQTT consumer. If it's in sim mode the broker
    # isn't actually touched — don't probe and don't fail the health check
    # just because a broker isn't up.
    if simulation_modes.get("iot_bridge", True):
        return {"status": "not_used", "reason": "iot_bridge in simulation mode"}

    settings = get_settings()
    broker_url = settings.MQTT_BROKER_URL
    if not broker_url:
        return {"status": "not_configured"}

    # Parse mqtt://host:port
    from urllib.parse import urlparse

    parsed = urlparse(broker_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 1883

    import aiomqtt

    async with aiomqtt.Client(hostname=host, port=port, timeout=CHECK_TIMEOUT_SECONDS):
        return {"status": "up", "host": host, "port": port}


async def _check_stripe() -> dict[str, Any]:
    settings = get_settings()
    if not settings.STRIPE_SECRET_KEY:
        return {"status": "not_configured"}

    import stripe

    # Stripe SDK is synchronous; run in the default executor so we don't
    # block the event loop.
    def _retrieve():
        stripe.api_key = settings.STRIPE_SECRET_KEY
        return stripe.Balance.retrieve()

    loop = asyncio.get_running_loop()
    balance = await loop.run_in_executor(None, _retrieve)
    mode = "live" if settings.STRIPE_SECRET_KEY.startswith("sk_live_") else "test"
    return {
        "status": "up",
        "mode": mode,
        "livemode": getattr(balance, "livemode", None),
    }


async def _check_llm(simulation_modes: dict[str, bool]) -> dict[str, Any]:
    # LLM is consumed by telemetry_pm (auto-PR) and the ai router. If
    # telemetry_pm is simulated and there's no key, don't probe.
    settings = get_settings()
    provider = settings.LLM_PROVIDER.lower().strip()

    if not settings.LLM_API_KEY and provider != "ollama":
        return {"status": "not_configured", "provider": provider}

    if simulation_modes.get("telemetry_pm", True):
        # Consumers are simulated — skip the probe to avoid needless API calls.
        return {
            "status": "not_used",
            "reason": "telemetry_pm in simulation mode",
            "provider": provider,
        }

    # Provider-specific lightweight probe.
    if provider in ("openai", "azure"):
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL or None,
        )
        models = await client.models.list()
        return {
            "status": "up",
            "provider": provider,
            "models_available": len(models.data),
        }

    if provider == "ollama":
        import httpx

        base = settings.OLLAMA_BASE_URL.rstrip("/")
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as http:
            r = await http.get(f"{base}/api/tags")
            r.raise_for_status()
            return {"status": "up", "provider": provider}

    # Providers we know about but don't probe yet.
    return {
        "status": "not_probed",
        "provider": provider,
        "reason": "no probe implemented for this provider",
    }


async def _check_upstream_mcp() -> dict[str, Any]:
    """Report the fail-closed startup registration without exposing secrets."""
    settings = get_settings()
    if not settings.MCP_UPSTREAM_ENABLED:
        return {"status": "not_configured", "enabled": False}

    from ..services.service_registry import get_service_registry
    from ..services.mcp_dispatch_attempts import (
        dispatch_reconciliation_idle_seconds,
        get_mcp_dispatch_attempt_service,
    )
    from ..services.upstream_mcp import get_upstream_mcp_metrics_snapshot

    registry = get_service_registry()
    service = registry.get_local(settings.MCP_UPSTREAM_PUBLIC_TOOL_ID)
    executor = registry.get_executor(settings.MCP_UPSTREAM_PUBLIC_TOOL_ID)
    if (
        not service
        or service.get("execution_backend") != "upstream_mcp"
        or executor is None
    ):
        return {
            "status": "down",
            "enabled": True,
            "public_tool_id": settings.MCP_UPSTREAM_PUBLIC_TOOL_ID,
            "error": "configured upstream tool is not registered",
        }
    dispatch_metrics = await get_mcp_dispatch_attempt_service().summarize(
        idle_seconds=dispatch_reconciliation_idle_seconds(
            connect_timeout_seconds=settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            call_timeout_seconds=settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS,
        )
    )
    returned_errors = dispatch_metrics.state_counts.get("returned_error", 0)
    rejected = dispatch_metrics.state_counts.get("response_rejected", 0)
    uncertainty_count = dispatch_metrics.state_counts.get("delivery_uncertain", 0)
    return {
        "status": "up",
        "enabled": True,
        "public_tool_id": service.get("service_id"),
        "upstream_tool_name": service.get("upstream_tool_name"),
        "upstream_origin": service.get("upstream_origin"),
        "call_metrics": get_upstream_mcp_metrics_snapshot(),
        "dispatch_metrics": {
            "state_counts": dispatch_metrics.state_counts,
            "failures": returned_errors + rejected,
            "uncertainty_count": uncertainty_count,
            "stale_active": dispatch_metrics.stale_active,
            "unfinalized_terminal": dispatch_metrics.unfinalized_terminal,
            "terminal_idempotency_incomplete": (
                dispatch_metrics.terminal_idempotency_incomplete
            ),
            "reconciliation_backlog": dispatch_metrics.reconciliation_backlog,
        },
    }


async def _check_signing_key() -> dict[str, Any]:
    """Report signing readiness without signing, persisting, or leaking keys."""

    try:
        state = validate_signing_key_configuration()
    except SigningKeyError as exc:
        error = str(exc)
        if error not in {
            "trust_signing_private_key_required",
            "invalid_trust_signing_private_key",
        }:
            error = "signing_key_unavailable"
        return {"status": "down", "state": "invalid", "error": error}
    except Exception:
        logger.debug("signing key configuration check raised", exc_info=True)
        return {
            "status": "down",
            "state": "invalid",
            "error": "signing_key_unavailable",
        }

    if state == "ephemeral":
        return {
            "status": "up",
            "state": "ephemeral",
            "reason": "trust mode disabled; process-ephemeral signing key",
        }
    return {"status": "up", "state": "loaded"}


async def check_database_readiness() -> dict[str, Any]:
    """Probe the required ORM database with the shared timeout/sanitization."""
    result = await _run_check("database", _check_postgres)
    # A missing engine/configuration cannot serve the trust-plane ORM routes.
    if result["status"] != "up":
        result["status"] = "down"
    result["configured"] = bool(get_settings().DATABASE_URL)
    return result


async def check_mqtt_readiness() -> dict[str, Any]:
    """Sim-aware mqtt entry for ``/health/ready``.

    Shares ``_check_mqtt`` with ``/health/dependencies`` so the two endpoints
    can never disagree: when ``iot_bridge`` is in simulation mode both report
    ``not_used`` instead of ready claiming ``up`` for a broker nothing
    touches.
    """
    sim_modes = get_simulation_modes()
    return await _run_check("mqtt", lambda: _check_mqtt(sim_modes))


# ---------------------------------------------------------------------------
# Public aggregator
# ---------------------------------------------------------------------------


async def _check_sentinel(simulation_modes: dict[str, bool]) -> dict[str, Any]:
    # The human-approval gate is the sole Sentinel consumer. In sim mode the
    # service is never called — don't probe and don't degrade health over it.
    if simulation_modes.get("human_approval", True):
        return {"status": "not_used", "reason": "human_approval in simulation mode"}

    settings = get_settings()
    raw_url = settings.SENTINEL_API_URL or ""
    raw_key = settings.SENTINEL_API_KEY or ""
    if not raw_url.strip() and not raw_key.strip():
        return {"status": "not_configured"}
    if not sentinel_api_key_is_valid(raw_key):
        return {"status": "down", "reason": "human_approval_unavailable"}

    try:
        health_url = sentinel_health_url(
            raw_url,
            allow_loopback=not is_production_like_environment(settings.ENVIRONMENT),
        )
    except SentinelTargetError:
        return {"status": "down", "reason": "human_approval_unavailable"}

    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=CHECK_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as http:
            # Deliberately unauthenticated and read-only. The health probe must
            # never transmit the Sentinel API key.
            resp = await http.get(health_url)
            resp.raise_for_status()
    except Exception:
        # Do not let transport exceptions echo a configured origin or embedded
        # credential into the full dependency report.
        return {"status": "down", "reason": "human_approval_unavailable"}
    return {"status": "up"}


async def gather_dependency_report() -> dict[str, Any]:
    """
    Run every dependency check in parallel and return a consolidated report.

    Overall status degrades to ``degraded`` if any required dependency
    returns ``down``. ``not_configured`` and ``not_used`` are both
    considered healthy.
    """
    settings = get_settings()
    sim_modes = get_simulation_modes()

    (
        postgres,
        redis_res,
        mqtt,
        stripe_res,
        llm,
        upstream_mcp,
        signing_key,
        sentinel,
    ) = await asyncio.gather(
        _run_check("postgres", _check_postgres),
        _run_check("redis", _check_redis),
        _run_check("mqtt", lambda: _check_mqtt(sim_modes)),
        _run_check("stripe", _check_stripe),
        _run_check("llm", lambda: _check_llm(sim_modes)),
        _run_check("upstream_mcp", _check_upstream_mcp),
        _run_check("signing_key", _check_signing_key),
        _run_check("sentinel", lambda: _check_sentinel(sim_modes)),
    )

    dependencies = {
        "postgres": postgres,
        "redis": redis_res,
        "mqtt": mqtt,
        "stripe": stripe_res,
        "llm": llm,
        "upstream_mcp": upstream_mcp,
        "signing_key": signing_key,
        "sentinel": sentinel,
    }

    unhealthy = [
        name for name, r in dependencies.items() if r.get("status") not in _OK_STATUSES
    ]
    runtime_degradation = get_runtime_degradation()
    if runtime_degradation.get("degraded"):
        overall = "degraded"
        if "runtime_degradation" not in unhealthy:
            unhealthy = [*unhealthy, "runtime_degradation"]
    else:
        overall = "healthy" if not unhealthy else "degraded"

    return {
        "status": overall,
        "version": settings.APP_VERSION,
        "commit_sha": get_build_commit_sha(),
        # How that SHA was established. "stamped" is the only value an image
        # built from the documented archive-stamped release context
        # path can produce; anything else means the running image was built by
        # something other than the release procedure. See build_metadata.
        "build_provenance": get_build_provenance(),
        "environment": settings.ENVIRONMENT,
        # Whether the production trust guardrails actually engage on this host.
        # ENVIRONMENT defaults to "local", so a deploy that never sets it runs
        # with those guardrails silently disabled; reporting the resolved value
        # makes that externally auditable instead of guesswork.
        "production_like": is_production_like_environment(settings.ENVIRONMENT),
        "dependencies": dependencies,
        "simulation_modes": sim_modes,
        "enable_proof_surfaces": bool(settings.ENABLE_PROOF_SURFACES),
        "enable_dogfood_tool": bool(settings.ENABLE_DOGFOOD_TOOL),
        "enable_dogfood_second_tool": bool(settings.ENABLE_DOGFOOD_SECOND_TOOL),
        "runtime_degradation": runtime_degradation,
        "metric_scopes": _METRIC_SCOPES,
        "unhealthy": unhealthy,
    }


# Dependencies the trust-plane wedge actually runs on. Everything else in the
# full report exists for proof-surface consumers (mqtt/iot, llm/telemetry_pm,
# sentinel/human_approval) or gated expansion surfaces (stripe: top-up, KYC).
PUBLIC_DEPENDENCY_KEYS: frozenset[str] = frozenset(
    {"postgres", "redis", "signing_key", "upstream_mcp"}
)


def build_public_dependency_report(full_report: dict[str, Any]) -> dict[str, Any]:
    """Project the full dependency report onto the wedge's public surface.

    The unauthenticated ``/health/dependencies`` payload used to publish the
    per-service ``simulation_modes`` map and probe results for dependencies
    only frozen proof surfaces consume — a public billboard of everything the
    platform is *not* running. When proof surfaces are unmounted
    (``ENABLE_PROOF_SURFACES=false``, the required production posture) none of
    those services are reachable, so their flags describe nothing a caller can
    exercise. This projection reports only what the wedge runs on: postgres,
    redis, the signing key, the upstream MCP tool, version + commit SHA, and
    the resolved environment posture. The overall verdict and ``unhealthy``
    list are recomputed from the projected set so a hidden proof-surface
    dependency can never flip the public status.

    Simulation posture remains available to operators in the startup log
    (``phase="runtime_posture"``). The full report remains available on
    deployments that mount proof surfaces, where those flags and probes
    describe live, reachable routes.
    """
    dependencies = {
        name: result
        for name, result in full_report["dependencies"].items()
        if name in PUBLIC_DEPENDENCY_KEYS
    }
    unhealthy = [
        name for name, r in dependencies.items() if r.get("status") not in _OK_STATUSES
    ]
    runtime_degradation = full_report["runtime_degradation"]
    if runtime_degradation.get("degraded"):
        overall = "degraded"
        if "runtime_degradation" not in unhealthy:
            unhealthy = [*unhealthy, "runtime_degradation"]
    else:
        overall = "healthy" if not unhealthy else "degraded"

    return {
        "status": overall,
        "version": full_report["version"],
        "commit_sha": full_report["commit_sha"],
        "build_provenance": full_report["build_provenance"],
        "environment": full_report["environment"],
        "production_like": full_report["production_like"],
        "dependencies": dependencies,
        "enable_proof_surfaces": full_report["enable_proof_surfaces"],
        "runtime_degradation": runtime_degradation,
        "metric_scopes": _METRIC_SCOPES,
        "unhealthy": unhealthy,
    }
