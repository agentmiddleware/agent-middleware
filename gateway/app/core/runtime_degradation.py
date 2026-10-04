"""
Track silent infrastructure fallbacks so operators can see them.

Rate limiting and durable state may fall back to in-memory when Redis
(or another configured backend) is unavailable. That is convenient for
local/dev but dangerous in multi-instance production — this module
surfaces the degradation on /health/dependencies and supports
fail-closed rate limiting in production-like environments.
"""

from __future__ import annotations

from typing import Any

from .config import get_settings

_rate_limiter_memory_fallback = False
_durable_state_fell_back = False
_durable_state_intended: str | None = None


def mark_rate_limiter_memory_fallback() -> None:
    global _rate_limiter_memory_fallback
    _rate_limiter_memory_fallback = True


def mark_durable_state_fell_back(intended_backend: str) -> None:
    global _durable_state_fell_back, _durable_state_intended
    _durable_state_fell_back = True
    _durable_state_intended = intended_backend


def reset_runtime_degradation() -> None:
    """Test helper: clear degradation flags between cases."""
    global _rate_limiter_memory_fallback, _durable_state_fell_back
    global _durable_state_intended
    _rate_limiter_memory_fallback = False
    _durable_state_fell_back = False
    _durable_state_intended = None


def get_runtime_degradation() -> dict[str, Any]:
    """Snapshot of active fallbacks for health / operator surfaces."""
    settings = get_settings()
    redis_configured = bool(settings.REDIS_URL.strip())
    degraded = bool(_rate_limiter_memory_fallback or _durable_state_fell_back)
    return {
        "degraded": degraded,
        "rate_limiter": {
            "redis_configured": redis_configured,
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
