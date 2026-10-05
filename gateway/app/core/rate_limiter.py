"""
Rate Limiting Middleware
========================
Enforces per-API-key request limits using a sliding window counter.
Returns standard rate limit headers on every response.
"""

import asyncio
import hashlib
import ipaddress
import logging
import os
import time
from collections import defaultdict
from typing import Any

import redis.asyncio as redis
from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .auth import (
    CREDENTIAL_ACCEPTANCE,
    CREDENTIAL_REJECTED_HEADER,
    CredentialAcceptance,
    _parse_bearer_authorization,
)
from .config import get_settings
from .jwt import JWTError, get_jwt_service
from .oidc_iga import is_iga_issuer_token
from .runtime_degradation import mark_rate_limiter_memory_fallback
from .trust_mode import is_production_like_environment

settings = get_settings()
logger = logging.getLogger(__name__)

# Bound every Redis round trip. Without these, a connection left half-open by a
# Redis restart blocks commands indefinitely, and the cached client is never
# replaced. health_check_interval makes redis-py PING an idle connection before
# reusing it, so a dead socket is noticed before a request depends on it.
REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS: float = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0
REDIS_HEALTH_CHECK_INTERVAL_SECONDS: int = 15

_PUBLIC_MCP_PATH = "/mcp/public"
_PUBLIC_MCP_BUCKET_PREFIX = "route:mcp-public"
_PUBLIC_MCP_GLOBAL_LIMIT_MULTIPLIER = 10

# Shared ceiling on requests whose credentials the app did not accept —
# refused, or never checked because the route does not authenticate. The
# per-key bucket is chosen from a caller-supplied header before the key has
# been verified, so without this a caller rotating a fresh X-API-Key per
# request would mint a fresh budget every time. Ten times the per-key limit,
# matching the public-MCP global bucket: wide enough that a partner whose key
# is briefly wrong is not throttled by it, narrow enough that key rotation is
# bounded.
_PREAUTH_BUCKET_PREFIX = "preauth:rejected-credentials"
_PREAUTH_LIMIT_MULTIPLIER = 10

# Prefix hashed with a presented X-API-Key to name its bucket; see
# _api_key_bucket.
_API_KEY_BUCKET_DOMAIN = b"agent-middleware-api:rate-limit-bucket:v1\x00"

# In-memory fallback bookkeeping: bucket keys are caller-controlled, so the
# dict is swept (at most once per window) once it grows past this many
# buckets. Without it, every distinct key value ever presented keeps a list
# for the life of the process.
_MEMORY_BUCKET_SWEEP_THRESHOLD = 1024


