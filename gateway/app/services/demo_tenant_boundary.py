"""Tenant and tool boundaries for self-serve demo credentials."""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware

DEMO_TENANT_LABEL = "demo"
DEMO_TENANT_DISABLED = "demo_tenant_disabled"
DEMO_KEY_ROUTE_FORBIDDEN = "demo_key_route_forbidden"
KEY_TOOL_NOT_ALLOWED = "key_tool_not_allowed"
DEMO_PERMIT_OUT_OF_BOUNDS = "demo_permit_out_of_bounds"


class KeyAllowlistDenied(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.error = KEY_TOOL_NOT_ALLOWED
        self.message = message


class DemoPermitDenied(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.error = DEMO_PERMIT_OUT_OF_BOUNDS
        self.message = message


def parse_allowlist(raw: str | None) -> tuple[str, ...] | None:
    """A missing list is unrestricted; malformed stored data denies all tools."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        return ()
    return tuple(parsed)


def check_key_allowlist_for_permit(
    *,
    key_allowlist: tuple[str, ...] | None,
    allowed_tools: list[str] | None,
    scopes: list[str] | None,
) -> None:
    if key_allowlist is None:
        return
    tools = list(allowed_tools or [])
    if not tools:
        raise KeyAllowlistDenied(
            "permit allowed_tools must be non-empty for a key with a tool allowlist"
        )
    for tool in tools:
        if tool == "*" or tool not in key_allowlist:
            raise KeyAllowlistDenied(
                f"permit tool {tool!r} is not in this key's allowlist"
            )
    effective_scopes = list(scopes or []) or [f"tool:{tool}:invoke" for tool in tools]
    for scope in effective_scopes:
        if scope == "billing:charge":
            continue
        parts = scope.split(":")
        if len(parts) < 3 or parts[0] != "tool" or not parts[1]:
            raise KeyAllowlistDenied(
                f"permit scope {scope!r} is not a recognized tool scope"
            )
        if parts[1] == "*" or parts[1] not in key_allowlist:
            raise KeyAllowlistDenied(
                f"permit scope {scope!r} names a tool outside this key's allowlist"
            )


def check_demo_permit_bounds(
    *,
    issuer_tenant: str | None,
    subject_tenant: str | None,
    caller_tenant: str | None,
    allowed_tools: list[str] | None,
    max_credits: Decimal,
    expires_at: datetime,
    caller_key_expires_at: datetime | None,
    demo_allowed_tools: list[str],
    max_permit_credits: Decimal | None,
    max_permit_ttl: timedelta | None,
    now: datetime,
) -> None:
    if DEMO_TENANT_LABEL not in (issuer_tenant, subject_tenant, caller_tenant):
        return
    if issuer_tenant != subject_tenant:
        raise DemoPermitDenied(
            "cross-tenant permits are forbidden: issuer and subject wallets "
            "must be in the same tenant"
        )
    if caller_tenant == DEMO_TENANT_LABEL and issuer_tenant != DEMO_TENANT_LABEL:
        raise DemoPermitDenied("a demo key may only issue permits for demo wallets")
    if demo_allowed_tools:
        tools = list(allowed_tools or [])
        if not tools or any(tool not in demo_allowed_tools for tool in tools):
            raise DemoPermitDenied(
                "demo permit allowed_tools must be a non-empty subset of "
                f"{demo_allowed_tools}"
            )
    if max_permit_credits is not None and max_credits > max_permit_credits:
        raise DemoPermitDenied(
            f"demo permit max_credits {max_credits} exceeds the cap {max_permit_credits}"
        )
    expires_naive = expires_at.replace(tzinfo=None)
    if (
        max_permit_ttl is not None
        and expires_naive > now.replace(tzinfo=None) + max_permit_ttl
    ):
        raise DemoPermitDenied("demo permit expires_at exceeds the demo TTL cap")
    if (
        caller_key_expires_at is not None
        and expires_naive > caller_key_expires_at.replace(tzinfo=None)
    ):
        raise DemoPermitDenied("demo permit cannot outlive the caller key's expiry")


async def enforce_permit_bounds(request: Any, auth: Any | None) -> None:
    """Check key and wallet limits before permit idempotency or signing."""
    from app.core.config import get_settings
    from app.core.time import utc_now
    from app.db.database import get_session_factory
    from app.db.models import WalletModel
    from app.services.api_key_service import get_api_key_service

    check_key_allowlist_for_permit(
        key_allowlist=getattr(auth, "allowed_tools", None),
        allowed_tools=list(request.allowed_tools or []),
        scopes=list(request.scopes or []),
    )
    async with get_session_factory()() as session:
        issuer = await session.get(WalletModel, request.issuer_wallet_id)
        subject = await session.get(WalletModel, request.subject_wallet_id)
    issuer_tenant = getattr(issuer, "tenant", None) if issuer else None
    subject_tenant = getattr(subject, "tenant", None) if subject else None
    caller_tenant = getattr(auth, "tenant", None)
    if DEMO_TENANT_LABEL not in (issuer_tenant, subject_tenant, caller_tenant):
        return
    caller_key_expires_at = None
    key_id = getattr(auth, "key_id", None)
    if key_id:
        key_row = await get_api_key_service().get_key_record(key_id)
        caller_key_expires_at = key_row.expires_at if key_row else None
    settings = get_settings()
    check_demo_permit_bounds(
        issuer_tenant=issuer_tenant,
        subject_tenant=subject_tenant,
        caller_tenant=caller_tenant,
        allowed_tools=list(request.allowed_tools or []),
        max_credits=request.max_credits,
        expires_at=request.expires_at,
        caller_key_expires_at=caller_key_expires_at,
        demo_allowed_tools=settings.demo_allowed_tools_list,
        max_permit_credits=settings.DEMO_MAX_PERMIT_CREDITS,
        max_permit_ttl=(
            timedelta(minutes=settings.DEMO_MAX_PERMIT_TTL_MINUTES)
            if settings.DEMO_MAX_PERMIT_TTL_MINUTES is not None
            else None
        ),
        now=utc_now(),
    )


_DEMO_DENIED_PREFIXES = (
    "/v1/api-keys",
    "/v1/auth",
    "/v1/kyc",
    "/v1/x402",
    "/v1/billing/top-up",
    "/v1/billing/acp/checkout",
    "/v1/webhooks",
    "/v1/billing/wallets/sponsor",
    "/v1/billing/wallets/agent",
    "/v1/billing/wallets/child",
    "/v1/pods",
    "/v1/signing-keys",
    "/v1/demo/admin",
    "/v1/admin",
)


def demo_route_allowed(method: str, path: str) -> bool:
    """Confine demo keys away from credential, fiat, and admin routes."""
    del method
    return not any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _DEMO_DENIED_PREFIXES
    )


_demo_request: ContextVar[tuple[str, str] | None] = ContextVar(
    "demo_request", default=None
)


def current_demo_request() -> tuple[str, str] | None:
    return _demo_request.get()


class DemoRequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        token = _demo_request.set((request.method, request.url.path))
        try:
            return await call_next(request)
        finally:
            _demo_request.reset(token)


async def resolve_effective_tenant(
    key_tenant: str | None, wallet_id: str | None
) -> str | None:
    """The wallet's demo label wins even if an older key lacks that label."""
    if key_tenant == DEMO_TENANT_LABEL:
        return DEMO_TENANT_LABEL
    if not wallet_id:
        return key_tenant

    from app.db.database import get_session_factory
    from app.db.models import WalletModel

    async with get_session_factory()() as session:
        wallet = await session.get(WalletModel, wallet_id)
        wallet_tenant = getattr(wallet, "tenant", None) if wallet else None
    if wallet_tenant == DEMO_TENANT_LABEL:
        return DEMO_TENANT_LABEL
    return key_tenant or wallet_tenant
