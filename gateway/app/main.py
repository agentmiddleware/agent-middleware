"""
Agent Middleware API
====================
Transaction integrity for consequential autonomous agent-to-tool actions.

The core path scopes authority, dispatches a configured upstream MCP tool at
most once per accepted idempotency key, meters the wallet debit, and issues
auditable receipts. Other workloads are labeled proof surfaces.
"""

import asyncio
import logging
import math
from pathlib import Path
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .core.auth import AuthContext, debug_open_auth_armed, get_auth_context
from .core.build_metadata import get_build_commit_sha, get_build_provenance
from .core.config import get_settings
from .core.durable_state import (
    DurableStateConfigError,
    close_durable_state,
    get_durable_state,
)
from .core.health import (
    CHECK_TIMEOUT_SECONDS,
    build_public_dependency_report,
    check_redis_liveness,
    check_database_readiness,
    check_mqtt_readiness,
    gather_dependency_report,
)
from .core.public_contact import validated_public_contact as _public_contact_metadata
from .core.product_positioning import (
    POSITIONING_DESCRIPTION,
    POSITIONING_TAGLINE,
)
from .core.rate_limiter import RateLimitMiddleware, rate_limit_discovery
from .core.runtime_mode import get_simulation_modes
from .middleware.head_method import HeadMethodMiddleware
from .middleware.request_body_limit import RequestBodyLimitMiddleware
from .middleware.security_headers import SecurityHeadersMiddleware
from .core.trust_mode import (
    is_production_like_environment,
    validate_trust_mode_guardrails,
    warn_if_trust_mode_permissive,
)
from .db.database import SchemaInitError, init_db, close_db
from .services.mcp_dispatch_attempts import get_duplicate_guard_metrics
from .services.mcp_phase9_tools import sync_proof_surface_mcp_registration
from .services.signing_keys import (
    SigningKeyError,
    validate_signing_key_configuration,
)
from .routers.well_known import get_agent_first_metadata
from .routers import (
    auth,
    iot,
    telemetry,
    media,
    comms,
    agent_comms_durable,
    docs,
    factory,
    content_generation,
    red_team,
    oracle,
    audit,
    billing,
    permits,
    permit_requests,
    quotes,
    receipts,
    evidence,
    preflight,
    protocol,
    rtaas,
    sandbox,
    sandbox_behavioral,
    telemetry_scope,
    broadcast,
    ai,
    webhooks,
    mcp,
    mcp_public,
    mcp_standard,
    kyc,
    api_keys,
    dev_keys,
    keys,
    me,
    awi,
    awi_enhanced,
    discover,
    well_known,
    static,
    planner,
    pods,
    policies,
    x402,
)

settings = get_settings()
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Try structured logging, fall back to standard logging
try:
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logger = structlog.get_logger()
    _USE_STRUCTLOG = True
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger(__name__)
    _USE_STRUCTLOG = False


_GENERATE_SEED_COMMAND = (
    "python3 -c 'import base64, secrets; "
    "print(base64.b64encode(secrets.token_bytes(32)).decode())'"
)

_SIGNING_KEY_REMEDIATION_DEFAULT = (
    "Check TRUST_SIGNING_PRIVATE_KEY_B64 and TRUST_SIGNING_KEY_ID. "
    "See docs/key-management.md."
)

