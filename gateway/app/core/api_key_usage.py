"""Metadata-only HTTP usage events for database-backed API keys."""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import CREDENTIAL_ACCEPTANCE
from .config import get_settings
from .time import to_naive_utc, utc_now
from ..db.database import get_session_factory
from ..db.models import APIKeyUsageEventModel

logger = logging.getLogger(__name__)
_USAGE_WRITE_TIMEOUT_SECONDS = 2.0
_MAX_PRUNE_BATCH_SIZE = 1_000


async def prune_api_key_usage_events(
    *, batch_size: int = _MAX_PRUNE_BATCH_SIZE, now: datetime | None = None
) -> int:
    """Delete at most one batch older than retention; never runs automatically."""
    retention_days = get_settings().API_KEY_USAGE_RETENTION_DAYS
    if not retention_days:
        return 0
    if not 1 <= batch_size <= _MAX_PRUNE_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {_MAX_PRUNE_BATCH_SIZE}")
    cutoff = to_naive_utc(now) if now is not None else utc_now()
    cutoff -= timedelta(days=retention_days)
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "DELETE FROM api_key_usage_events WHERE id IN ("
                "SELECT id FROM api_key_usage_events WHERE created_at < :cutoff "
                "ORDER BY created_at, id LIMIT :batch_size)"
            ),
            {"cutoff": cutoff, "batch_size": batch_size},
        )
        await session.commit()
        return cast(Any, result).rowcount


async def record_api_key_usage_event(
    *,
    key_id: str,
    tenant: str | None,
    method: str,
    endpoint: str,
    status_code: int,
    latency_ms: int,
) -> None:
    """Append one event using a separate session after the response."""
    async with get_session_factory()() as session:
        session.add(
            APIKeyUsageEventModel(
                id=str(uuid4()),
                key_id=key_id,
                tenant=tenant,
                method=method,
                endpoint=endpoint,
                status_code=status_code,
                latency_ms=latency_ms,
            )
        )
        await session.commit()


class APIKeyUsageMiddleware:
    """Record the resolved key and final HTTP status once per API request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # RateLimitMiddleware sets this shared holder before entering this
        # layer; auth fills its key identity in the child task after lookup.
        acceptance = CREDENTIAL_ACCEPTANCE.get()
        if acceptance is None:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # route.path is the registered template. Never substitute scope's
            # raw path, which could contain IDs, query values, or user text.
            endpoint = getattr(scope.get("route"), "path", None)
            if acceptance.key_id and endpoint:
                latency_ms = max(0, int((time.perf_counter() - started) * 1000))
                try:
                    await asyncio.wait_for(
                        record_api_key_usage_event(
                            key_id=acceptance.key_id,
                            tenant=acceptance.tenant,
                            method=scope.get("amw_original_method", scope["method"]),
                            endpoint=endpoint,
                            status_code=status_code,
                            latency_ms=latency_ms,
                        ),
                        timeout=_USAGE_WRITE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001 - usage cannot fail API calls
                    logger.warning(
                        "api_key_usage_event_write_failed error_type=%s",
                        type(exc).__name__,
                    )
