from __future__ import annotations

import base64
import binascii
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.db_urls import is_sqlite_url

if TYPE_CHECKING:
    from app.core.config import Settings


logger = logging.getLogger(__name__)


PRODUCTION_LIKE_ENVIRONMENTS = frozenset(
    {
        "prod",
        "production",
        "staging",
        "stage",
        "preprod",
        "pre-production",
        "preview",
    }
)

LOCAL_COMPATIBLE_ENVIRONMENTS = frozenset(
    {
        "",
        "ci",
        "dev",
        "development",
        "local",
        "localhost",
        "test",
        "testing",
    }
)


# BEHAVIORAL_SANDBOX_PYTHON_BACKEND values that run agent code directly on the
# host (app/services/behavioral_sandbox.py). Kept in step with that selector.
UNSAFE_HOST_SANDBOX_BACKENDS = frozenset({"unsafe_host", "host"})


class TrustModeGuardrailError(RuntimeError):
    """Raised when trust mode is unsafe for a production-like deployment."""


# The raw variable the explicitness check reads. Settings.ENVIRONMENT cannot
# be used for this: pydantic fills in its "local" default when the variable
# is absent, erasing the difference between "operator chose local" and
# "variable was dropped".
RUNTIME_ENVIRONMENT_VARIABLE = "ENVIRONMENT"

# Variables the Railway runtime always injects into a deployed container.
# Their presence distinguishes a hosted deployment from a local checkout.
HOSTED_RUNTIME_MARKER_VARS = (
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)


def require_explicit_environment_on_hosted_runtime(
    environment: str | None,
    runtime_env: Mapping[str, str] | None = None,
) -> None:
    """Refuse to boot on a hosted runtime with no explicit ENVIRONMENT.

    An unset ENVIRONMENT is local-compatible by design, which keeps local
    checkouts friction-free — but on a managed platform that same default
    silently disables every production guardrail in
    ``validate_trust_mode_config`` if the variable is ever dropped from the
    service. Railway always injects its RAILWAY_* identifiers, so their
    presence alongside a missing ENVIRONMENT is a misconfiguration, not a
    local run. An explicitly local-compatible value (say, ENVIRONMENT=dev on
    a hosted sandbox) stays allowed: the operator made a visible choice.

    ``environment`` must be the RAW variable (``os.environ`` /
    ``RUNTIME_ENVIRONMENT_VARIABLE``), never ``Settings.ENVIRONMENT``: the
    settings field substitutes its "local" default when the variable is
    absent, which would make the deleted-variable case — the exact failure
    this guard exists for — look explicit.
    """
    if normalize_environment(environment):
        return
    env = os.environ if runtime_env is None else runtime_env
    markers = [
        name for name in HOSTED_RUNTIME_MARKER_VARS if (env.get(name) or "").strip()
    ]
    if markers:
        raise TrustModeGuardrailError(
            "ENVIRONMENT must be set explicitly on a hosted runtime: "
            f"{', '.join(markers)} indicate a Railway deployment, and an "
            "empty ENVIRONMENT would boot with local-compatible defaults — "
            "no production trust guardrails. Set ENVIRONMENT=production (or "
            "an explicit non-production value for a deliberate sandbox)."
        )


def _has_valid_ed25519_private_key(signing_private_key_b64: str) -> bool:
    try:
        raw = base64.b64decode(signing_private_key_b64, validate=True)
        Ed25519PrivateKey.from_private_bytes(raw)
    except (binascii.Error, TypeError, ValueError):
        return False
    return True


def normalize_environment(environment: str | None) -> str:
    return (environment or "").strip().lower().replace("_", "-")


def is_production_like_environment(environment: str | None) -> bool:
    normalized = normalize_environment(environment)
    if normalized in LOCAL_COMPATIBLE_ENVIRONMENTS:
        return False
    return (
        normalized in PRODUCTION_LIKE_ENVIRONMENTS
        or normalized.startswith(("prod-", "production-", "staging-", "stage-"))
        or bool(normalized)
    )