def rate_limit_discovery() -> dict[str, Any]:
    """Describe the limit ``RateLimitMiddleware`` actually enforces.

    One budget per 60-second window, keyed by the ``X-API-Key`` header value.
    Requests without a key share a single 'anonymous' bucket. There is no burst
    allowance and no per-partner override; ``RATE_LIMIT_PER_MINUTE`` is the
    only knob, so this payload is derived from it rather than hardcoded. A
    burst of 40 requests will not 429: the 121st counted request in a 60-second
    window (at the default of 120) is the first that does.

    The budget and the window length are published; the counting algorithm is
    not, because it is not the same on both backends. The shared Redis limiter
    counts fixed 60-second buckets, and the in-memory fallback — reached only
    where a Redis outage does not fail closed, i.e. never in a production-like
    environment — counts a rolling 60 seconds, which is strictly tighter.
    ``window_accounting`` names that difference rather than letting a reader
    infer one algorithm from ``window_seconds``.

    Requests whose credentials the app does not accept — a ``401``, the
    ``403`` an unknown API key is answered with, or any request to a route
    that never authenticates — are additionally charged to a shared per-client
    bucket (``rejected_credentials_scope``); see
    ``RateLimitMiddleware.dispatch``. An authenticated caller denied on scope
    is not: in a trust plane, denials are ordinary traffic.
    """
    cfg = get_settings()
    return {
        "requests_per_minute": cfg.RATE_LIMIT_PER_MINUTE,
        "window_seconds": 60,
        "window_accounting": "fixed_window_shared_rolling_window_in_memory",
        "scope": "per_api_key",
        "unauthenticated_scope": "shared_anonymous_bucket",
        "rejected_credentials_scope": "shared_per_client_bucket",
        "rejected_credentials_multiplier": _PREAUTH_LIMIT_MULTIPLIER,
        "burst_allowance": 0,
        "per_partner_override": False,
        "headers": [
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
    }


def _presented_rate_identity(request: Request) -> str | None:
    """Mirror credential precedence without consuming authentication budgets.

    Internal JWTs share their originating key's bucket across token rotation.
    An enterprise bearer is attribution only; its accompanying API key remains
    the credential. Invalid credentials remain subject to the preauth ceiling.
    """
    api_key = request.headers.get(settings.API_KEY_HEADER, "").strip() or None
    authorization = request.headers.get("authorization")
    token = None
    if authorization is not None:
        try:
            token = _parse_bearer_authorization(authorization)
        except HTTPException:
            return f"invalid-authorization:{authorization}"
        if api_key and is_iga_issuer_token(token):
            return api_key
    elif api_key is not None:
        candidate = api_key.removeprefix("Bearer ").strip()
        if candidate.count(".") == 2:
            token = candidate
    if token is None:
        return api_key
    try:
        payload = get_jwt_service().verify_access_token(token)
    except JWTError:
        return f"invalid-jwt:{token}"
    return f"jwt:{payload.key_id}"


def _client_id(request: Request) -> str:
    """Return a non-spoofable client identifier for the supported ingress.

    Railway documents ``X-Real-IP`` as an ingress-populated client address.
    Trust it only when Railway's injected environment marker is present, and
    only when exactly one canonical IP value was supplied. Other deployments
    retain the ASGI peer address and ignore all caller-provided forwarding
    headers.
    """

    peer_host = request.client.host if request.client else "unknown"
    if not os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip():
        return peer_host

    values = request.headers.getlist("x-real-ip")
    if len(values) != 1:
        return peer_host
    raw = values[0].strip()
    if not raw or "," in raw or "%" in raw:
        return peer_host
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        return peer_host


def _api_key_bucket(presented_key: str | None) -> str:
    """Name a presented key's bucket without putting the key itself in it.

    Bucket names become Redis key names, so the raw header value would write
    every API key that called in the last minute into the limiter's store in
    plaintext. A digest still gives each distinct value its own bucket. It is
    domain-separated so the name is not also the ``key_hash`` the API-key
    table stores for the same key.
    """
    if presented_key is None:
        return "anonymous"
    digest = hashlib.sha256(
        _API_KEY_BUCKET_DOMAIN + presented_key.encode("utf-8")
    ).hexdigest()
    return f"api_key_sha256:{digest}"


class RateLimiterUnavailable(RuntimeError):
    """Raised when Redis rate limiting is required but unavailable."""


def _credentials_were_rejected(response: Response) -> bool:
    """Say whether the app refused this request's credentials, and unmark it.

    A 401 is always the app declining to authenticate the caller. A 403 is
    ambiguous — an unknown API key is refused with 403, but so is an
    authenticated caller denied on scope, whose denials are ordinary
    governed-loop traffic — so authentication is distinguished by the internal
    header ``app.core.auth`` sets. The header is removed here, in the innermost
    middleware, so it never reaches the client.
    """
    marked = CREDENTIAL_REJECTED_HEADER in response.headers
    if marked:
        del response.headers[CREDENTIAL_REJECTED_HEADER]
    return marked or response.status_code == 401


def _rate_limited_response(limit: int, reset_in: int, message: str) -> JSONResponse:
    """Build the one 429 shape every exhausted bucket answers with."""
    return JSONResponse(
        status_code=429,
        content={
            "detail": {
                "error": "rate_limited",
                "message": message,
                "retry_after_seconds": reset_in,
            }
        },
        headers={
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset_in),
            "Retry-After": str(reset_in),
        },
    )


