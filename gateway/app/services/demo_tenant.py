"""Anonymous demo-key issuance with isolated demo wallets and abuse limits."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlmodel import col

from ..core.config import get_settings
from ..core.rate_limiter import enforce_redis_timeouts
from ..core.time import utc_now
from ..core.time import to_naive_utc
from ..core.trust_mode import is_production_like_environment
from ..db.database import get_session_factory
from ..db.models import APIKeyModel
from .agent_money import get_agent_money
from .api_key_service import get_api_key_service
from .ownership import CallerIdentity

logger = logging.getLogger("app.demo_tenant")

DEMO_TENANT_LABEL = "demo"
ISSUE_SCOPE_IP = "ip"
ISSUE_SCOPE_GLOBAL_HOUR = "global_hour"
ISSUE_SCOPE_GLOBAL_DAY = "global_day"
ISSUE_SCOPE_LIVE_KEYS = "live_keys"
ISSUANCE_UNAVAILABLE = "demo_issuance_unavailable"
ISSUANCE_RATE_LIMITED = "demo_issuance_rate_limited"

_memory_counters: dict[str, tuple[int, float]] = {}
_memory_counters_lock = asyncio.Lock()


def hash_client_id(client_id: str) -> str:
    """One-way identifier for counters and logs; never retain the raw IP."""
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()


def log_demo_event(event: str, **fields: Any) -> None:
    parts = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("%s %s", event, parts)


async def _incr_counter(key: str, ttl_seconds: int) -> int | None:
    """Use shared Redis counters when configured, otherwise local counters."""
    redis_url = get_settings().REDIS_URL.strip()
    if redis_url:
        try:
            import redis.asyncio as redis

            client = enforce_redis_timeouts(redis.from_url(redis_url))
            try:
                count = int(await client.incr(key))
                if count == 1:
                    await client.expire(key, ttl_seconds)
                return count
            finally:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 - best-effort close
                    pass
        except Exception:  # noqa: BLE001 - caller fails closed in production
            return None
    now = time.time()
    async with _memory_counters_lock:
        count, expires = _memory_counters.get(key, (0, 0.0))
        if expires <= now:
            count = 0
        count += 1
        _memory_counters[key] = (count, now + ttl_seconds)
        return count


async def check_issuance_limits(client_id: str) -> tuple[str | None, int | None]:
    """Return the first exhausted issuance scope and its configured limit."""
    settings = get_settings()
    production_like = is_production_like_environment(settings.ENVIRONMENT)
    # Production replicas must share issuance counters. Fail closed even if
    # startup validation was bypassed or configuration changed after boot.
    if production_like and not settings.REDIS_URL.strip():
        return "unavailable", None

    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%Y-%m-%d-%H")
    checks = (
        (
            f"demo:issue:ip:{hash_client_id(client_id)}:{day}",
            24 * 3600,
            settings.DEMO_ISSUE_PER_IP_PER_DAY,
            ISSUE_SCOPE_IP,
        ),
        (
            f"demo:issue:global-hour:{hour}",
            2 * 3600,
            settings.DEMO_ISSUE_GLOBAL_PER_HOUR,
            ISSUE_SCOPE_GLOBAL_HOUR,
        ),
        (
            f"demo:issue:global-day:{day}",
            2 * 24 * 3600,
            settings.DEMO_ISSUE_GLOBAL_PER_DAY,
            ISSUE_SCOPE_GLOBAL_DAY,
        ),
    )
    for counter_key, ttl, limit, scope in checks:
        count = await _incr_counter(counter_key, ttl)
        if count is None:
            if production_like:
                return "unavailable", None
            continue
        if count > limit:
            return scope, limit

    live = await count_live_demo_keys()
    if live is None:
        if production_like:
            return "unavailable", None
    elif live >= settings.DEMO_MAX_LIVE_KEYS:
        return ISSUE_SCOPE_LIVE_KEYS, settings.DEMO_MAX_LIVE_KEYS
    return None, None


async def count_live_demo_keys() -> int | None:
    """Count active, unexpired demo keys; None signals a database failure."""
    try:
        now = utc_now().replace(tzinfo=None)
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(APIKeyModel)
                .where(
                    col(APIKeyModel.tenant) == DEMO_TENANT_LABEL,
                    col(APIKeyModel.status) == "active",
                    col(APIKeyModel.revoked_at).is_(None),
                    or_(
                        col(APIKeyModel.expires_at).is_(None),
                        col(APIKeyModel.expires_at) >= now,
                    ),
                )
            )
            return int(result.scalar_one())
    except Exception:  # noqa: BLE001 - production callers fail closed
        logger.warning("demo_count_live_keys_failed")
        return None


def _referrer_host(referrer: str | None) -> str | None:
    if not referrer:
        return referrer
    try:
        parts = urlsplit(referrer)
    except ValueError:
        return referrer
    if parts.scheme and parts.hostname:
        return parts.hostname
    return referrer


async def demo_key_funnel(
    since: datetime | None = None, until: datetime | None = None
) -> list[dict[str, Any]]:
    """Group issued and first-used demo keys by acquisition source."""
    query = (
        select(
            col(APIKeyModel.utm_source),
            col(APIKeyModel.utm_medium),
            col(APIKeyModel.utm_campaign),
            col(APIKeyModel.referrer),
            func.count(),
            func.count(col(APIKeyModel.first_used_at)),
            func.min(col(APIKeyModel.created_at)),
            func.max(col(APIKeyModel.created_at)),
        )
        .where(col(APIKeyModel.tenant) == DEMO_TENANT_LABEL)
        .group_by(
            col(APIKeyModel.utm_source),
            col(APIKeyModel.utm_medium),
            col(APIKeyModel.utm_campaign),
            col(APIKeyModel.referrer),
        )
    )
    if since is not None:
        query = query.where(col(APIKeyModel.created_at) >= to_naive_utc(since))
    if until is not None:
        query = query.where(col(APIKeyModel.created_at) < to_naive_utc(until))

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(query)
        raw_rows = result.all()

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for src, medium, campaign, referrer, issued, used, first, last in raw_rows:
        key = (src or "(none)", medium, campaign, _referrer_host(referrer))
        row = merged.get(key)
        if row is None:
            merged[key] = {
                "utm_source": key[0],
                "utm_medium": medium,
                "utm_campaign": campaign,
                "referrer_host": key[3],
                "keys_issued": int(issued),
                "keys_used": int(used),
                "first_issued_at": first,
                "last_issued_at": last,
            }
            continue
        row["keys_issued"] += int(issued)
        row["keys_used"] += int(used)
        row["first_issued_at"] = min(row["first_issued_at"], first)
        row["last_issued_at"] = max(row["last_issued_at"], last)
    return sorted(
        merged.values(),
        key=lambda r: (-r["keys_issued"], r["utm_source"], r["referrer_host"] or ""),
    )


async def mint_demo_keypair(
    *, attribution: dict[str, str] | None = None
) -> dict[str, Any]:
    """Create a demo sponsor, funded agent wallet, and key in one transaction."""
    settings = get_settings()
    money = get_agent_money()
    keys = get_api_key_service()
    agent_id = f"demo-{uuid4().hex[:8]}"
    demo_tools = settings.demo_allowed_tools_list

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            sponsor = await money.create_sponsor_wallet(
                sponsor_name=f"demo-self-provision:{agent_id}",
                email=f"{agent_id}@demo.local",
                initial_credits=settings.DEMO_WALLET_CREDITS,
                require_kyc=False,
                tenant=DEMO_TENANT_LABEL,
                session=session,
            )
            agent = await money.create_agent_wallet(
                sponsor_wallet_id=sponsor.wallet_id,
                agent_id=agent_id,
                budget_credits=settings.DEMO_WALLET_CREDITS,
                daily_limit=settings.DEMO_WALLET_DAILY_LIMIT,
                session=session,
                caller=CallerIdentity.for_wallet(sponsor.wallet_id),
            )
            key = await keys.create_key(
                wallet_id=agent.wallet_id,
                key_name="self-provisioned-demo",
                expires_in_days=settings.DEMO_KEY_TTL_DAYS,
                max_uses=settings.DEMO_KEY_MAX_USES,
                allowed_tools=demo_tools if demo_tools else None,
                tenant=DEMO_TENANT_LABEL,
                attribution=attribution,
                session=session,
            )
    return {
        "api_key": key["api_key"],
        "key_id": key["key_id"],
        "key_prefix": key["key_prefix"],
        "wallet_id": agent.wallet_id,
        "sponsor_wallet_id": sponsor.wallet_id,
        "tenant": DEMO_TENANT_LABEL,
        "allowed_tools": demo_tools if demo_tools else None,
        "expires_at": key["expires_at"],
        "max_uses": key["max_uses"],
        "budget_credits": settings.DEMO_WALLET_CREDITS,
    }