def validate_trust_mode_config(
    *,
    environment: str | None,
    trust_mode_enabled: bool,
    signing_private_key_b64: str | None,
    allow_legacy_unpermitted_mcp: bool,
    debug: bool = False,
    webauthn_allow_mock: bool = False,
    enable_proof_surfaces: bool = True,
    static_dev_api_keys: str = "",
    enable_dev_key_self_provision: bool = False,
    enable_public_mcp_endpoint: bool = False,
    allow_private_network_targets: bool = False,
    allow_unsafe_host_python_sandbox: bool = False,
    behavioral_sandbox_python_backend: str = "",
    redis_url: str = "",
    public_url: str = "",
    database_url: str = "",
    enable_dogfood_tool: bool = False,
    enable_dogfood_second_tool: bool = False,
) -> None:
    """Refuse unsafe deploy postures in production-like environments.

    Production-like environments must run the complete strict trust posture:
    trust mode enabled, a durable signing key configured, legacy unpermitted
    MCP disabled, and all development-only escape hatches disabled.
    """
    production_like = is_production_like_environment(environment)
    violations: list[str] = []
    # redis_url / public_url used to gate anonymous public MCP. That surface is
    # now refused outright in production-like boots, but the parameters stay so
    # existing call sites keep working.
    _ = redis_url, public_url

    if production_like:
        if not trust_mode_enabled:
            violations.append(
                "TRUST_MODE_ENABLED must be true in production-like environments "
                "(permit validation cannot be disabled)"
            )
        configured_signing_key = (signing_private_key_b64 or "").strip()
        if not configured_signing_key:
            violations.append(
                "TRUST_SIGNING_PRIVATE_KEY_B64 is required in production-like "
                "environments"
            )
        elif not _has_valid_ed25519_private_key(configured_signing_key):
            violations.append(
                "TRUST_SIGNING_PRIVATE_KEY_B64 must encode a valid 32-byte "
                "Ed25519 private key in production-like environments"
            )
        if allow_legacy_unpermitted_mcp:
            violations.append(
                "ALLOW_LEGACY_UNPERMITTED_MCP must be false in production-like "
                "environments"
            )
        if debug:
            violations.append(
                "DEBUG must be false in production-like environments "
                "(DEBUG empty-key auth bootstrap is a deploy footgun)"
            )
        if webauthn_allow_mock:
            violations.append(
                "WEBAUTHN_ALLOW_MOCK must be false in production-like environments"
            )
        if enable_proof_surfaces:
            violations.append(
                "ENABLE_PROOF_SURFACES must be false in production-like "
                "environments (mount only CORE_TRUST_ROUTERS + MCP)"
            )
        if (static_dev_api_keys or "").strip():
            violations.append(
                "STATIC_DEV_API_KEYS must be empty in production-like "
                "environments (static development/training keys are "
                "local-only and are never rotated — see "
                "docs/static-dev-api-keys.md)"
            )
        if enable_dev_key_self_provision:
            violations.append(
                "ENABLE_DEV_KEY_SELF_PROVISION must be false in "
                "production-like environments (self-serve dev key minting "
                "is local-only — see docs/static-dev-api-keys.md)"
            )
        if enable_public_mcp_endpoint:
            violations.append(
                "ENABLE_PUBLIC_MCP_ENDPOINT must be false in production-like "
                "environments (anonymous MCP discovery and verification is "
                "local-only; receipt keys stay on "
                "/.well-known/trust-keys.json)"
            )
        if enable_dogfood_tool:
            violations.append(
                "ENABLE_DOGFOOD_TOOL must be false in production-like "
                "environments (partner.notes.write is local/CI dogfood "
                "scaffolding, not a partner integration)"
            )
        if enable_dogfood_second_tool:
            violations.append(
                "ENABLE_DOGFOOD_SECOND_TOOL must be false in production-like "
                "environments (partner.notes.count is CI-only scaffolding)"
            )
        if allow_private_network_targets:
            violations.append(
                "ALLOW_PRIVATE_NETWORK_TARGETS must be false in production-like "
                "environments (it skips the outbound-URL guard's "
                "loopback/RFC1918/link-local checks, re-opening SSRF against "
                "cloud metadata endpoints and internal services)"
            )
        if allow_unsafe_host_python_sandbox:
            violations.append(
                "ALLOW_UNSAFE_HOST_PYTHON_SANDBOX must be false in "
                "production-like environments (host Python execution is not "
                "a sandbox; it is a local-development escape hatch)"
            )
        sandbox_backend = (behavioral_sandbox_python_backend or "").strip().lower()
        if sandbox_backend in UNSAFE_HOST_SANDBOX_BACKENDS:
            violations.append(
                "BEHAVIORAL_SANDBOX_PYTHON_BACKEND must not be "
                f"{sandbox_backend!r} in production-like environments (it "
                "selects the same unsandboxed host Python execution as "
                "ALLOW_UNSAFE_HOST_PYTHON_SANDBOX; use 'docker' or 'disabled')"
            )
        configured_database_url = (database_url or "").strip()
        if not configured_database_url:
            violations.append(
                "DATABASE_URL must be set in production-like environments "
                "(wallets, permits, receipts, and the ledger are relational; "
                "without it get_engine() returns None and the trust plane has "
                "nowhere durable to record what it authorized)"
            )
        elif is_sqlite_url(configured_database_url):
            # STATE_BACKEND already refuses SQLite (see
            # app/core/durable_state.py), but that guard covers the key/value
            # state store, not the ORM engine this URL builds. A deployment
            # setting STATE_BACKEND=redis with REDIS_URL satisfies it while
            # DATABASE_URL stays SQLite -- and the money and permit paths run
            # against the ORM engine, not the state store. Every
            # ``SELECT ... FOR UPDATE`` guarding a balance, a budget, or a
            # counter is silently dropped by SQLAlchemy on SQLite, so the
            # serialization those paths are written to rely on is simply
            # absent. Refuse at boot rather than let concurrent writers
            # discover it against real money.
            violations.append(
                "DATABASE_URL must not be SQLite in production-like "
                "environments: SQLAlchemy silently drops SELECT ... FOR UPDATE "
                "on SQLite, so concurrent charges, budget reservations, and "
                "velocity counters lose the serialization the money and permit "
                "paths depend on. Use PostgreSQL "
                "(postgresql+asyncpg://...). This is a separate control from "
                "STATE_BACKEND, which governs the key/value state store rather "
                "than the ORM engine"
            )

    if violations:
        raise TrustModeGuardrailError("; ".join(violations))


