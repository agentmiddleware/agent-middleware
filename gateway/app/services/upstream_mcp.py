"""Single-server Streamable HTTP MCP adapter for the design-partner pilot.

The adapter deliberately owns only remote transport concerns. Permit checks,
metering, dispatch-attempt persistence, compensation, receipts, and audit stay
in the governed MCP invocation layer. ``before_dispatch`` is the boundary
between refundable setup failures and an externally ambiguous tool call.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Protocol, cast
from urllib.parse import SplitResult, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from pydantic import SecretStr, ValidationError

from ..core.config import Settings, get_settings
from ..core.trust_mode import is_production_like_environment
from ..schemas.billing import ServiceCategory
from .mcp_dispatch_attempts import (
    MAX_UPSTREAM_CALL_TIMEOUT_SECONDS,
    MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
)
from .service_registry import ServiceRegistry, get_service_registry
from .signing_keys import canonical_json

logger = logging.getLogger(__name__)

_SAFE_TOOL_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.aws.internal",
        "instance-data",
    }
)
_BLOCKED_HOST_SUFFIXES = (".internal", ".local")
_MAX_DISCOVERY_PAGES = 100
_MAX_DISCOVERY_TOOLS = 1024
_CREDITS_QUANTUM = Decimal("0.00000001")
_MAX_NUMERIC_20_8 = Decimal("999999999999.99999999")
_GATEWAY_TOOL_DESCRIPTION = (
    "Configured remote MCP tool dispatched through the Agent Middleware "
    "transaction-integrity boundary."
)
_METRICS_LOCK = threading.Lock()
_CALL_OUTCOMES = (
    "succeeded",
    "returned_error",
    "delivery_uncertain",
    "response_rejected",
    "pre_dispatch_failure",
    "internal_failure",
)
_CALL_METRICS: dict[str, Any] = {
    "calls_total": 0,
    "latency_seconds_total": 0.0,
    "latency_seconds_max": 0.0,
    "outcomes": {outcome: 0 for outcome in _CALL_OUTCOMES},
}


class McpSession(Protocol):
    async def initialize(self) -> Any: ...

    async def list_tools(self, cursor: str | None = None) -> Any: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult: ...


Resolver = Callable[[str, int | None], Awaitable[Sequence[str]]]
BeforeDispatch = Callable[[], Awaitable[None]]


class UpstreamMcpError(RuntimeError):
    """Base error with a stable code and dispatch-boundary signal."""

    def __init__(self, code: str, *, dispatch_started: bool) -> None:
        super().__init__(code)
        self.code = code
        self.dispatch_started = dispatch_started


class UpstreamMcpPreDispatchError(UpstreamMcpError):
    """Connection, initialization, or dispatch-checkpoint failure."""

    def __init__(self, code: str = "upstream_pre_dispatch_failed") -> None:
        super().__init__(code, dispatch_started=False)


class UpstreamMcpDispatchClaimUnavailableError(UpstreamMcpError):
    """Another activation already owns the durable right to send."""

    def __init__(self) -> None:
        super().__init__("idempotency_in_progress", dispatch_started=False)


class UpstreamMcpConfigurationError(UpstreamMcpPreDispatchError):
    """Unsafe or incomplete operator configuration."""

    def __init__(self, detail: str) -> None:
        super().__init__("upstream_mcp_configuration_invalid")
        self.detail = detail


class UpstreamMcpDeliveryUncertainError(UpstreamMcpError):
    """The call began but no trustworthy terminal response was obtained."""

    def __init__(self) -> None:
        super().__init__("upstream_delivery_uncertain", dispatch_started=True)


class UpstreamMcpResponseRejectedError(UpstreamMcpError):
    """A confirmed response could not be safely retained."""

    def __init__(self, code: str = "upstream_response_rejected") -> None:
        super().__init__(code, dispatch_started=True)


@dataclass(frozen=True)
class UpstreamMcpResult:
    """Canonical, bounded representation of one upstream MCP result."""

    payload: dict[str, Any]
    canonical_json: str
    response_hash: str
    size_bytes: int
    is_error: bool


class UpstreamMcpReturnedError(UpstreamMcpError):
    """The upstream returned a valid MCP tool-level error result."""

    def __init__(self, result: UpstreamMcpResult) -> None:
        super().__init__("upstream_returned_error", dispatch_started=True)
        self.result = result


@dataclass(frozen=True)
class DiscoveredUpstreamTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class UpstreamMcpConfiguration:
    url: str
    origin: str
    tool_name: str
    public_tool_id: str
    bearer_token: SecretStr = field(repr=False)
    credits_per_call: Decimal
    connect_timeout_seconds: float
    call_timeout_seconds: float
    max_response_bytes: int
    environment: str

    @classmethod
    def from_settings(cls, settings: Settings) -> UpstreamMcpConfiguration:
        """Build validated non-network configuration from application settings."""
        required_strings = {
            "MCP_UPSTREAM_URL": settings.MCP_UPSTREAM_URL,
            "MCP_UPSTREAM_TOOL_NAME": settings.MCP_UPSTREAM_TOOL_NAME,
            "MCP_UPSTREAM_PUBLIC_TOOL_ID": settings.MCP_UPSTREAM_PUBLIC_TOOL_ID,
        }
        missing = sorted(name for name, value in required_strings.items() if not value)
        token = settings.MCP_UPSTREAM_BEARER_TOKEN.get_secret_value()
        if not token:
            missing.append("MCP_UPSTREAM_BEARER_TOKEN")
        if missing:
            raise UpstreamMcpConfigurationError(
                "missing required settings: " + ", ".join(missing)
            )

        if token != token.strip() or any(ord(char) < 32 for char in token):
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_BEARER_TOKEN contains surrounding whitespace or control characters"
            )

        public_configuration = (
            settings.MCP_UPSTREAM_URL,
            settings.MCP_UPSTREAM_TOOL_NAME,
            settings.MCP_UPSTREAM_PUBLIC_TOOL_ID,
            _GATEWAY_TOOL_DESCRIPTION,
        )
        if any(token in value for value in public_configuration):
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_BEARER_TOKEN must differ from public upstream configuration"
            )

        for setting_name, tool_id in (
            ("MCP_UPSTREAM_TOOL_NAME", settings.MCP_UPSTREAM_TOOL_NAME),
            ("MCP_UPSTREAM_PUBLIC_TOOL_ID", settings.MCP_UPSTREAM_PUBLIC_TOOL_ID),
        ):
            if not _SAFE_TOOL_ID.fullmatch(tool_id):
                raise UpstreamMcpConfigurationError(
                    f"{setting_name} must use only the MCP tool-name characters A-Z, a-z, 0-9, _, -, and ."
                )

        credits = settings.MCP_UPSTREAM_CREDITS_PER_CALL
        if not credits.is_finite() or credits <= 0:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CREDITS_PER_CALL must be finite and greater than zero"
            )
        if credits > _MAX_NUMERIC_20_8:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CREDITS_PER_CALL must fit NUMERIC(20,8)"
            )
        try:
            with localcontext() as context:
                context.prec = 64
                quantized_credits = credits.quantize(_CREDITS_QUANTUM)
        except InvalidOperation:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CREDITS_PER_CALL must fit NUMERIC(20,8)"
            ) from None
        if quantized_credits != credits:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CREDITS_PER_CALL must use at most 8 decimal places"
            )
        if (
            not math.isfinite(settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS)
            or settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS <= 0
            or settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS
            > MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS
        ):
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS must be finite, greater than zero, "
                f"and no greater than {MAX_UPSTREAM_CONNECT_TIMEOUT_SECONDS} seconds"
            )
        if (
            not math.isfinite(settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS)
            or settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS <= 0
            or settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS
            > MAX_UPSTREAM_CALL_TIMEOUT_SECONDS
        ):
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CALL_TIMEOUT_SECONDS must be finite, greater than zero, "
                f"and no greater than {MAX_UPSTREAM_CALL_TIMEOUT_SECONDS} seconds"
            )
        if settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES <= 0:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_MAX_RESPONSE_BYTES must be greater than zero"
            )
        if not math.isfinite(float(credits)):
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_CREDITS_PER_CALL is too large"
            )

        parts = _parse_url_shape(settings.MCP_UPSTREAM_URL)
        return cls(
            url=settings.MCP_UPSTREAM_URL,
            origin=_sanitized_origin(parts),
            tool_name=settings.MCP_UPSTREAM_TOOL_NAME,
            public_tool_id=settings.MCP_UPSTREAM_PUBLIC_TOOL_ID,
            bearer_token=settings.MCP_UPSTREAM_BEARER_TOKEN,
            credits_per_call=quantized_credits,
            connect_timeout_seconds=settings.MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
            call_timeout_seconds=settings.MCP_UPSTREAM_CALL_TIMEOUT_SECONDS,
            max_response_bytes=settings.MCP_UPSTREAM_MAX_RESPONSE_BYTES,
            environment=settings.ENVIRONMENT,
        )


def _parse_url_shape(url: str) -> SplitResult:
    if not url or url != url.strip() or any(ord(char) < 32 for char in url):
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL must be a non-empty URL without whitespace or control characters"
        )
    if "?" in url:
        raise UpstreamMcpConfigurationError("MCP_UPSTREAM_URL must not contain a query")
    if "#" in url:
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL must not contain a fragment"
        )
    try:
        parts = urlsplit(url)
        # Accessing ``port`` validates malformed/out-of-range port text.
        _ = parts.port
    except ValueError as exc:
        raise UpstreamMcpConfigurationError("MCP_UPSTREAM_URL is malformed") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise UpstreamMcpConfigurationError("MCP_UPSTREAM_URL must use http or https")
    if not parts.hostname:
        raise UpstreamMcpConfigurationError("MCP_UPSTREAM_URL must include a host")
    if parts.username is not None or parts.password is not None:
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL must not contain embedded credentials"
        )
    return parts


def _sanitized_origin(parts: SplitResult) -> str:
    host = (parts.hostname or "").rstrip(".").lower()
    rendered_host = f"[{host}]" if ":" in host else host
    port = parts.port
    default_port = 443 if parts.scheme.lower() == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parts.scheme.lower()}://{rendered_host}{suffix}"


async def _default_resolver(host: str, port: int | None) -> Sequence[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL host could not be resolved"
        ) from exc
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


async def validate_upstream_url(
    configuration: UpstreamMcpConfiguration,
    *,
    resolver: Resolver | None = None,
) -> str:
    """Validate the destination and return the address to pin for connection."""
    parts = _parse_url_shape(configuration.url)
    host = (parts.hostname or "").strip("[]").rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL host is reserved for internal infrastructure"
        )

    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    explicit_loopback = host == "localhost" or host.endswith(".localhost")
    addresses: Sequence[str]
    if literal is not None:
        explicit_loopback = literal.is_loopback
        addresses = (str(literal),)
    elif explicit_loopback:
        addresses = ("127.0.0.1", "::1")
    else:
        try:
            async with asyncio.timeout(configuration.connect_timeout_seconds):
                addresses = tuple(
                    await (resolver or _default_resolver)(host, parts.port)
                )
        except UpstreamMcpConfigurationError:
            raise
        except Exception:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_URL host could not be resolved"
            ) from None

    if not addresses:
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL host resolved to no addresses"
        )

    parsed_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for address in addresses:
        try:
            parsed_addresses.append(ipaddress.ip_address(address))
        except ValueError as exc:
            raise UpstreamMcpConfigurationError(
                "MCP_UPSTREAM_URL resolution returned an invalid address"
            ) from exc

    local_environment = not is_production_like_environment(configuration.environment)
    loopback_exception = (
        local_environment
        and explicit_loopback
        and all(address.is_loopback for address in parsed_addresses)
    )
    if parts.scheme.lower() == "http" and not loopback_exception:
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL must use https except for loopback local/test servers"
        )
    # is_global alone admits multicast: the stdlib classifies 224.0.0.0/4 and
    # ff00::/8 as global, so a multicast destination is refused by name.
    if not loopback_exception and any(
        address.is_multicast or not address.is_global for address in parsed_addresses
    ):
        raise UpstreamMcpConfigurationError(
            "MCP_UPSTREAM_URL must not resolve to private, loopback, link-local, "
            "metadata, multicast, or reserved addresses"
        )
    return str(parsed_addresses[0])


@dataclass
class _ResponseGuardState:
    rejection_code: str | None = None
    rejected: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def reject(self, code: str) -> None:
        if self.rejection_code is None:
            self.rejection_code = code
            self.rejected.set()


class _GuardedResponseRejected(RuntimeError):
    """Internal marker for a response rejected before buffering or parsing."""


class _BoundedResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        *,
        limit: int,
        state: _ResponseGuardState,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._state = state

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self._stream:
            received += len(chunk)
            if received > self._limit:
                self._state.reject("upstream_response_too_large")
                await self._stream.aclose()
                raise _GuardedResponseRejected("upstream_response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _DelegatedResponseStream(httpx.AsyncByteStream):
    """Keep an injected client's streamed response alive without owning the client."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._response.is_stream_consumed:
            if self._response.content:
                yield self._response.content
            return
        async for chunk in self._response.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class _InjectedClientTransport(httpx.AsyncBaseTransport):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._client.send(
            request,
            follow_redirects=False,
            stream=True,
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_DelegatedResponseStream(response),
            extensions=response.extensions,
        )