_SIGNING_KEY_REMEDIATION = {
    "trust_signing_private_key_required": (
        "TRUST_MODE_ENABLED is true, so TRUST_SIGNING_PRIVATE_KEY_B64 must hold an "
        f"Ed25519 seed. Generate one with: {_GENERATE_SEED_COMMAND} — then set it in "
        ".env (local) or your host secret store, and reuse the same seed on every "
        "restart. See .env.example and docs/key-management.md."
    ),
    "invalid_trust_signing_private_key": (
        "TRUST_SIGNING_PRIVATE_KEY_B64 is set but is not strict base64 of exactly 32 "
        f"raw bytes. Regenerate it with: {_GENERATE_SEED_COMMAND} — and copy the value "
        "without surrounding quotes or whitespace. See docs/key-management.md."
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_trust_mode_guardrails(settings)
    warn_if_trust_mode_permissive(settings)
    # Operator-facing posture record. The unauthenticated /health/dependencies
    # payload no longer publishes per-service simulation modes when proof
    # surfaces are unmounted, so this startup line is where that truth lives
    # for production operators.
    logger.info(
        "app_startup",
        phase="runtime_posture",
        environment=settings.ENVIRONMENT,
        production_like=is_production_like_environment(settings.ENVIRONMENT),
        enable_proof_surfaces=bool(settings.ENABLE_PROOF_SURFACES),
        enable_dogfood_tool=bool(settings.ENABLE_DOGFOOD_TOOL),
        enable_dogfood_second_tool=bool(settings.ENABLE_DOGFOOD_SECOND_TOOL),
        simulation_modes=get_simulation_modes(),
        cors_origins=settings.CORS_ORIGINS,
    )
    # A public deployment whose manifest tells agents the operator has no
    # contact is a discovery-honesty defect, not a neutral default. Partial
    # configuration already fails the boot (validated_public_contact raises at
    # import); complete absence is legal for local instances, so production
    # only gets this loud nudge.
    # DEBUG open mode (DEBUG=true with no keys configured) authenticates
    # unknown local callers as bootstrap admin. It is now loopback-bound,
    # but a shared demo should still boot with real keys instead, so say so
    # loudly whenever this boot leaves it reachable.
    if debug_open_auth_armed(settings):
        logger.warning(
            "debug_open_auth_armed: DEBUG=true with no VALID_API_KEYS or "
            "STATIC_DEV_API_KEYS, so unknown callers from this machine "
            "authenticate as bootstrap admin (remote peers are refused). "
            "Set VALID_API_KEYS before any shared or buyer-visible demo."
        )
    if public_contact is None and is_production_like_environment(settings.ENVIRONMENT):
        logger.warning(
            "public_contact_not_configured: /.well-known/agent.json is "
            "reporting provider.status=contact_not_configured on a "
            "production-like deployment. Set PUBLIC_CONTACT_NAME, "
            "PUBLIC_CONTACT_EMAIL, and PUBLIC_CONTACT_URL together "
            "(see the hosted-service deploy runbook (not in this repository), Required production variables)."
        )
    try:
        signing_key_state = validate_signing_key_configuration(settings)
    except SigningKeyError as exc:
        # The exception message is a stable error code consumed by
        # /health/dependencies. Log the operator remediation alongside it so a
        # failed first boot says how to fix itself instead of only what broke.
        logger.error(
            "app_startup_failed",
            phase="signing_key_validation",
            error=str(exc),
            remediation=_SIGNING_KEY_REMEDIATION.get(
                str(exc), _SIGNING_KEY_REMEDIATION_DEFAULT
            ),
        )
        raise
    logger.info(
        "app_startup",
        phase="signing_key_validated",
        state=signing_key_state,
    )

    cleanup_task: asyncio.Task | None = None
    startup_time = time.monotonic()

    async def periodic_cleanup():
        """Background task to cleanup expired entries from services."""
        while True:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes

                # Frozen proof surfaces do not run background work when they are
                # not mounted. This keeps production-like trust deployments from
                # importing or mutating demo-only state behind a disabled flag.
                if settings.ENABLE_PROOF_SURFACES:
                    from .services.webauthn_provider import get_webauthn_provider

                    webauthn = get_webauthn_provider()
                    result = webauthn.cleanup_expired()
                    if (
                        result["challenges_removed"] > 0
                        or result["verifications_removed"] > 0
                    ):
                        logger.info(
                            "cleanup_completed",
                            challenges_removed=result["challenges_removed"],
                            verifications_removed=result["verifications_removed"],
                        )

                    from .services.awi_session import get_awi_session_manager

                    session_mgr = get_awi_session_manager()
                    result = await session_mgr.cleanup_expired_async()
                    if result["sessions_removed"] > 0:
                        logger.info(
                            "cleanup_completed",
                            sessions_removed=result["sessions_removed"],
                        )

                    # Telemetry retention sweep (per
                    # TELEMETRY_RETENTION_HOURS). Ingests do a lazy eviction too;
                    # this handles idle proof-surface systems.
                    if settings.DATABASE_URL:
                        from .services.telemetry_pm import EventStore

                        removed = await EventStore(
                            retention_hours=settings.TELEMETRY_RETENTION_HOURS
                        )._evict_expired()
                        if removed:
                            logger.info(
                                "cleanup_completed",
                                telemetry_events_removed=removed,
                            )

                # Repair permit budget reservations orphaned by a crash between
                # reserve and the receipt write (only touches idle permits).
                if settings.DATABASE_URL:
                    from .services.idempotency import get_idempotency_service
                    from .services.mcp_dispatch_attempts import (
                        MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
                        MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
                        dispatch_reconciliation_idle_seconds,
                        get_mcp_dispatch_attempt_service,
                    )
                    from .services.mcp_dispatch_reconciliation import (
                        get_mcp_dispatch_reconciliation_service,
                    )
                    from .services.permits import get_permit_service

                    # Reconciliation uses the rollout-wide supported maximum,
                    # not this worker's optional upstream settings. Dormant or
                    # locally smaller timeout values cannot disable unrelated
                    # permit/idempotency maintenance.
                    dispatch_idle_seconds = dispatch_reconciliation_idle_seconds(
                        connect_timeout_seconds=MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
                        call_timeout_seconds=MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
                    )
                    dispatch_result = (
                        await get_mcp_dispatch_reconciliation_service().reconcile(
                            idle_seconds=dispatch_idle_seconds,
                            terminal_idle_seconds=300,
                        )
                    )
                    if dispatch_result.repaired:
                        logger.info(
                            "cleanup_completed",
                            dispatch_prepared_finalized=(
                                dispatch_result.prepared_finalized
                            ),
                            dispatch_marked_uncertain=(
                                dispatch_result.dispatched_uncertain
                            ),
                            dispatch_terminal_recovered=(
                                dispatch_result.terminal_recovered
                            ),
                            dispatch_idempotency_recovered=(
                                dispatch_result.idempotency_recovered
                            ),
                        )
                    if dispatch_result.failed_attempt_ids:
                        logger.warning(
                            "mcp_dispatch_reconciliation_failures",
                            failed_count=len(dispatch_result.failed_attempt_ids),
                        )

                    dispatch_metrics = (
                        await get_mcp_dispatch_attempt_service().summarize(
                            idle_seconds=dispatch_idle_seconds,
                            terminal_idle_seconds=300,
                        )
                    )
                    uncertainty_count = dispatch_metrics.state_counts.get(
                        "delivery_uncertain", 0
                    )
                    if uncertainty_count or dispatch_metrics.reconciliation_backlog:
                        logger.warning(
                            "mcp_dispatch_operator_alert",
                            delivery_uncertain=uncertainty_count,
                            stale_active=dispatch_metrics.stale_active,
                            unfinalized_terminal=(
                                dispatch_metrics.unfinalized_terminal
                            ),
                            terminal_idempotency_incomplete=(
                                dispatch_metrics.terminal_idempotency_incomplete
                            ),
                            reconciliation_backlog=(
                                dispatch_metrics.reconciliation_backlog
                            ),
                        )

                    corrected = await get_permit_service().reconcile_budgets()
                    if corrected:
                        logger.info(
                            "cleanup_completed",
                            permit_budgets_reconciled=corrected,
                        )

                    (
                        repaired,
                        needs_review,
                    ) = await get_idempotency_service().reconcile_stuck_records(
                        idle_seconds=300
                    )
                    if repaired or needs_review:
                        logger.info(
                            "cleanup_completed",
                            idempotency_records_repaired=repaired,
                            idempotency_records_needing_review=needs_review,
                        )

            except asyncio.CancelledError:
                logger.info("cleanup_task_stopped")
                break
            except Exception as e:
                logger.warning("cleanup_error", error=str(e))

    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info(
        "app_startup",
        phase="cleanup_task_started",
        startup_time_s=time.monotonic() - startup_time,
    )

    startup_time = time.monotonic()
    if settings.DATABASE_URL:
        try:
            await init_db()
            logger.info(
                "app_startup",
                phase="database_initialized",
                startup_time_s=time.monotonic() - startup_time,
            )
        except Exception as e:
            # Production-like (and any SchemaInitError) fail closed: do not
            # boot against a DB that still needs Alembic.
            if isinstance(e, SchemaInitError) or is_production_like_environment(
                settings.ENVIRONMENT
            ):
                logger.error(
                    "app_startup",
                    phase="database_init_failed",
                    error=str(e),
                )
                raise
            logger.warning("app_startup", phase="database_init_failed", error=str(e))

        if settings.TRUST_MODE_ENABLED:
            from .services.signing_keys import get_signing_key_service

            try:
                signing_key = await get_signing_key_service().ensure_active_key()
                logger.info(
                    "app_startup",
                    phase="trust_signing_key_ready",
                    key_id=signing_key.key_id,
                    startup_time_s=time.monotonic() - startup_time,
                )
            except Exception as e:
                logger.error(
                    "app_startup",
                    phase="trust_signing_key_failed",
                    error=str(e),
                )
                raise

    # Fail closed at boot in production-like envs (DurableStateConfigError)
    # rather than waiting for the first request that touches durable state.
    startup_time = time.monotonic()
    try:
        state_report = await get_durable_state().health_report()
        logger.info(
            "app_startup",
            phase="durable_state_ready",
            backend=state_report.get("backend"),
            enabled=state_report.get("enabled"),
            startup_time_s=time.monotonic() - startup_time,
        )
    except DurableStateConfigError:
        logger.error("app_startup", phase="durable_state_failed")
        raise

    startup_time = time.monotonic()
    sync_proof_surface_mcp_registration()
    logger.info(
        "app_startup",
        phase="mcp_proof_surface_registration_synced",
        enable_proof_surfaces=bool(settings.ENABLE_PROOF_SURFACES),
        startup_time_s=time.monotonic() - startup_time,
    )

    # The design-partner adapter is configuration-only and fail-closed. When
    # enabled, startup must complete MCP initialize + tools/list and register
    # the exact executable tool before the app advertises readiness.
    startup_time = time.monotonic()
    from .services.upstream_mcp import register_configured_upstream_mcp

    upstream_service = await register_configured_upstream_mcp(settings=settings)
    logger.info(
        "app_startup",
        phase="mcp_upstream_registration_synced",
        enabled=bool(settings.MCP_UPSTREAM_ENABLED),
        public_tool_id=(
            upstream_service.get("service_id") if upstream_service else None
        ),
        upstream_origin=(
            upstream_service.get("upstream_origin") if upstream_service else None
        ),
        startup_time_s=time.monotonic() - startup_time,
    )

    logger.info("app_ready", version=settings.APP_VERSION)

    yield

    logger.info("app_shutdown", phase="starting")
    shutdown_start = time.monotonic()

    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        logger.info("app_shutdown", phase="cleanup_task_stopped")

    await close_db()
    await close_durable_state()

    await asyncio.sleep(2)  # Allow in-flight requests to drain
    logger.info(
        "app_shutdown",
        phase="complete",
        shutdown_duration_s=time.monotonic() - shutdown_start,
    )


def build_openapi_servers(public_url: str, environment: str) -> list[dict[str, str]]:
    """Advertise only server entries the operator means.

    An unset PUBLIC_URL used to fall back to a test domain labeled "Public
    API", so generated clients pointed at a host the operator never
    configured and manifests named the wrong issuer. The public entry now
    appears only when PUBLIC_URL is set. Local-compatible environments always
    keep the localhost entry, so a stock checkout still documents where it
    actually serves; a production-like boot with no PUBLIC_URL advertises no
    servers rather than a wrong one.
    """
    servers = (
        [{"url": public_url, "description": "Public API"}] if public_url.strip() else []
    )
    if not is_production_like_environment(environment):
        servers.append(
            {"url": "http://localhost:8000", "description": "Local Development"}
        )
    return servers


public_contact = _public_contact_metadata(settings)


app = FastAPI(
    lifespan=lifespan,
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "## Agent Transaction Integrity\n\n"
        f"**{POSITIONING_TAGLINE}**\n\n"
        f"{POSITIONING_DESCRIPTION}\n\n"
        "It is not a full agent middleware platform. This is a durable gateway "
        "state machine, not one distributed ACID transaction and not proof of "
        "the downstream effect. It is not exactly-once: a remote side effect is "
        "deduplicated only when "
        "the upstream honors the forwarded idempotency key. See `/WEDGE.md` "
        "and `/SECURITY_LIMITATIONS.md`.\n\n"
        "### Canonical Loop\n\n"
        "`logical action -> authorize -> reserve allowance -> debit -> claim "
        "dispatch -> confirmed outcome | delivery_uncertain -> receipt/audit "
        "-> authoritative external reconciliation required`\n\n"
        "### Product Surface\n\n"
        "- **Logical action identity** — accepted key bound to one payload\n"
        "- **Bounded authority consumption** — scoped permit and configured allowance\n"
        "- **One-shot gateway dispatch/debit** — at most once per accepted action\n"
        "- **Explicit uncertainty** — `delivery_uncertain` is not auto-redispatched\n"
        "- **Linked gateway evidence** — receipt and audit data for reconciliation\n"
        "- **Discovery** — `.well-known/agent.json`, `/mcp/tools.json`, "
        "`/llms.txt`, `/v1/discover`\n\n"
        "### Proof Surfaces\n\n"
        "Agentic-web, browser, content, oracle, sandbox, media, IoT, red-team, "
        "and telemetry demos are labeled scaffolding. They mount only when "
        "`ENABLE_PROOF_SURFACES=true` and do not define the product.\n\n"
        "### Authentication\n\n"
        "Protected endpoints require an API key via the `X-API-Key` header.\n\n"
        "### For Agents\n\n"
        "Start at `/.well-known/agent.json`, then `/llms.txt` and "
        "`/mcp/tools.json`. Prefer `PUBLIC_URL` over any localhost server entry."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    # Omit contact metadata until an accountable, monitored identity is set.
    # This avoids presenting placeholder support details as a real escalation path.
    contact=public_contact or None,
    license_info={
        "name": "Business Source License 1.1",
        "url": "https://github.com/PetrefiedThunder/agent-middleware/blob/main/LICENSE",
    },
    servers=build_openapi_servers(settings.PUBLIC_URL, settings.ENVIRONMENT),
)

# --- Middleware Stack ---

# Rate limiting (enforces documented 120 req/min per API key)
app.add_middleware(RateLimitMiddleware)

# Inbound body ceiling. Registered between the rate limiter and CORS so the
# stack puts it outside RateLimitMiddleware — an oversized body is refused
# before any per-key bookkeeping or handler buffers it — and inside
# CORSMiddleware, so the 413 carries the same headers as every other response.
app.add_middleware(
    RequestBodyLimitMiddleware, max_body_size=settings.MAX_REQUEST_BODY_BYTES
)


def add_cors_middleware(application: FastAPI, origins: list[str]) -> None:
    """Attach CORS, enabling credentials only for an explicit allowlist.

    Never pair credentialed CORS with a wildcard origin: with
    ``allow_origins=["*"]`` and ``allow_credentials=True``, Starlette echoes the
    caller's ``Origin`` back and still returns
    ``Access-Control-Allow-Credentials: true``, letting any website make
    credentialed cross-origin reads against the trust plane. A wildcard therefore
    serves ``Access-Control-Allow-Origin: *`` with credentials disabled.
    """
    allow_all_origins = "*" in origins
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=not allow_all_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# CORS origins are configurable via the CORS_ORIGINS env var (comma-separated).
# The default wildcard is a deliberate posture, not an accident: every
# authenticated route takes explicit header credentials (X-API-Key / Bearer),
# never cookies, and add_cors_middleware disables credentialed CORS under a
# wildcard, so `*` here grants cross-origin reads of public discovery
# surfaces and nothing else. Operators fronting a browser app that sends
# credentials must set an explicit origin list instead. Decision documented
# in SECURITY_LIMITATIONS.md ("CORS posture").
cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
add_cors_middleware(app, cors_origins)
if "*" in cors_origins:
    logger.info(
        "cors_wildcard_active: credential-less wildcard CORS is the documented "
        "default for this header-authenticated API; set CORS_ORIGINS to an "
        "explicit list to change it (see SECURITY_LIMITATIONS.md)"
    )

# Baseline response hardening (nosniff, framing, referrer, CSP, no-store
# on sensitive paths, HSTS over TLS). Registered near-last so it wraps
# almost everything: Starlette builds the stack in reverse registration order,
# and stamping rate-limit 429s and CORS preflights too is the point.
app.add_middleware(SecurityHeadersMiddleware)

# HEAD → GET translation, outermost. FastAPI's APIRoute does not auto-register
# HEAD for GET routes (plain Starlette routes like /openapi.json do), which
# made HEAD answer 405 on most public GETs. Outermost placement means every
# layer below — routing included — sees a GET, and the response leaves with
# the GET's status and headers but no body, per RFC 9110 §9.3.2.
app.add_middleware(HeadMethodMiddleware)


def _json_safe_numbers(value: Any) -> Any:
    """Spell non-finite floats as strings; strict JSON has no such numbers."""
    if isinstance(value, float) and not math.isfinite(value):
        return (
            "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        )
    if isinstance(value, dict):
        return {key: _json_safe_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_numbers(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI's default 422, safe to render for every refused input.

    Starlette's JSON parser accepts the bare literals Infinity, -Infinity and
    NaN, and a validation error echoes the refused input back. The default
    handler then failed to serialize its own 422 and answered 500 instead.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": _json_safe_numbers(jsonable_encoder(exc.errors()))},
    )


# --- Mount service routers ---

CORE_TRUST_ROUTERS = (
    audit,
    policies,
    billing,
    permits,
    permit_requests,
    quotes,
    receipts,
    evidence,
    preflight,
    mcp,
    mcp_standard,
    mcp_public,
    api_keys,
    keys,
    me,
    discover,
    well_known,
    static,
    docs,
)

# Dormant trust surfaces — real trust-plane features with no active customer
# demand (see AGENTS.md: no new core capability without documented customer
# evidence). Unlike PROOF_SURFACE_ROUTERS these are not demo scaffolding, but
# they are not the wedge either, so production deployments
# (ENABLE_PROOF_SURFACES=false) neither mount nor advertise them:
#   - auth: JWT exchange — a second authentication story; the wedge contract
#     is "send the API key" (X-API-Key). app.core.auth still *validates*
#     Bearer JWTs, but nothing can mint one while this router is unmounted.
#   - kyc: Stripe Identity verification; Stripe is not configured in
#     production and /v1/discover already omits the capability without it.
#   - planner: budget optimizer, adjacent to but outside the
#     permit→invoke→receipt loop.
#   - pods: a named group of agent API keys under one shared budget
#     (composition of existing sponsor/agent wallets + keys, see
#     docs/pods.md); a new core capability with no named-customer evidence
#     yet, so it stays dormant per the invariant above.
#   - x402: settlement facilitation — a real trust surface (permit-governed
#     402 payment authorization, shadow-ledger metering, signed receipts)
#     with no active customer demand; the docs/settlement-rails.md freeze
#     holds, so it never writes real ledger entries and stays unmounted in
#     production.
# Billing expansion surfaces (transfers, top-ups, child/swarm wallets,
# dry-run sandbox, marketplace) gate the same way via
# billing.expansion_router — see app/routers/billing.py.
DORMANT_TRUST_ROUTERS = (
    auth,
    kyc,
    planner,
    pods,
    x402,
)


def mount_dormant_trust_surfaces(application: FastAPI) -> None:
    """Mount every dormant trust surface (single source of truth).

    Used below when ENABLE_PROOF_SURFACES is true and by the test suite's
    dormant-marked fixtures, so the mounted set can never drift between the
    two.
    """
    for router_module in DORMANT_TRUST_ROUTERS:
        application.include_router(router_module.router)
    application.include_router(billing.expansion_router)


# Frozen scaffolding — do not expand. See docs/PROOF_SURFACES.md.
PROOF_SURFACE_ROUTERS = (
    iot,
    telemetry,
    media,
    comms,
    agent_comms_durable,
    factory,
    content_generation,
    red_team,
    oracle,
    protocol,
    rtaas,
    sandbox,
    sandbox_behavioral,
    telemetry_scope,
    broadcast,
    ai,
    awi,
    awi_enhanced,
)

# Action issuance stays frozen: the configured upstream has no qualified
# ActionToolBinding. Only explicit test fixtures mount permits.action_router;
# production and proof-enabled apps retain recovery without advertising issuance.
for router_module in CORE_TRUST_ROUTERS:
    app.include_router(
        router_module.router,
        include_in_schema=(
            settings.ENABLE_STANDARD_MCP_ENDPOINT
            if router_module is mcp_standard
            else True
        ),
    )

# Self-serve dev keys: the handler is triple-gated at runtime (its own
# ENABLE_DEV_KEY_SELF_PROVISION flag answers 404, production-like environments
# fail closed with 403, and cross-origin browser calls are refused), so the
# route stays mounted for local flag flips — but it is only *advertised* in
# the OpenAPI schema when the local opt-in flag is actually on. Production
# cannot set the flag (validate_trust_mode_guardrails refuses to boot), so
# the public spec never carries the path.
app.include_router(
    dev_keys.router, include_in_schema=settings.ENABLE_DEV_KEY_SELF_PROVISION
)

# Stripe webhooks answer only signed Stripe traffic; a deployment with no
# Stripe key configured has nothing that could ever call them, so they are
# mounted only when Stripe is actually configured (or on instances that mount
# every surface anyway).
if settings.STRIPE_SECRET_KEY or settings.ENABLE_PROOF_SURFACES:
    app.include_router(webhooks.router)

if settings.ENABLE_PROOF_SURFACES:
    mount_dormant_trust_surfaces(app)
    for router_module in PROOF_SURFACE_ROUTERS:
        app.include_router(router_module.router)
else:
    logger.info(
        "proof_surfaces_disabled: ENABLE_PROOF_SURFACES=false; "
        "only CORE_TRUST_ROUTERS are mounted (dormant trust surfaces and "
        "proof surfaces stay unmounted and unadvertised)"
    )


# --- Discovery & Health Endpoints ---


@app.get(
    "/",
    tags=["Discovery"],
    summary="API root — service index and operator evidence negotiation",
    description=(
        "Returns the API service index for JSON callers or the truthful public "
        "operator evidence index for browsers. For machine bootstrap, follow "
        "`GET /.well-known/agent.json` → field `agent_first`."
    ),
    responses={
        200: {
            "description": "JSON service index or HTML operator evidence index",
            "content": {"text/html": {"schema": {"type": "string"}}},
        }
    },
)
async def root(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        dashboard_path = _REPO_ROOT / "static" / "dashboard.html"
        if dashboard_path.is_file():
            return HTMLResponse(
                dashboard_path.read_text(encoding="utf-8"),
                media_type="text/html; charset=utf-8",
            )

    payload: dict[str, Any] = {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "agent_first": get_agent_first_metadata(),
        "description": POSITIONING_DESCRIPTION,
        "surface_boundaries": {
            "core_trust": [
                "audit",
                "billing",
                "permits",
                "receipts",
                "evidence",
                "mcp",
                "api_keys",
                "signing_keys",
                "discover",
                "policies",
            ],
            "dormant_trust": [
                "auth_jwt",
                "kyc",
                "planner",
                "billing_expansion",
            ],
            # Unmounted workloads are not discovery: the proof-surface name
            # list is published only while those routers are actually mounted.
            "proof_surface": (
                [
                    "awi",
                    "browser_automation",
                    "content_generation",
                    "iot_bridge",
                    "media_engine",
                    "oracle",
                    "red_team",
                    "rtaas",
                    "sandbox",
                    "telemetry_pm",
                ]
                if get_settings().ENABLE_PROOF_SURFACES
                else []
            ),
            "proof_surfaces_mounted": bool(get_settings().ENABLE_PROOF_SURFACES),
        },
        "services": {
            "iot_bridge": {
                "base_path": "/v1/iot",
                "description": (
                    "Secure protocol translation for IoT devices with topic-level ACLs."
                ),
                "endpoints": [
                    "POST /v1/iot/devices",
                    "GET /v1/iot/devices",
                    "GET /v1/iot/devices/{device_id}",
                    "DELETE /v1/iot/devices/{device_id}",
                    "POST /v1/iot/devices/{device_id}/messages",
                    "POST /v1/iot/devices/{device_id}/subscribe",
                ],
            },
            "autonomous_pm": {
                "base_path": "/v1/telemetry",
                "description": (
                    "Telemetry ingestion, anomaly detection, and autonomous "
                    "pull request generation."
                ),
                "endpoints": [
                    "POST /v1/telemetry/events",
                    "POST /v1/telemetry/events/single",
                    "GET /v1/telemetry/anomalies",
                    "GET /v1/telemetry/anomalies/{anomaly_id}",
                    "POST /v1/telemetry/anomalies/{anomaly_id}/auto-pr",
                    "GET /v1/telemetry/stats",
                ],
            },
            "media_engine": {
                "base_path": "/v1/media",
                "description": (
                    "Video-to-viral-clip pipeline with cross-platform distribution."
                ),
                "endpoints": [
                    "POST /v1/media/videos",
                    "GET /v1/media/videos/{video_id}",
                    "GET /v1/media/videos/{video_id}/hooks",
                    "POST /v1/media/videos/{video_id}/clips",
                    "POST /v1/media/distribute",
                    "GET /v1/media/clips/{clip_id}",
                ],
            },
            "agent_comms": {
                "base_path": "/v1/comms",
                "description": (
                    "Agent-to-agent messaging, capability discovery, and "
                    "swarm task handoffs."
                ),
                "endpoints": [
                    "POST /v1/comms/agents",
                    "GET /v1/comms/agents",
                    "POST /v1/comms/messages",
                    "GET /v1/comms/messages/{agent_id}/inbox",
                    "POST /v1/comms/messages/{agent_id}/ack/{message_id}",
                    "POST /v1/comms/handoff",
                    "POST /v1/agent-comms/send",
                    "GET /v1/agent-comms/inbox",
                ],
            },
            "content_factory": {
                "base_path": "/v1/factory",
                "description": (
                    "Multi-format content generation from single sources, "
                    "with hook-based 1-to-20 multiplication, 9:16 vertical "
                    "rendering, animated captions, and algorithmic posting "
                    "schedule optimization."
                ),
                "endpoints": [
                    "POST /v1/factory/pipelines",
                    "GET /v1/factory/pipelines/{pipeline_id}",
                    "GET /v1/factory/pipelines/{pipeline_id}/content",
                    "GET /v1/factory/content/{content_id}",
                    "POST /v1/factory/campaigns",
                    "GET /v1/factory/campaigns/{campaign_id}",
                    "GET /v1/factory/campaigns",
                    "POST /v1/factory/analytics",
                    "GET /v1/factory/analytics/summary",
                    "POST /v1/factory/schedule",
                    "POST /v1/content/generate",
                    "GET /v1/content/{content_id}",
                ],
            },
            "agent_oracle": {
                "base_path": "/v1/oracle",
                "description": (
                    "Agent network infiltration: crawl directories, index APIs, "
                    "compute compatibility, register for inbound discovery "
                    "traffic."
                ),
                "endpoints": [
                    "POST /v1/oracle/crawl",
                    "POST /v1/oracle/crawl/batch",
                    "GET /v1/oracle/index",
                    "GET /v1/oracle/index/{api_id}",
                    "POST /v1/oracle/register",
                    "GET /v1/oracle/registrations",
                    "GET /v1/oracle/visibility",
                    "GET /v1/oracle/network",
                    "POST /v1/oracle/discovery",
                ],
            },
            "agent_billing": {
                "base_path": "/v1/billing",
                "description": (
                    "Two-tier wallet system (sponsor funds agent) with "
                    "ledger-backed per-action metering."
                ),
                # Only routes billing.router actually mounts in the wedge
                # posture. Expansion routes (child/swarm wallets, top-ups,
                # arbitrage, alerts) live on billing.expansion_router and are
                # advertised only when they are mounted — never list a path
                # that answers 404.
                "endpoints": [
                    "POST /v1/billing/wallets/sponsor",
                    "POST /v1/billing/wallets/agent",
                    "GET /v1/billing/wallets/{wallet_id}",
                    "GET /v1/billing/wallets",
                    "GET /v1/billing/ledger/{wallet_id}",
                    "POST /v1/billing/charge",
                    "GET /v1/billing/pricing",
                ]
                + (
                    [
                        "POST /v1/billing/wallets/child",
                        "POST /v1/billing/wallets/{wallet_id}/reclaim",
                        "GET /v1/billing/wallets/{wallet_id}/swarm",
                        "POST /v1/billing/top-up/prepare",
                        "GET /v1/billing/arbitrage",
                        "GET /v1/billing/alerts",
                    ]
                    if get_settings().ENABLE_PROOF_SURFACES
                    else []
                ),
            },
            "mcp_server": {
                "base_path": "/mcp",
                "description": (
                    "Model Context Protocol (MCP) gateway for governed tool "
                    "discovery and execution (permit → meter → dispatch → "
                    "receipt)."
                ),
                "endpoints": [
                    "GET /mcp/tools.json",
                    "GET /.well-known/mcp/tools.json",
                    "POST /mcp/messages",
                    *(
                        ["POST /mcp"]
                        if get_settings().ENABLE_STANDARD_MCP_ENDPOINT
                        else []
                    ),
                    "GET /mcp/tools",
                    "GET /mcp/tools/{service_id}",
                    "POST /mcp/tools/{service_id}/invoke",
                ],
            },
            "red_team_security": {
                "base_path": "/v1/security",
                "description": (
                    "Simulated red-team scan lifecycle (dormant proof "
                    "surface). Models scan jobs and findings; probes "
                    "nothing and refuses to run unless simulation is "
                    "explicitly enabled."
                ),
                "endpoints": [
                    "POST /v1/security/scans",
                    "GET /v1/security/scans",
                    "GET /v1/security/scans/{scan_id}",
                    "GET /v1/security/scans/{scan_id}/vulnerabilities",
                    "POST /v1/security/scans/quick",
                ],
            },
            "protocol_engine": {
                "base_path": "/v1/protocol",
                "description": (
                    "Code-to-discovery pipeline. Feed raw API code, get "
                    "llm.txt + OpenAPI spec + agent.json + Oracle registration."
                ),
                "endpoints": [
                    "POST /v1/protocol/generate",
                    "GET /v1/protocol/generations",
                    "GET /v1/protocol/generations/{generation_id}",
                ],
            },
            "rtaas": {
                "base_path": "/v1/rtaas",
                "description": (
                    "Simulated Red-Team-as-a-Service (dormant proof "
                    "surface). Models a scan-job lifecycle for an agent's "
                    "own external endpoints; contacts nothing and refuses "
                    "to run unless simulation is explicitly enabled."
                ),
                "endpoints": [
                    "POST /v1/rtaas/jobs",
                    "GET /v1/rtaas/jobs",
                    "GET /v1/rtaas/jobs/{job_id}",
                    "GET /v1/rtaas/jobs/{job_id}/vulnerabilities",
                ],
            },
            "sandbox": {
                "base_path": "/v1/sandbox",
                "description": (
                    "Interactive testing sandboxes. Headless puzzle "
                    "environments for testing agent generalization."
                ),
                "endpoints": [
                    "POST /v1/sandbox/environments",
                    "POST /v1/sandbox/environments/{env_id}/actions",
                    "POST /v1/sandbox/environments/{env_id}/evaluate",
                    "GET /v1/sandbox/environments/{env_id}",
                    "GET /v1/sandbox/environments",
                ],
            },
            "awi_phase9": {
                "base_path": "/v1/awi",
                "description": (
                    "AWI Phase 9 enhanced capabilities: FIDO2 passkey auth, "
                    "bidirectional DOM bridge, and RAG-based semantic memory."
                ),
                "endpoints": [
                    "POST /v1/awi/passkey/register",
                    "POST /v1/awi/passkey/challenge",
                    "POST /v1/awi/passkey/verify",
                    "GET /v1/awi/passkey/list/{wallet_id}",
                    "DELETE /v1/awi/passkey/{credential_id}",
                    "POST /v1/awi/dom/snapshot",
                    "POST /v1/awi/dom/element_at",
                    "POST /v1/awi/dom/execute",
                    "POST /v1/awi/dom/query",
                    "POST /v1/awi/dom/to_awi",
                    "POST /v1/awi/rag/ingest",
                    "POST /v1/awi/rag/search",
                    "POST /v1/awi/rag/context",
                    "GET /v1/awi/rag/list/{wallet_id}",
                    "DELETE /v1/awi/rag/{memory_id}",
                    "DELETE /v1/awi/rag/clear/{wallet_id}",
                ],
            },
            "telemetry_scope": {
                "base_path": "/v1/telemetry-scope",
                "description": (
                    "Multi-tenant autonomous PM. Scoped telemetry "
                    "pipelines with anomaly detection and auto-PR generation."
                ),
                "endpoints": [
                    "POST /v1/telemetry-scope/pipelines",
                    "POST /v1/telemetry-scope/pipelines/{pipeline_id}/events",
                    "GET /v1/telemetry-scope/pipelines/{pipeline_id}/anomalies",
                    "POST /v1/telemetry-scope/pipelines/{pipeline_id}/auto-pr",
                    "GET /v1/telemetry-scope/pipelines/{pipeline_id}/stats",
                    "GET /v1/telemetry-scope/pipelines/{pipeline_id}",
                    "GET /v1/telemetry-scope/pipelines",
                ],
            },
            "oracle_broadcast": {
                "base_path": "/v1/broadcast",
                "description": (
                    "Push published APIs into agent directories. "
                    "The network effects engine."
                ),
                "endpoints": [
                    "POST /v1/broadcast",
                    "GET /v1/broadcast/jobs",
                    "GET /v1/broadcast/jobs/{job_id}",
                    "GET /v1/broadcast/jobs/{job_id}/metrics",
                    "POST /v1/broadcast/jobs/{job_id}/events",
                    "GET /v1/broadcast/directories",
                ],
            },
        },
        "auth": {
            "method": "api_key",
            "header": "X-API-Key",
            "description": (
                "Pass your API key in the X-API-Key header on every request."
            ),
        },
        "rate_limits": rate_limit_discovery(),
        "docs": {
            "openapi": "/openapi.json",
            "interactive": "/docs",
            "redoc": "/redoc",
            "llm_txt": "/llm.txt",
            "llms_txt": "/llms.txt",
            "agent_manifest": "/.well-known/agent.json",
            "capability_index": "/v1/discover",
            "dependency_truth": "/health/dependencies",
        },
    }
    if not get_settings().ENABLE_PROOF_SURFACES:
        # Only advertise mounted core trust services when proof surfaces are off.
        payload["services"] = {
            key: value
            for key, value in payload["services"].items()
            if key in {"agent_billing", "mcp_server"}
        }
    return payload


@app.get(
    "/health",
    tags=["Discovery"],
    summary="Liveness check",
    responses={
        503: {
            "description": (
                "Degraded: Redis (the shared rate limiter) is configured but "
                "not answering; production-like deployments refuse /v1 "
                "requests in this state."
            )
        }
    },
    description=(
        "Returns 200 with status `healthy` when the API is running and its "
        "Redis (shared rate limiter) answers a PING, or when REDIS_URL is not "
        "configured (no PING is sent). Returns 503 with status `degraded` when "
        "Redis is configured but does not answer within 1 second; in "
        "production-like environments the rate limiter then refuses /v1 "
        "requests, so the API is not serving even though the process is up. "
        "`checks.redis` is `up`, `down`, or `not_configured`."
    ),
)
async def health():
    redis_status = await check_redis_liveness()
    healthy = redis_status != "down"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "degraded",
            "version": settings.APP_VERSION,
            "commit_sha": get_build_commit_sha(),
            # Same field /health/dependencies publishes, so the liveness probe
            # alone says whether that SHA came through the documented release
            # path. A bare SHA cannot be told apart from a stale stamp; a SHA
            # plus "stamped" can only mean the deployment is behind main.
            "build_provenance": get_build_provenance(),
            "checks": {"redis": redis_status},
        },
    )


@app.get(
    "/health/ready",
    tags=["Discovery"],
    summary="Readiness check",
    description="Returns 200 if all dependencies are ready. Use for Kubernetes readinessProbe.",
)
async def health_ready():
    checks: dict[str, dict[str, Any]] = {}
    all_healthy = True

    try:
        state_report = await asyncio.wait_for(
            get_durable_state().health_report(), timeout=CHECK_TIMEOUT_SECONDS
        )
    except Exception:
        state_report = {"ok": False, "backend": "unknown"}
    checks["state_store"] = {
        "status": "up" if state_report.get("ok", False) else "down",
        "backend": state_report.get("backend", "unknown"),
    }
    if checks["state_store"]["status"] == "down":
        all_healthy = False

    # Same sim-aware check /health/dependencies uses, so the two endpoints
    # cannot contradict each other: with iot_bridge in simulation mode both
    # report `not_used` — never "up" for a broker nothing touches. Only a
    # probe that actually fails (`down`) degrades readiness, and a probe only
    # runs when iot_bridge is real.
    checks["mqtt"] = await check_mqtt_readiness()
    if checks["mqtt"]["status"] == "down":
        all_healthy = False

    checks["database"] = await check_database_readiness()
    if checks["database"]["status"] != "up":
        all_healthy = False

    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content={
            "status": "ready" if all_healthy else "not_ready",
            "version": settings.APP_VERSION,
            "checks": checks,
        },
    )


@app.get(
    "/health/dependencies",
    tags=["Discovery"],
    summary="Dependency health check",
    description=(
        "Probes the dependencies this deployment runs on, in parallel with a "
        "short timeout. Each entry reports status, latency_ms, and an error "
        "code when unreachable (the exception class or a timeout; driver "
        "messages are logged server-side, never returned). With proof "
        "surfaces unmounted (the production posture) the payload covers the "
        "transaction-integrity boundary only: postgres, redis, signing key, "
        "upstream MCP, version + commit SHA. "
        "Instances that mount proof surfaces additionally report those "
        "surfaces' dependencies and per-service simulation modes; deps whose "
        "consumers are simulated return `not_used` so the verdict doesn't "
        "degrade on mock-only deployments."
    ),
)
async def health_dependencies():
    report = await gather_dependency_report()
    if get_settings().ENABLE_PROOF_SURFACES:
        # Proof surfaces are mounted and reachable, so their dependency truth
        # and simulation flags are live, relevant disclosures.
        return report
    # Production posture: report the wedge, not a billboard of unmounted
    # surfaces. Full truth stays in the startup log (phase="runtime_posture").
    return build_public_dependency_report(report)


@app.get(
    "/health/duplicate-guard",
    tags=["Discovery"],
    summary="Duplicate guard observability (admin-only)",
    description=(
        "Returns the current duplicate guard mode and observability metrics. "
        "Exposes log_mode_blocks (how many times log mode detected but allowed "
        "a duplicate) and enforce_mode_blocks (how many times enforce mode "
        "blocked a duplicate) for this process, plus "
        "enforce_mode_denials_durable (denial receipts with reason_code "
        "duplicate_request_new_key across the service lifetime) and the scope "
        "of each metric. Does not expose request contents or secrets. "
        "Requires bootstrap admin authentication."
    ),
)
async def health_duplicate_guard(
    auth: AuthContext = Depends(get_auth_context),
):
    auth.require_bootstrap_admin()
    return await get_duplicate_guard_metrics()