async def _close_quietly(client: redis.Redis) -> None:
    """Close a Redis client without letting a dead socket block or raise."""
    try:
        await asyncio.wait_for(client.aclose(), timeout=REDIS_SOCKET_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Closing a Redis rate-limiter client failed.", exc_info=True)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter keyed by API key.

    Headers returned on every response:
    - X-RateLimit-Limit: max requests per window
    - X-RateLimit-Remaining: requests left in current window
    - X-RateLimit-Reset: seconds until the window resets
    """

    def __init__(self, app, requests_per_minute: int | None = None):
        super().__init__(app)
        self.limit = requests_per_minute or settings.RATE_LIMIT_PER_MINUTE
        self.preauth_limit = self.limit * _PREAUTH_LIMIT_MULTIPLIER
        self.window = 60.0  # seconds
        self._redis_url = settings.REDIS_URL.strip()
        self._redis: redis.Redis | None = None
        self._redis_lock = asyncio.Lock()
        self._redis_warned = False

        # key -> list of timestamps
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._last_sweep = 0.0

    def _fail_closed_on_redis_outage(self) -> bool:
        """Production-like + REDIS_URL configured → no silent memory fallback."""
        return bool(
            self._redis_url and is_production_like_environment(settings.ENVIRONMENT)
        )

    async def _get_redis(self) -> redis.Redis | None:
        if not self._redis_url:
            return None
        if self._redis is not None:
            return self._redis

        async with self._redis_lock:
            if self._redis is not None:
                return self._redis
            client = None
            try:
                client = redis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
                    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                    health_check_interval=REDIS_HEALTH_CHECK_INTERVAL_SECONDS,
                )
                await client.ping()
                self._redis = client
                if self._redis_warned:
                    logger.info("Redis rate limiter reconnected.")
                # Re-arm the warning so the next outage is logged too.
                self._redis_warned = False
            except Exception:
                if client is not None:
                    await _close_quietly(client)
                mark_rate_limiter_memory_fallback()
                if not self._redis_warned:
                    logger.exception(
                        "Redis rate limiter unavailable; %s.",
                        (
                            "failing closed (production-like)"
                            if self._fail_closed_on_redis_outage()
                            else "falling back to in-memory limiter"
                        ),
                    )
                    self._redis_warned = True
                self._redis = None
            return self._redis

    async def _reset_redis(self) -> None:
        """Drop the cached client so the next request opens a fresh connection.

        Called when a command fails on an already-established connection: a
        Redis restart leaves the cached client pointing at a dead (or hung)
        socket, and without a reset every later request keeps failing on it
        long after Redis itself has recovered.
        """
        async with self._redis_lock:
            client, self._redis = self._redis, None
        if client is not None:
            await _close_quietly(client)

    async def _check_limit_with_redis(
        self,
        bucket_key: str,
        now: float,
        *,
        limit: int | None = None,
    ) -> tuple[bool, int, int]:
        """
        Returns (limited, remaining, reset_in_seconds).
        """
        effective_limit = self.limit if limit is None else limit
        client = await self._get_redis()
        if client is None:
            if self._fail_closed_on_redis_outage():
                raise RateLimiterUnavailable("redis_rate_limiter_unavailable")
            if self._redis_url:
                mark_rate_limiter_memory_fallback()
            return await self._check_limit_in_memory(
                bucket_key,
                now,
                limit=effective_limit,
            )

        window_size = int(self.window)
        bucket_start = int(now // window_size) * window_size
        reset_in = max(1, (bucket_start + window_size) - int(now))
        key = f"rate_limit:{bucket_key}:{bucket_start}"

        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_size + 2)

        remaining = max(0, effective_limit - int(count))
        limited = int(count) > effective_limit
        return limited, remaining, reset_in

    def _preauth_bucket(self, request: Request) -> str:
        """Name the shared bucket that unaccepted credentials are charged to."""
        namespace = (
            f"{settings.STATE_NAMESPACE}:{settings.PUBLIC_URL or settings.APP_NAME}"
        )
        return f"{namespace}:{_PREAUTH_BUCKET_PREFIX}:client:{_client_id(request)}"

    async def _reserve(
        self,
        bucket_key: str,
        now: float,
        limit: int,
        *,
        force_memory: bool = False,
    ) -> tuple[bool, int]:
        """Atomically take one unit of a bucket's budget.

        Returns ``(exhausted, reset_in_seconds)``. ``exhausted`` means the unit
        was not taken and the caller must refuse the request; otherwise the
        caller holds a reservation it either keeps (the request turned out to
        be one this bucket counts) or hands back with ``_release``.

        Reserving up front is what makes a ceiling hold under concurrency.
        Reading the bucket now and charging it after the response would let
        every request already in flight pass the same read, so the ceiling
        would only bound traffic that arrives one request at a time.

        ``force_memory`` is for the caller that has already seen Redis fail on
        this request and fallen back, so the ceiling keeps applying instead of
        disappearing for the duration of the outage.
        """
        window_size = int(self.window)
        client = None if force_memory else await self._get_redis()
        if client is None:
            if not force_memory and self._fail_closed_on_redis_outage():
                raise RateLimiterUnavailable("redis_rate_limiter_unavailable")
            window_start = now - self.window
            async with self._lock:
                # .get, not [], so a refused reservation never creates a bucket.
                live = [
                    ts for ts in self._requests.get(bucket_key, ()) if ts > window_start
                ]
                if len(live) >= limit:
                    if bucket_key in self._requests:
                        self._requests[bucket_key] = live
                    return True, max(1, int(live[0] + self.window - now) + 1)
                live.append(now)
                self._requests[bucket_key] = live
            return False, window_size

        bucket_start = int(now // window_size) * window_size
        reset_in = max(1, (bucket_start + window_size) - int(now))
        key = f"rate_limit:{bucket_key}:{bucket_start}"
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_size + 2)
        if count > limit:
            # Refused, so hand the unit straight back: the counter has to keep
            # meaning "reservations currently held", or a caller that keeps
            # knocking would inflate it past any hope of draining.
            await self._give_back(client, key)
            return True, reset_in
        return False, reset_in

    async def _release(
        self,
        bucket_key: str,
        now: float,
        *,
        force_memory: bool = False,
    ) -> None:
        """Hand back a reservation the request turned out not to owe.

        Best effort: the response has already been produced, so a limiter
        failure here must not turn an answered request into an error. A
        reservation that cannot be handed back expires with its window.
        """
        try:
            window_size = int(self.window)
            client = None if force_memory else await self._get_redis()
            if client is None:
                async with self._lock:
                    timestamps = self._requests.get(bucket_key)
                    if timestamps is not None:
                        try:
                            timestamps.remove(now)
                        except ValueError:
                            # Already swept or expired: nothing left to give back.
                            pass
                return
            bucket_start = int(now // window_size) * window_size
            try:
                await self._give_back(client, f"rate_limit:{bucket_key}:{bucket_start}")
            except Exception:
                await self._reset_redis()
                raise
        except Exception:
            logger.warning(
                "Could not release the rate-limit reservation on %s.",
                bucket_key,
                exc_info=True,
            )

    @staticmethod
    async def _give_back(client: redis.Redis, key: str) -> None:
        """Decrement a counter, dropping the key once it reaches zero.

        A window that expired between reserve and release would otherwise be
        recreated at -1 by the decrement alone, with no TTL to remove it.
        """
        remaining = await client.decr(key)
        if remaining <= 0:
            await client.delete(key)

    async def _preauth_reserve(
        self,
        preauth_bucket: str | None,
        now: float,
        *,
        force_memory: bool = False,
    ) -> tuple[JSONResponse | None, bool]:
        """Take a pre-auth reservation, or the 429 that refuses it."""
        if preauth_bucket is None:
            return None, False
        exhausted, reset_in = await self._reserve(
            preauth_bucket,
            now,
            self.preauth_limit,
            force_memory=force_memory,
        )
        if not exhausted:
            return None, True
        return (
            _rate_limited_response(
                self.preauth_limit,
                reset_in,
                (
                    "Rate limit exceeded. "
                    f"{self.preauth_limit} requests per minute without accepted "
                    "credentials allowed per client; presenting a different API "
                    "key does not reset it."
                ),
            ),
            False,
        )

    def _sweep_expired_buckets(self, window_start: float) -> None:
        """Drop in-memory buckets with nothing left inside the window.

        Bucket keys are caller-controlled (an ``X-API-Key`` value), so a caller
        rotating key values would otherwise leave one list per value behind for
        the life of the process. Called under ``self._lock``.
        """
        stale = [
            key
            for key, timestamps in self._requests.items()
            if not timestamps or timestamps[-1] <= window_start
        ]
        for key in stale:
            del self._requests[key]

    async def _check_limit_in_memory(
        self,
        bucket_key: str,
        now: float,
        *,
        limit: int | None = None,
    ) -> tuple[bool, int, int]:
        """
        Returns (limited, remaining, reset_in_seconds).
        """
        effective_limit = self.limit if limit is None else limit
        window_start = now - self.window

        async with self._lock:
            if (
                len(self._requests) > _MEMORY_BUCKET_SWEEP_THRESHOLD
                and now - self._last_sweep >= self.window
            ):
                self._last_sweep = now
                self._sweep_expired_buckets(window_start)

            self._requests[bucket_key] = [
                ts for ts in self._requests[bucket_key] if ts > window_start
            ]

            current_count = len(self._requests[bucket_key])
            if current_count >= effective_limit:
                oldest = (
                    self._requests[bucket_key][0] if self._requests[bucket_key] else now
                )
                reset_in = max(1, int(oldest + self.window - now) + 1)
                return True, 0, reset_in

            self._requests[bucket_key].append(now)
            remaining = max(
                0,
                effective_limit - len(self._requests[bucket_key]),
            )
            oldest = self._requests[bucket_key][0]
            reset_in = max(1, int(oldest + self.window - now) + 1)
            return False, remaining, reset_in

    async def dispatch(self, request: Request, call_next) -> Response:
        # Public MCP is intentionally unauthenticated. Ignore caller-supplied
        # API-key headers there so rotating bogus values cannot mint fresh
        # rate-limit buckets (and the local test-key bypass cannot be reached
        # from the Internet).
        public_mcp_request = request.url.path.rstrip("/") == _PUBLIC_MCP_PATH
        if public_mcp_request:
            client_id = _client_id(request)
            namespace = (
                f"{settings.STATE_NAMESPACE}:{settings.PUBLIC_URL or settings.APP_NAME}"
            )
            bucket_limits = [
                (
                    f"{namespace}:{_PUBLIC_MCP_BUCKET_PREFIX}:client:{client_id}",
                    self.limit,
                ),
                (
                    f"{namespace}:{_PUBLIC_MCP_BUCKET_PREFIX}:global",
                    max(
                        self.limit,
                        self.limit * _PUBLIC_MCP_GLOBAL_LIMIT_MULTIPLIER,
                    ),
                ),
            ]
        else:
            # Canonicalize the way auth does (app.core.auth strips before
            # lookup): otherwise "key" and "key " count as separate buckets and
            # one accepted credential can sidestep the per-key limit. A blank
            # header is no credential at all, so it shares the anonymous
            # bucket. The bucket is then named by a digest, never the key.
            presented_key = _presented_rate_identity(request)
            bucket_limits = [(_api_key_bucket(presented_key), self.limit)]

        # Skip rate limiting for docs, health, and test clients
        skip_paths = (
            "/docs",
            "/redoc",
            "/openapi.json",
            "/health",
            "/",
            "/.well-known/agent.json",
            "/llm.txt",
            "/llms.txt",
            "/docs/index",
            "/WEDGE.md",
            "/SECURITY_LIMITATIONS.md",
            "/DESIGN_PARTNER_GUIDE.md",
            "/docs/partner-api-key-bootstrap.md",
        )
        if request.url.path in skip_paths:
            response = await call_next(request)
            _credentials_were_rejected(response)  # strip the internal marker
            return response  # type: ignore[no-any-return]

        # In test / CI environments, the special "test-key" bypasses rate limits
        # so that large test suites don't self-throttle.
        if (
            not public_mcp_request
            and presented_key == "test-key"
            and not is_production_like_environment(settings.ENVIRONMENT)
        ):
            response = await call_next(request)
            _credentials_were_rejected(response)  # strip the internal marker
            response.headers["X-RateLimit-Limit"] = str(self.limit)
            response.headers["X-RateLimit-Remaining"] = str(self.limit)
            response.headers["X-RateLimit-Reset"] = "60"
            return response  # type: ignore[no-any-return]

        now = time.time()
        applied_limit = self.limit
        header_remaining = self.limit
        header_reset = int(self.window)

        # Pre-authentication ceiling. The per-key bucket above is selected from
        # a header the caller controls, before verify_api_key has had a chance
        # to reject it, so a caller sending a fresh X-API-Key on every request
        # would otherwise be handed a fresh 120-request budget each time — an
        # unbounded amount of traffic from one client. One shared per-client
        # bucket bounds that instead: every request reserves from it before
        # running, and hands the reservation back only if get_auth_context
        # accepted the credentials. Keying that on acceptance rather than on
        # refusal matters: a route that never authenticates (a public route,
        # a 404) never refuses an invented key either. Rotation buys nothing
        # past the bucket, and a request whose credentials the app accepts
        # leaves it exactly as it found it.
        preauth_bucket = None if public_mcp_request else self._preauth_bucket(request)
        preauth_reserved = False
        preauth_in_memory = False
        keep_reservation = False

        try:
            try:
                rejection, preauth_reserved = await self._preauth_reserve(
                    preauth_bucket, now
                )
                if rejection is not None:
                    return rejection
                for index, (bucket_key, applied_limit) in enumerate(bucket_limits):
                    limited, remaining, reset_in = await self._check_limit_with_redis(
                        bucket_key,
                        now,
                        limit=applied_limit,
                    )
                    if index == 0:
                        header_remaining = remaining
                        header_reset = reset_in
                    if limited:
                        break
            except RateLimiterUnavailable:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": {
                            "error": "rate_limiter_unavailable",
                            "message": (
                                "Shared rate limiter (Redis) is unavailable. "
                                "In-memory fallback is refused in production-like "
                                "environments."
                            ),
                        }
                    },
                    headers={
                        "X-RateLimit-Limit": str(self.limit),
                        "X-RateLimit-Remaining": "0",
                        "Retry-After": "30",
                    },
                )
            except Exception:
                # A command failed on a connection we already had (connect
                # failures surface as RateLimiterUnavailable above). Drop the
                # cached client so the next request reconnects instead of
                # failing on the same dead socket until the process restarts.
                fail_closed = self._fail_closed_on_redis_outage()
                logger.exception(
                    "Redis rate limiter command failed; reset the cached "
                    "connection and %s.",
                    (
                        "refused the request (production-like)"
                        if fail_closed
                        else "used the in-memory limiter for this request"
                    ),
                )
                await self._reset_redis()
                mark_rate_limiter_memory_fallback()
                if fail_closed:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": {
                                "error": "rate_limiter_unavailable",
                                "message": (
                                    "Shared rate limiter (Redis) failed. "
                                    "In-memory fallback is refused in "
                                    "production-like environments."
                                ),
                            }
                        },
                        headers={
                            "X-RateLimit-Limit": str(self.limit),
                            "X-RateLimit-Remaining": "0",
                            "Retry-After": "30",
                        },
                    )
                if not preauth_reserved:
                    # Redis failed before the reservation landed; take it from
                    # memory so the ceiling does not vanish for the outage.
                    rejection, preauth_reserved = await self._preauth_reserve(
                        preauth_bucket,
                        now,
                        force_memory=True,
                    )
                    if rejection is not None:
                        return rejection
                    preauth_in_memory = preauth_reserved
                for index, (bucket_key, applied_limit) in enumerate(bucket_limits):
                    limited, remaining, reset_in = await self._check_limit_in_memory(
                        bucket_key,
                        now,
                        limit=applied_limit,
                    )
                    if index == 0:
                        header_remaining = remaining
                        header_reset = reset_in
                    if limited:
                        break

            if limited:
                return _rate_limited_response(
                    applied_limit,
                    reset_in,
                    (
                        f"Rate limit exceeded. {applied_limit} requests per "
                        "minute allowed."
                    ),
                )

            # Process request, recording whether it authenticated. Once the app
            # has run, the reservation is kept unless it accepted credentials
            # — decided in the finally, so a request that raised on its way
            # through is held to the same rule as one that answered.
            acceptance = CredentialAcceptance()
            acceptance_token = CREDENTIAL_ACCEPTANCE.set(acceptance)
            try:
                response = await call_next(request)
            finally:
                CREDENTIAL_ACCEPTANCE.reset(acceptance_token)
                keep_reservation = not acceptance.accepted

            # Also keep it when the app refused credentials — a 401, or the 403
            # it answers an unknown API key with — even if another credential
            # on the request was accepted. An authenticated caller denied on
            # scope hands its reservation back: in a trust plane, denials are
            # ordinary traffic, not abuse.
            if _credentials_were_rejected(response):
                keep_reservation = True

            response.headers["X-RateLimit-Limit"] = str(self.limit)
            response.headers["X-RateLimit-Remaining"] = str(header_remaining)
            response.headers["X-RateLimit-Reset"] = str(header_reset)

            return response  # type: ignore[no-any-return]
        finally:
            # Every other exit — accepted credentials, or a request refused
            # before it ran (the per-key 429, a limiter 503) — gives the
            # reservation back.
            if preauth_bucket is not None and preauth_reserved and not keep_reservation:
                await self._release(
                    preauth_bucket,
                    now,
                    force_memory=preauth_in_memory,
                )