class _PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """Connect to one validated address while retaining HTTP Host and TLS SNI."""

    def __init__(
        self,
        configuration: UpstreamMcpConfiguration,
        *,
        pinned_address: str,
        response_guard: _ResponseGuardState,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = _parse_url_shape(configuration.url)
        self._expected_url = httpx.URL(configuration.url)
        self._expected_host_header = self._expected_url.netloc.decode("ascii")
        self._server_name = (parts.hostname or "").strip("[]").rstrip(".").lower()
        self._pinned_address = pinned_address
        self._max_response_bytes = configuration.max_response_bytes
        self._response_guard = response_guard
        self._transport = transport or httpx.AsyncHTTPTransport(
            verify=True,
            retries=0,
            trust_env=False,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url != self._expected_url
            or request.headers.get("Host") != self._expected_host_header
        ):
            raise UpstreamMcpConfigurationError(
                "upstream MCP HTTP client attempted an unconfigured endpoint"
            )

        extensions = dict(request.extensions)
        extensions["sni_hostname"] = self._server_name
        pinned_request = httpx.Request(
            request.method,
            request.url.copy_with(host=self._pinned_address),
            headers=request.headers,
            stream=request.stream,
            extensions=extensions,
        )
        response = await self._transport.handle_async_request(pinned_request)

        content_encoding = response.headers.get("content-encoding", "identity")
        if content_encoding.strip().lower() not in {"", "identity"}:
            self._response_guard.reject("upstream_response_invalid")
            await response.aclose()
            raise _GuardedResponseRejected("upstream_response_invalid")

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                self._response_guard.reject("upstream_response_invalid")
                await response.aclose()
                raise _GuardedResponseRejected("upstream_response_invalid") from None
            if declared_length < 0:
                self._response_guard.reject("upstream_response_invalid")
                await response.aclose()
                raise _GuardedResponseRejected("upstream_response_invalid")
            if declared_length > self._max_response_bytes:
                self._response_guard.reject("upstream_response_too_large")
                await response.aclose()
                raise _GuardedResponseRejected("upstream_response_too_large")

        # This is the identity-encoded HTTP body cap, including the JSON-RPC
        # envelope. Discovery and tool results are separately capped after
        # parsing so their retained canonical payloads cannot exceed the limit.
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_BoundedResponseStream(
                cast(httpx.AsyncByteStream, response.stream),
                limit=self._max_response_bytes,
                state=self._response_guard,
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


async def _await_guarded_response(
    operation: Awaitable[Any],
    *,
    response_guard: _ResponseGuardState,
) -> Any:
    """Stop waiting on the MCP SDK as soon as its HTTP stream is rejected."""
    operation_task = asyncio.ensure_future(operation)
    rejection_task = asyncio.create_task(response_guard.rejected.wait())
    try:
        await asyncio.wait(
            {operation_task, rejection_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if response_guard.rejection_code is not None:
            raise _GuardedResponseRejected(response_guard.rejection_code)
        return await operation_task
    finally:
        for task in (operation_task, rejection_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation_task, rejection_task, return_exceptions=True)


def _contains_exact_secret(value: Any, secret: str) -> bool:
    """Return whether an untrusted JSON-like value reflects the bearer token."""
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Mapping):
        return any(
            _contains_exact_secret(key, secret) or _contains_exact_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_exact_secret(item, secret) for item in value)
    return False


def _bounded_json_size(value: Any, *, limit: int) -> int:
    """Serialize JSON deterministically and reject invalid or oversized metadata."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise UpstreamMcpConfigurationError(
            "upstream MCP discovery metadata is invalid"
        ) from None
    if len(encoded) > limit:
        raise UpstreamMcpConfigurationError(
            "upstream MCP discovery metadata exceeds the configured size limit"
        )
    return len(encoded)


def _record_call_metric(*, outcome: str, latency_seconds: float) -> None:
    with _METRICS_LOCK:
        _CALL_METRICS["calls_total"] += 1
        _CALL_METRICS["latency_seconds_total"] += latency_seconds
        _CALL_METRICS["latency_seconds_max"] = max(
            _CALL_METRICS["latency_seconds_max"],
            latency_seconds,
        )
        _CALL_METRICS["outcomes"][outcome] += 1


def get_upstream_mcp_metrics_snapshot() -> dict[str, Any]:
    """Return bounded process-local counters without request or partner data."""
    with _METRICS_LOCK:
        calls_total = int(_CALL_METRICS["calls_total"])
        return {
            "calls_total": calls_total,
            "latency_seconds_total": float(_CALL_METRICS["latency_seconds_total"]),
            "latency_seconds_max": float(_CALL_METRICS["latency_seconds_max"]),
            "latency_seconds_average": (
                float(_CALL_METRICS["latency_seconds_total"]) / calls_total
                if calls_total
                else 0.0
            ),
            "outcomes": dict(_CALL_METRICS["outcomes"]),
        }


class UpstreamMcpAdapter:
    """Discover and invoke exactly one configured upstream MCP tool."""

    def __init__(
        self,
        configuration: UpstreamMcpConfiguration,
        *,
        http_client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.configuration = configuration
        self._injected_http_client = http_client
        self._resolver = resolver
        self._discovered_tool: DiscoveredUpstreamTool | None = None

    @property
    def discovered_tool(self) -> DiscoveredUpstreamTool | None:
        return self._discovered_tool

    @asynccontextmanager
    async def _http_client(
        self,
        *,
        response_guard: _ResponseGuardState | None = None,
    ) -> AsyncIterator[httpx.AsyncClient]:
        active_guard = response_guard or _ResponseGuardState()
        if self._injected_http_client is not None and is_production_like_environment(
            self.configuration.environment
        ):
            raise UpstreamMcpConfigurationError(
                "injected upstream MCP HTTP clients are not allowed in production"
            )
        active_address = await validate_upstream_url(
            self.configuration,
            resolver=self._resolver,
        )
        delegated_transport: httpx.AsyncBaseTransport | None = None
        if self._injected_http_client is not None:
            if self._injected_http_client.follow_redirects:
                raise UpstreamMcpConfigurationError(
                    "injected upstream MCP HTTP client must disable redirects"
                )
            expected_authorization = (
                "Bearer " + self.configuration.bearer_token.get_secret_value()
            )
            if (
                self._injected_http_client.headers.get("Authorization")
                != expected_authorization
            ):
                raise UpstreamMcpConfigurationError(
                    "injected upstream MCP HTTP client must carry the configured bearer credential"
                )
            delegated_transport = _InjectedClientTransport(self._injected_http_client)

        token = self.configuration.bearer_token.get_secret_value()
        timeout = httpx.Timeout(
            connect=self.configuration.connect_timeout_seconds,
            read=self.configuration.call_timeout_seconds,
            write=self.configuration.call_timeout_seconds,
            pool=self.configuration.connect_timeout_seconds,
        )
        transport = _PinnedAsyncTransport(
            self.configuration,
            pinned_address=active_address,
            response_guard=active_guard,
            transport=delegated_transport,
        )
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "identity",
            },
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            yield client

    @asynccontextmanager
    async def _session(
        self,
        *,
        response_guard: _ResponseGuardState | None = None,
    ) -> AsyncIterator[McpSession]:
        async with self._http_client(response_guard=response_guard) as client:
            async with streamable_http_client(
                self.configuration.url,
                http_client=client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    yield cast(McpSession, session)

    async def _initialize_session(
        self,
        session: McpSession,
        *,
        response_guard: _ResponseGuardState,
    ) -> None:
        async with asyncio.timeout(self.configuration.call_timeout_seconds):
            await _await_guarded_response(
                session.initialize(),
                response_guard=response_guard,
            )

    async def discover_tool(self) -> DiscoveredUpstreamTool:
        """Initialize the upstream and cache the exact configured tool schema."""
        matches: list[Any] = []
        discovered_bytes = 0
        discovered_tool_count = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()
        secret = self.configuration.bearer_token.get_secret_value()
        response_guard = _ResponseGuardState()
        try:
            async with self._session(response_guard=response_guard) as session:
                await self._initialize_session(
                    session,
                    response_guard=response_guard,
                )
                for _page in range(_MAX_DISCOVERY_PAGES):
                    async with asyncio.timeout(self.configuration.call_timeout_seconds):
                        page = await _await_guarded_response(
                            session.list_tools(cursor=cursor),
                            response_guard=response_guard,
                        )
                    page_payload = page.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    )
                    if _contains_exact_secret(page_payload, secret):
                        raise UpstreamMcpConfigurationError(
                            "upstream MCP discovery metadata is unsafe"
                        )
                    discovered_bytes += _bounded_json_size(
                        page_payload,
                        limit=self.configuration.max_response_bytes,
                    )
                    if discovered_bytes > self.configuration.max_response_bytes:
                        raise UpstreamMcpConfigurationError(
                            "upstream MCP discovery metadata exceeds the configured size limit"
                        )
                    discovered_tool_count += len(page.tools)
                    if discovered_tool_count > _MAX_DISCOVERY_TOOLS:
                        raise UpstreamMcpConfigurationError(
                            "upstream tools/list exceeded the discovery tool limit"
                        )
                    matches.extend(
                        tool
                        for tool in page.tools
                        if tool.name == self.configuration.tool_name
                    )
                    cursor = page.nextCursor
                    if not cursor:
                        break
                    if cursor in seen_cursors:
                        raise UpstreamMcpConfigurationError(
                            "upstream tools/list returned a repeated cursor"
                        )
                    seen_cursors.add(cursor)
                else:
                    raise UpstreamMcpConfigurationError(
                        "upstream tools/list exceeded the discovery page limit"
                    )
        except UpstreamMcpConfigurationError:
            raise
        except Exception:
            if response_guard.rejection_code == "upstream_response_too_large":
                raise UpstreamMcpConfigurationError(
                    "upstream MCP response exceeds the configured size limit"
                ) from None
            if response_guard.rejection_code is not None:
                raise UpstreamMcpConfigurationError(
                    "upstream MCP response is invalid"
                ) from None
            raise UpstreamMcpConfigurationError(
                "upstream MCP discovery or initialization failed"
            ) from None

        if len(matches) != 1:
            raise UpstreamMcpConfigurationError(
                "configured upstream tool must appear exactly once in tools/list"
            )
        selected = matches[0]
        if not isinstance(selected.inputSchema, dict):
            raise UpstreamMcpConfigurationError(
                "configured upstream tool has an invalid input schema"
            )
        input_schema_type = selected.inputSchema.get("type")
        if input_schema_type is not None and input_schema_type != "object":
            raise UpstreamMcpConfigurationError(
                "configured upstream tool input schema must describe an object"
            )
        output_schema = selected.outputSchema
        if output_schema is not None and not isinstance(output_schema, dict):
            raise UpstreamMcpConfigurationError(
                "configured upstream tool has an invalid output schema"
            )
        discovered = DiscoveredUpstreamTool(
            name=selected.name,
            description=_GATEWAY_TOOL_DESCRIPTION,
            input_schema=selected.inputSchema,
            output_schema=output_schema,
        )
        self._discovered_tool = discovered
        return discovered

    def _canonicalize_result(self, result: Any) -> UpstreamMcpResult:
        if not isinstance(result, CallToolResult):
            raise UpstreamMcpResponseRejectedError("upstream_response_invalid")
        try:
            dumped = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            if _contains_exact_secret(
                dumped,
                self.configuration.bearer_token.get_secret_value(),
            ):
                raise UpstreamMcpResponseRejectedError()
            payload: dict[str, Any] = {
                "content": dumped.get("content", []),
                "isError": bool(dumped.get("isError", False)),
            }
            if "structuredContent" in dumped:
                payload["structuredContent"] = dumped["structuredContent"]
            canonical_result_json = canonical_json(
                payload,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError, ValidationError):
            raise UpstreamMcpResponseRejectedError(
                "upstream_response_invalid"
            ) from None
        encoded = canonical_result_json.encode("utf-8")
        if len(encoded) > self.configuration.max_response_bytes:
            raise UpstreamMcpResponseRejectedError("upstream_response_too_large")
        return UpstreamMcpResult(
            payload=payload,
            canonical_json=canonical_result_json,
            response_hash=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            is_error=bool(payload["isError"]),
        )

    async def _call_tool_once(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: BeforeDispatch,
    ) -> UpstreamMcpResult:
        """Call the cached tool once, with an explicit durable dispatch boundary.

        ``before_dispatch`` runs after HTTP connection + MCP initialization and
        immediately before ``ClientSession.call_tool``. No automatic retry is
        performed. A transport failure after that callback is conservatively
        classified as delivery-uncertain.
        """
        if self._discovered_tool is None:
            raise UpstreamMcpPreDispatchError("upstream_tool_not_initialized")
        if not invocation_id or not idempotency_key:
            raise UpstreamMcpPreDispatchError("upstream_invocation_context_missing")

        dispatch_started = False
        dispatch_claim_unavailable: UpstreamMcpDispatchClaimUnavailableError | None = (
            None
        )
        canonical_result: UpstreamMcpResult | None = None
        response_rejected: UpstreamMcpResponseRejectedError | None = None
        response_guard = _ResponseGuardState()
        try:
            async with self._session(response_guard=response_guard) as session:
                await self._initialize_session(
                    session,
                    response_guard=response_guard,
                )
                try:
                    await before_dispatch()
                except UpstreamMcpDispatchClaimUnavailableError as exc:
                    dispatch_claim_unavailable = exc
                    raise
                except Exception:
                    raise UpstreamMcpPreDispatchError(
                        "upstream_dispatch_checkpoint_failed"
                    ) from None
                dispatch_started = True
                try:
                    async with asyncio.timeout(self.configuration.call_timeout_seconds):
                        result = await _await_guarded_response(
                            session.call_tool(
                                self.configuration.tool_name,
                                arguments,
                                read_timeout_seconds=timedelta(
                                    seconds=self.configuration.call_timeout_seconds
                                ),
                                meta={
                                    "io.agentmiddleware/invocation_id": invocation_id,
                                    "io.agentmiddleware/idempotency_key": idempotency_key,
                                },
                            ),
                            response_guard=response_guard,
                        )
                    canonical_result = self._canonicalize_result(result)
                except UpstreamMcpResponseRejectedError as exc:
                    response_rejected = exc
        except UpstreamMcpError:
            if dispatch_claim_unavailable is not None:
                # A context-manager cleanup failure can itself use one of our
                # typed errors. The saved claim contention still takes
                # precedence so the router cannot enter a refund path.
                raise dispatch_claim_unavailable
            if response_guard.rejection_code is not None:
                if dispatch_started:
                    raise UpstreamMcpResponseRejectedError(
                        response_guard.rejection_code
                    ) from None
                raise UpstreamMcpPreDispatchError(
                    response_guard.rejection_code
                ) from None
            if canonical_result is not None or response_rejected is not None:
                # Cleanup may raise one of our typed transport exceptions too.
                # A canonical result or a known rejection still outranks that
                # close-time failure just as it does for an untyped exception.
                logger.warning(
                    "upstream_mcp_session_cleanup_failed",
                    extra={
                        "public_tool_id": self.configuration.public_tool_id,
                        "upstream_origin": self.configuration.origin,
                    },
                )
            else:
                raise
        except (ValidationError, json.JSONDecodeError):
            if dispatch_claim_unavailable is not None:
                raise dispatch_claim_unavailable
            if response_guard.rejection_code is not None:
                if dispatch_started:
                    raise UpstreamMcpResponseRejectedError(
                        response_guard.rejection_code
                    ) from None
                raise UpstreamMcpPreDispatchError(
                    response_guard.rejection_code
                ) from None
            if dispatch_started:
                raise UpstreamMcpResponseRejectedError(
                    "upstream_response_invalid"
                ) from None
            raise UpstreamMcpPreDispatchError() from None
        except Exception:
            if dispatch_claim_unavailable is not None:
                # Session cleanup must never erase expected claim contention:
                # doing so would misclassify the loser as refundable and let it
                # mutate the winning activation's shared financial state.
                logger.warning(
                    "upstream_mcp_session_cleanup_failed_after_claim_contention",
                    extra={
                        "public_tool_id": self.configuration.public_tool_id,
                        "upstream_origin": self.configuration.origin,
                    },
                )
                raise dispatch_claim_unavailable
            if response_guard.rejection_code is not None:
                if dispatch_started:
                    raise UpstreamMcpResponseRejectedError(
                        response_guard.rejection_code
                    ) from None
                raise UpstreamMcpPreDispatchError(
                    response_guard.rejection_code
                ) from None
            if canonical_result is not None or response_rejected is not None:
                # The terminal response is already known. A close/terminate
                # failure cannot change its outcome and must not create false
                # delivery uncertainty or trigger a refund.
                logger.warning(
                    "upstream_mcp_session_cleanup_failed",
                    extra={
                        "public_tool_id": self.configuration.public_tool_id,
                        "upstream_origin": self.configuration.origin,
                    },
                )
            elif dispatch_started:
                raise UpstreamMcpDeliveryUncertainError() from None
            else:
                raise UpstreamMcpPreDispatchError() from None

        if response_rejected is not None:
            raise response_rejected
        if canonical_result is None:
            if dispatch_started:
                raise UpstreamMcpDeliveryUncertainError()
            raise UpstreamMcpPreDispatchError()
        if canonical_result.is_error:
            raise UpstreamMcpReturnedError(canonical_result)
        return canonical_result

    async def call_tool(
        self,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
        idempotency_key: str,
        before_dispatch: BeforeDispatch,
    ) -> UpstreamMcpResult:
        """Invoke once and record bounded transport outcome/latency metrics."""
        started_at = time.perf_counter()
        outcome = "internal_failure"
        try:
            result = await self._call_tool_once(
                arguments,
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
                before_dispatch=before_dispatch,
            )
        except UpstreamMcpReturnedError:
            outcome = "returned_error"
            raise
        except UpstreamMcpDeliveryUncertainError:
            outcome = "delivery_uncertain"
            raise
        except UpstreamMcpResponseRejectedError:
            outcome = "response_rejected"
            raise
        except UpstreamMcpError as exc:
            outcome = (
                "internal_failure" if exc.dispatch_started else "pre_dispatch_failure"
            )
            raise
        else:
            outcome = "succeeded"
            return result
        finally:
            _record_call_metric(
                outcome=outcome,
                latency_seconds=max(0.0, time.perf_counter() - started_at),
            )


async def register_configured_upstream_mcp(
    *,
    settings: Settings | None = None,
    registry: ServiceRegistry | None = None,
    http_client: httpx.AsyncClient | None = None,
    resolver: Resolver | None = None,
) -> dict[str, Any] | None:
    """Discover and register the single configured executable upstream tool."""
    active_settings = settings or get_settings()
    active_registry = registry or get_service_registry()
    active_registry.unregister_execution_backend("upstream_mcp")
    if not active_settings.MCP_UPSTREAM_ENABLED:
        return None

    configuration = UpstreamMcpConfiguration.from_settings(active_settings)
    adapter = UpstreamMcpAdapter(
        configuration,
        http_client=http_client,
        resolver=resolver,
    )
    discovered = await adapter.discover_tool()
    return active_registry.register_upstream(
        service_id=configuration.public_tool_id,
        name=discovered.name,
        description=discovered.description,
        category=ServiceCategory.PLATFORM_FEE,
        executor=adapter,
        input_schema=discovered.input_schema,
        output_schema=discovered.output_schema,
        credits_per_unit=float(configuration.credits_per_call),
        upstream_tool_name=configuration.tool_name,
        upstream_origin=configuration.origin,
        credits_per_unit_exact=format(configuration.credits_per_call, "f"),
    )