def validate_trust_mode_guardrails(settings: Settings) -> None:
    # Deliberately the raw variable, not settings.ENVIRONMENT: the settings
    # field defaults to "local" when the variable is absent, which is
    # exactly the dropped-variable case this check must catch.
    require_explicit_environment_on_hosted_runtime(
        os.environ.get(RUNTIME_ENVIRONMENT_VARIABLE)
    )
    validate_trust_mode_config(
        environment=settings.ENVIRONMENT,
        trust_mode_enabled=settings.TRUST_MODE_ENABLED,
        signing_private_key_b64=settings.TRUST_SIGNING_PRIVATE_KEY_B64,
        allow_legacy_unpermitted_mcp=settings.ALLOW_LEGACY_UNPERMITTED_MCP,
        debug=settings.DEBUG,
        webauthn_allow_mock=settings.WEBAUTHN_ALLOW_MOCK,
        enable_proof_surfaces=settings.ENABLE_PROOF_SURFACES,
        static_dev_api_keys=settings.STATIC_DEV_API_KEYS,
        enable_dev_key_self_provision=settings.ENABLE_DEV_KEY_SELF_PROVISION,
        enable_public_mcp_endpoint=settings.ENABLE_PUBLIC_MCP_ENDPOINT,
        allow_private_network_targets=settings.ALLOW_PRIVATE_NETWORK_TARGETS,
        allow_unsafe_host_python_sandbox=settings.ALLOW_UNSAFE_HOST_PYTHON_SANDBOX,
        behavioral_sandbox_python_backend=settings.BEHAVIORAL_SANDBOX_PYTHON_BACKEND,
        redis_url=settings.REDIS_URL,
        public_url=settings.PUBLIC_URL,
        database_url=settings.DATABASE_URL,
        enable_dogfood_tool=settings.ENABLE_DOGFOOD_TOOL,
        enable_dogfood_second_tool=settings.ENABLE_DOGFOOD_SECOND_TOOL,
    )


def describe_permissive_trust_mode(
    *,
    trust_mode_enabled: bool,
    allow_legacy_unpermitted_mcp: bool,
) -> str | None:
    """Describe a permissive trust-mode posture, or return None when strict.

    The shipped defaults are `TRUST_MODE_ENABLED=true` and
    `ALLOW_LEGACY_UNPERMITTED_MCP=false`. Any deviation is an explicit
    operator opt-out and should be surfaced at startup so demos and
    incremental migrations cannot drift into permissive territory by
    accident.
    """
    if trust_mode_enabled and not allow_legacy_unpermitted_mcp:
        return None
    parts: list[str] = []
    if not trust_mode_enabled:
        parts.append("TRUST_MODE_ENABLED=false (no permit validation)")
    if allow_legacy_unpermitted_mcp:
        parts.append(
            "ALLOW_LEGACY_UNPERMITTED_MCP=true (ungoverned MCP calls accepted)"
        )
    return "; ".join(parts)


def warn_if_trust_mode_permissive(settings: Settings) -> None:
    """Log a loud warning when the trust plane is running in opt-out mode.

    Called once at startup, after `validate_trust_mode_guardrails`. In
    production-like environments the validator has already refused to boot
    under a permissive posture, so this only fires in local/dev/test
    environments that explicitly opted out — exactly when we want a visible
    reminder that ungoverned MCP calls are accepted.
    """
    description = describe_permissive_trust_mode(
        trust_mode_enabled=settings.TRUST_MODE_ENABLED,
        allow_legacy_unpermitted_mcp=settings.ALLOW_LEGACY_UNPERMITTED_MCP,
    )
    if description is None:
        return
    logger.warning(
        "trust_mode_permissive: %s. The trust plane is in legacy/opt-out "
        "mode; production deployments must set TRUST_MODE_ENABLED=true and "
        "ALLOW_LEGACY_UNPERMITTED_MCP=false.",
        description,
    )
