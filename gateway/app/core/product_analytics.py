"""Fail-safe, non-blocking PostHog product tracking.

Tracking is off unless ``POSTHOG_API_KEY`` is set in the environment. When it
is unset every function here is a no-op: no client, no network, no task.

Events go to PostHog's public ``/capture/`` endpoint over httpx (already a
gateway dependency), so no PostHog SDK is installed. Each send is scheduled as
a background task with a short timeout, and every exception is caught and
logged at debug level without the API key, so tracking can never fail or slow
a request.

The distinct id is the account's root (sponsor) wallet id. Only
:func:`identify` sends personal data: the ``email`` person property.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://us.i.posthog.com"
SEND_TIMEOUT_SECONDS = 2.0

EVENT_SIGNUP = "signup"
EVENT_ACTIVATION = "activation"

_pending: set[asyncio.Task[None]] = set()


def _config() -> tuple[str, str] | None:
    api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
    if not api_key:
        return None
    host = os.environ.get("POSTHOG_HOST", "").strip() or DEFAULT_HOST
    return api_key, host.rstrip("/")


def is_enabled() -> bool:
    return _config() is not None


async def _post(host: str, payload: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS) as client:
        response = await client.post(f"{host}/capture/", json=payload)
        response.raise_for_status()


async def _send(host: str, payload: dict[str, Any]) -> None:
    try:
        await _post(host, payload)
    except Exception as exc:  # noqa: BLE001 - tracking must never raise
        logger.debug(
            "product_analytics_send_failed event=%s error=%s",
            payload.get("event"),
            type(exc).__name__,
        )


def _enqueue(event: str, distinct_id: str, properties: dict[str, Any]) -> None:
    try:
        config = _config()
        if config is None or not distinct_id:
            return
        api_key, host = config
        payload = {
            "api_key": api_key,
            "event": event,
            "distinct_id": distinct_id,
            "properties": {**properties, "$lib": "agent-middleware-gateway"},
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (sync caller): drop rather than block
        task = loop.create_task(_send(host, payload))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception as exc:  # noqa: BLE001 - tracking must never raise
        logger.debug(
            "product_analytics_enqueue_failed event=%s error=%s",
            event,
            type(exc).__name__,
        )


def identify(distinct_id: str, *, email: str | None) -> None:
    """Attach the email person property to the account."""
    props: dict[str, Any] = {}
    if email:
        props["$set"] = {"email": email}
    _enqueue("$identify", distinct_id, props)


def capture(event: str, distinct_id: str, **properties: Any) -> None:
    _enqueue(event, distinct_id, properties)


def track_signup(distinct_id: str, *, method: str, email: str | None = None) -> None:
    """identify (with email when known) then a ``signup`` event."""
    identify(distinct_id, email=email)
    capture(EVENT_SIGNUP, distinct_id, method=method)


def track_activation(distinct_id: str, *, key_id: str, wallet_id: str) -> None:
    capture(EVENT_ACTIVATION, distinct_id, key_id=key_id, wallet_id=wallet_id)


async def drain_pending() -> None:
    """Await in-flight sends (tests and graceful shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
