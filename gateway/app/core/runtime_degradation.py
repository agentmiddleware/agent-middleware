"""
Track silent infrastructure fallbacks so operators can see them.

Rate limiting and durable state may fall back to in-memory when Redis
(or another configured backend) is unavailable. That is convenient for
local/dev but dangerous in multi-instance production, so this module
surfaces the degradation on /health/dependencies and supports
fail-closed rate limiting in production-like environments that configure
REDIS_URL.

A production-like deployment that never sets REDIS_URL has no shared limiter
to fall back from: every worker and replica counts its own requests. That
state is reported as degraded with a stable reason code, and logged once at
startup, instead of looking healthy.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import get_settings
from .trust_mode import is_production_like_environment

logger = logging.getLogger(__name__)

# Stable reason codes published in ``reasons`` on /health/dependencies. They
# are part of the operator-facing contract: add new codes, do not rename these.
REASON_RATE_LIMITER_MEMORY_FALLBACK = "rate_limiter_memory_fallback"
REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED = "rate_limiter_redis_not_configured"
REASON_DURABLE_STATE_MEMORY_FALLBACK = "durable_state_memory_fallback"

_rate_limiter_memory_fallback = False
_memory_only_limiter_warned = False
_durable_state_fell_back = False
_durable_state_intended: str | None = None


def mark_rate_limiter_memory_fallback() -> None:
    global _rate_limiter_memory_fallback
    _rate_limiter_memory_fallback = True


def mark_durable_state_fell_back(intended_backend: str) -> None:
    global _durable_state_fell_back, _durable_state_intended
    _durable_state_fell_back = True
    _durable_state_intended = intended_backend


def rate_limiter_memory_only_in_production_like() -> bool:
    """True when this process counts rate limits in memory by configuration.

    That is a production-like environment with no ``REDIS_URL``. Outside
    production-like environments an in-memory limiter is the expected local
    default, and with ``REDIS_URL`` set the shared limiter is in use.
    """
    settings = get_settings()
    return bool(
        is_production_like_environment(settings.ENVIRONMENT)
        and not settings.REDIS_URL.strip()
    )


def warn_if_rate_limiter_memory_only() -> bool:
    """Log one startup warning for a memory-only limiter in production.

    Returns True when this call logged. Later calls in the same process, and
    every call where the condition does not hold, log nothing.
    """
    global _memory_only_limiter_warned
    if _memory_only_limiter_warned or not rate_limiter_memory_only_in_production_like():
        return False
    _memory_only_limiter_warned = True
    logger.warning(
        "%s: ENVIRONMENT is production-like and REDIS_URL is not set, so rate "
        "limits are counted per process. Each worker and replica keeps its own "
        "counters, so every limit is multiplied by the process count, and the "
        "limiter does not fail closed when counting is unavailable. "
        "/health/dependencies reports runtime_degradation with this reason. "
        "Set REDIS_URL to a shared Redis for shared limits.",
        REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED,
    )
    return True


def reset_runtime_degradation() -> None:
    """Test helper: clear degradation flags between cases."""
    global _rate_limiter_memory_fallback, _durable_state_fell_back
    global _durable_state_intended, _memory_only_limiter_warned
    _memory_only_limiter_warned = False
    _rate_limiter_memory_fallback = False
    _durable_state_fell_back = False
    _durable_state_intended = None


def get_runtime_degradation() -> dict[str, Any]:
    """Snapshot of active fallbacks for health / operator surfaces."""
    settings = get_settings()
    redis_configured = bool(settings.REDIS_URL.strip())
    memory_only = rate_limiter_memory_only_in_production_like()
    reasons: list[str] = []
    if _rate_limiter_memory_fallback:
        reasons.append(REASON_RATE_LIMITER_MEMORY_FALLBACK)
    if memory_only:
        reasons.append(REASON_RATE_LIMITER_REDIS_NOT_CONFIGURED)
    if _durable_state_fell_back:
        reasons.append(REASON_DURABLE_STATE_MEMORY_FALLBACK)
    return {
        "degraded": bool(reasons),
        "reasons": reasons,
        "rate_limiter": {
            "redis_configured": redis_configured,
            "memory_only_in_production_like": memory_only,
            "using_memory_fallback": _rate_limiter_memory_fallback,
            "backend": (
                "memory"
                if _rate_limiter_memory_fallback or not redis_configured
                else "redis"
            ),
        },
        "durable_state": {
            "fell_back_to_memory": _durable_state_fell_back,
            "intended_backend": _durable_state_intended,
        },
    }
