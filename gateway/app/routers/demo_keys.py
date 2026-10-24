"""Anonymous issuance of wallet-scoped credentials for the demo tenant."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from ..core.auth import AuthContext, get_auth_context
from ..core.config import get_settings
from ..core.rate_limiter import _client_id
from ..services.demo_tenant import (
    DEMO_TENANT_LABEL,
    ISSUE_SCOPE_IP,
    ISSUANCE_RATE_LIMITED,
    ISSUANCE_UNAVAILABLE,
    check_issuance_limits,
    demo_key_funnel,
    log_demo_event,
    mint_demo_keypair,
)

router = APIRouter(prefix="/v1/demo", tags=["Demo"])
admin_router = APIRouter(prefix="/v1/admin/demo-keys", tags=["Demo"])

_UTM_MAX_LENGTH = 128
_REFERRER_MAX_LENGTH = 512
_UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _optional_text(value: object, *, limit: int, truncate: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    if truncate:
        return cleaned[:limit]
    raise ValueError(f"must be at most {limit} characters")


class DemoKeyIssueRequest(BaseModel):
    """Optional acquisition attribution; all fields may be omitted."""

    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    referrer: str | None = Field(
        default=None,
        description="document.referrer of the page that requested the key.",
    )

    @field_validator(*_UTM_FIELDS, mode="before")
    @classmethod
    def _normalize_utm(cls, value: object) -> str | None:
        return _optional_text(value, limit=_UTM_MAX_LENGTH, truncate=False)

    @field_validator("referrer", mode="before")
    @classmethod
    def _normalize_referrer(cls, value: object) -> str | None:
        return _optional_text(value, limit=_REFERRER_MAX_LENGTH, truncate=True)

    def attribution(self) -> dict[str, str]:
        fields: dict[str, str] = {}
        for name in (*_UTM_FIELDS, "referrer"):
            value = getattr(self, name)
            if value:
                fields[name] = value
        return fields


class DemoKeyIssueResponse(BaseModel):
    """A freshly minted demo credential. The secret is shown once."""

    api_key: str = Field(description="Demo API key (shown once; store it now).")
    key_id: str
    key_prefix: str
    wallet_id: str = Field(description="Demo agent wallet backing this key.")
    sponsor_wallet_id: str
    tenant: str = DEMO_TENANT_LABEL
    allowed_tools: list[str] | None = Field(
        default=None, description="Tool allowlist, or null for unrestricted."
    )
    expires_at: datetime | None = None
    max_uses: int | None = None
    budget_credits: Decimal
    base_url: str = Field(description="API base URL hint for SDK callers.")
    note: str


class DemoKeyFunnelRow(BaseModel):
    utm_source: str
    utm_medium: str | None = None
    utm_campaign: str | None = None
    referrer_host: str | None = None
    keys_issued: int
    keys_used: int
    first_issued_at: datetime
    last_issued_at: datetime


class DemoKeyFunnelResponse(BaseModel):
    since: datetime | None = None
    until: datetime | None = None
    rows: list[DemoKeyFunnelRow]


def _require_enabled() -> None:
    if not get_settings().ENABLE_DEMO_TENANT:
        raise HTTPException(status_code=404, detail="Not Found")


async def _require_enabled_before_body() -> None:
    # Dependencies run before body validation, keeping the disabled route a 404.
    _require_enabled()


def _allowed_origin_host(entry: str) -> str | None:
    entry = entry.strip()
    if not entry:
        return None
    if "://" not in entry:
        entry = "https://" + entry
    return urlsplit(entry).hostname


def _reject_cross_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_host = urlsplit(origin).hostname
    allowed = {request.url.hostname}
    public_url = get_settings().PUBLIC_URL
    if public_url:
        allowed.add(urlsplit(public_url).hostname)
    for entry in get_settings().DEMO_ALLOWED_ORIGINS.split(","):
        host = _allowed_origin_host(entry)
        if host:
            allowed.add(host)
    if origin_host not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "origin_not_allowed",
                "message": (
                    "Cross-origin browser requests are not accepted on this "
                    "endpoint. Call it from a CLI, SDK, or server-side client."
                ),
            },
        )


@router.get("/keys", summary="How to request a demo API key")
async def demo_key_help(request: Request) -> dict[str, object]:
    base_url = get_settings().PUBLIC_URL.rstrip("/") or str(request.base_url).rstrip(
        "/"
    )
    example_url = f"{base_url}/v1/demo/keys"
    return {
        "what_it_does": (
            "Creates a demo sponsor wallet, agent wallet, and wallet-scoped API "
            "key when the demo tenant is enabled. The key is shown once."
        ),
        "post": {
            "method": "POST",
            "path": "/v1/demo/keys",
            "content_type": "application/json",
            "fields": [*_UTM_FIELDS, "referrer"],
        },
        "curl": (
            f"curl -X POST '{example_url}' -H 'Content-Type: application/json' "
            '-d \'{"utm_source":"website"}\''
        ),
        "docs": f"{base_url}/docs",
    }


@router.post(
    "/keys",
    response_model=DemoKeyIssueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint a self-serve demo key (anonymous)",
)
async def issue_demo_key(
    request: Request,
    _: None = Depends(_require_enabled_before_body),
    body: DemoKeyIssueRequest | None = None,
) -> DemoKeyIssueResponse:
    _require_enabled()
    _reject_cross_origin(request)
    settings = get_settings()
    client_id = _client_id(request)

    scope, limit = await check_issuance_limits(client_id)
    if scope == "unavailable":
        log_demo_event("demo_issue_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": ISSUANCE_UNAVAILABLE,
                "message": "Demo issuance is temporarily unavailable. Try again shortly.",
            },
        )
    if scope is not None:
        log_demo_event("demo_issue_rate_limited", scope=scope, limit=limit)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": ISSUANCE_RATE_LIMITED, "scope": scope},
            headers={"Retry-After": "86400" if scope == ISSUE_SCOPE_IP else "3600"},
        )

    attribution = body.attribution() if body is not None else {}
    minted = await mint_demo_keypair(attribution=attribution)
    log_demo_event(
        "demo_key_issued",
        key_id=minted["key_id"],
        wallet_id=minted["wallet_id"],
    )
    base_url = settings.PUBLIC_URL.rstrip("/") if settings.PUBLIC_URL else ""
    return DemoKeyIssueResponse(
        api_key=minted["api_key"],
        key_id=minted["key_id"],
        key_prefix=minted["key_prefix"],
        wallet_id=minted["wallet_id"],
        sponsor_wallet_id=minted["sponsor_wallet_id"],
        tenant=minted["tenant"],
        allowed_tools=minted["allowed_tools"],
        expires_at=minted["expires_at"],
        max_uses=minted["max_uses"],
        budget_credits=minted["budget_credits"],
        base_url=base_url,
        note=(
            "Store the api_key now; it is shown only once. This key is "
            "confined to the demo tenant: it can only ever act on "
            "demo-tenant wallets, and flipping ENABLE_DEMO_TENANT off "
            "disables it instantly."
        ),
    )


@admin_router.get(
    "/funnel",
    response_model=DemoKeyFunnelResponse,
    summary="Admin: demo keys issued vs. used by source (bootstrap only)",
)
async def admin_demo_key_funnel(
    since: datetime | None = None,
    until: datetime | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> DemoKeyFunnelResponse:
    # Reporting remains available after issuance is disabled.
    auth.require_bootstrap_admin()
    rows = await demo_key_funnel(since=since, until=until)
    return DemoKeyFunnelResponse(
        since=since,
        until=until,
        rows=[DemoKeyFunnelRow(**row) for row in rows],
    )
