"""Isolated stateless MCP server for the controlled design-partner pilot.

This module intentionally has no database or main-application imports. Select it
with ``APP_MODULE=app.partner_mcp:app`` and provide the two required
``PARTNER_MCP_*`` settings. Configuration is validated at import time so a
misconfigured partner service refuses to start.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_BEARER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_MIN_BEARER_TOKEN_LENGTH = 32
_MAX_BEARER_TOKEN_LENGTH = 512
_MAX_ALLOWED_HOSTS = 32
_MAX_HOST_LENGTH = 260
_MAX_ECHO_LENGTH = 4096
_MAX_METADATA_VALUE_LENGTH = 256
_INVOCATION_META_KEY = "io.agentmiddleware/invocation_id"
_IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"

EchoMessage = Annotated[str, Field(max_length=_MAX_ECHO_LENGTH)]


class PartnerMcpConfigurationError(RuntimeError):
    """Raised when the isolated partner service is not safe to start."""


@dataclass(frozen=True)
class PartnerMcpConfiguration:
    """Validated startup settings with the bearer secret masked from repr."""

    bearer_token: SecretStr = field(repr=False)
    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> PartnerMcpConfiguration:
        values = os.environ if environment is None else environment
        raw_token = values.get("PARTNER_MCP_BEARER_TOKEN", "")
        raw_allowed_hosts = values.get("PARTNER_MCP_ALLOWED_HOSTS", "")

        _validate_bearer_token(raw_token)
        allowed_hosts = _parse_allowed_hosts(raw_allowed_hosts)
        if any(raw_token in host for host in allowed_hosts):
            raise PartnerMcpConfigurationError(
                "PARTNER_MCP_BEARER_TOKEN must not appear in public host configuration"
            )

        return cls(
            bearer_token=SecretStr(raw_token),
            allowed_hosts=allowed_hosts,
        )


def _validate_bearer_token(token: str) -> None:
    if not token:
        raise PartnerMcpConfigurationError("PARTNER_MCP_BEARER_TOKEN is required")
    if not (_MIN_BEARER_TOKEN_LENGTH <= len(token) <= _MAX_BEARER_TOKEN_LENGTH):
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_BEARER_TOKEN must be between 32 and 512 characters"
        )
    if _BEARER_TOKEN_PATTERN.fullmatch(token) is None:
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_BEARER_TOKEN must use bearer-token characters only"
        )
    if len(set(token)) < 8:
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_BEARER_TOKEN does not meet the minimum strength requirement"
        )


def _parse_allowed_hosts(raw_allowed_hosts: str) -> tuple[str, ...]:
    if not raw_allowed_hosts:
        raise PartnerMcpConfigurationError("PARTNER_MCP_ALLOWED_HOSTS is required")

    allowed_hosts = tuple(part.strip() for part in raw_allowed_hosts.split(","))
    if not allowed_hosts or any(not host for host in allowed_hosts):
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS must contain exact, non-empty Host values"
        )
    if len(allowed_hosts) > _MAX_ALLOWED_HOSTS:
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS contains too many Host values"
        )
    if len(set(allowed_hosts)) != len(allowed_hosts):
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS must not contain duplicates"
        )

    for host in allowed_hosts:
        _validate_exact_host(host)
    return allowed_hosts


def _validate_exact_host(host: str) -> None:
    if len(host) > _MAX_HOST_LENGTH or "*" in host or host.endswith(":"):
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS must contain valid exact Host values"
        )
    if any(ord(character) <= 32 or ord(character) == 127 for character in host):
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS contains an invalid Host value"
        )
    try:
        parsed = urlsplit(f"//{host}")
        port = parsed.port
    except ValueError as exc:
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS contains an invalid Host value"
        ) from exc
    if (
        parsed.netloc != host
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and port == 0)
    ):
        raise PartnerMcpConfigurationError(
            "PARTNER_MCP_ALLOWED_HOSTS contains an invalid Host value"
        )


class _BearerAuthMiddleware:
    """Require one bearer token on every Streamable HTTP MCP request."""

    def __init__(self, app: ASGIApp, *, expected_token_digest: bytes) -> None:
        self._app = app
        self._expected_token_digest = expected_token_digest

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_mcp_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        candidate = _bearer_candidate(scope)
        candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
        authenticated = secrets.compare_digest(
            candidate_digest,
            self._expected_token_digest,
        )
        if not authenticated:
            response = JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _bearer_candidate(scope: Scope) -> str:
    authorization_values = [
        value
        for key, value in scope.get("headers", [])
        if key.lower() == b"authorization"
    ]
    if len(authorization_values) != 1:
        return ""
    try:
        authorization = authorization_values[0].decode("ascii")
    except UnicodeDecodeError:
        return ""
    scheme, separator, candidate = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not candidate
        or candidate != candidate.strip()
        or " " in candidate
    ):
        return ""
    return candidate


def _safe_forwarded_metadata(ctx: Context) -> dict[str, str | None]:
    metadata = ctx.request_context.meta
    payload: dict[str, Any] = (
        metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
        if metadata is not None
        else {}
    )

    def bounded_string(key: str) -> str | None:
        value = payload.get(key)
        if isinstance(value, str) and len(value) <= _MAX_METADATA_VALUE_LENGTH:
            return value
        return None

    return {
        "invocation_id": bounded_string(_INVOCATION_META_KEY),
        "idempotency_key": bounded_string(_IDEMPOTENCY_META_KEY),
    }


def create_partner_mcp_app(
    environment: Mapping[str, str] | None = None,
) -> Starlette:
    """Build the isolated server after validating all security settings."""

    configuration = PartnerMcpConfiguration.from_environment(environment)
    expected_token_digest = hashlib.sha256(
        configuration.bearer_token.get_secret_value().encode("ascii")
    ).digest()
    server = FastMCP(
        "agent-middleware-partner-pilot",
        instructions=(
            "Controlled stateless echo server for governed upstream MCP "
            "integration verification."
        ),
        stateless_http=True,
        json_response=True,
        max_request_body_size=64 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(configuration.allowed_hosts),
        ),
    )

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "partner-mcp"})

    @server.tool(
        name="partner.echo",
        description=(
            "Stateless echo for governed upstream forwarding verification; "
            "it performs no persistence or external side effects."
        ),
    )
    async def partner_echo(message: EchoMessage, ctx: Context) -> dict[str, str | None]:
        forwarded = _safe_forwarded_metadata(ctx)
        return {
            "echo": message,
            "invocation_id": forwarded["invocation_id"],
            "idempotency_key": forwarded["idempotency_key"],
        }

    application = server.streamable_http_app()
    application.add_middleware(
        _BearerAuthMiddleware,
        expected_token_digest=expected_token_digest,
    )
    return application


app = create_partner_mcp_app()
