"""
API Key + JWT Bearer authentication for agent consumers.

Agents pass credentials via:
- X-API-Key header (legacy, long-lived keys)
- Authorization: Bearer <jwt> header (modern, short-lived tokens)
"""

import hmac
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from .config import get_settings
from .oidc_iga import EnterprisePrincipal, IGADecision, IGAError, is_iga_issuer_token
from .trust_mode import is_production_like_environment

settings = get_settings()

# Static development/training keys (STATIC_DEV_API_KEYS) must carry this
# prefix. It keeps dev keys greppable/scannable and means a rotated
# amw_live_ bootstrap key pasted into the wrong variable never
# authenticates through the static-dev path.
STATIC_DEV_KEY_PREFIX = "amw_dev_"

api_key_header = APIKeyHeader(
    name=settings.API_KEY_HEADER,
    auto_error=False,
    description="API key for agent authentication. Pass in the X-API-Key header.",
)


bearer_header = HTTPBearer(
    auto_error=False,
    description="Bearer authentication via Authorization; this header takes precedence over X-API-Key.",
)


@dataclass(frozen=True)
class AuthContext:
    """Authenticated caller details used for tenant-scoped authorization."""

    source: str
    raw_key: str
    key_id: str | None = None
    wallet_id: str | None = None
    is_bootstrap_admin: bool = False
    scopes: list[str] = None  # type: ignore[assignment]
    # Enterprise (Okta/Entra) bearer that accompanied an X-API-Key call. Set
    # ONLY when the Authorization header carried a token whose UNVERIFIED
    # issuer names a pinned IGA issuer (see get_auth_context). It is identity
    # attribution for the human behind the agent, NEVER an API credential:
    # enforcement points (app/core/oidc_iga.parse_enterprise_token +
    # enforce_tool_call) fully verify it before any trust decision.
    enterprise_bearer_token: str | None = None

    def __post_init__(self):
        if self.scopes is None:
            object.__setattr__(self, "scopes", [])

    def require_wallet_access(self, wallet_id: str | None) -> None:
        """Allow bootstrap admins or the exact wallet owning a DB-backed key.

        ``wallet_id`` may be ``None`` for a resource with no owning wallet (e.g.
        a sandbox environment created by a bootstrap admin). A non-admin caller
        always has a concrete ``self.wallet_id``, so an ownerless resource is
        correctly denied to everyone but bootstrap admins.
        """
        if self.is_bootstrap_admin:
            return
        if self.wallet_id is not None and self.wallet_id == wallet_id:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "wallet_access_denied",
                "message": "API key is not authorized for this wallet.",
                "wallet_id": wallet_id,
            },
        )

    def require_bootstrap_admin(self) -> None:
        """Allow only trusted bootstrap/admin environment keys."""
        if self.is_bootstrap_admin:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "admin_access_denied",
                "message": "This operation requires a bootstrap admin API key.",
            },
        )


# Internal marker on a response that failed *authentication* rather than
# authorization. RateLimitMiddleware charges those to its shared
# rejected-credential bucket and strips the header before the response leaves
# the innermost middleware, so it is never a public surface. It exists because
# an unknown-but-well-formed key is refused with 403, not 401, and the limiter
# must not confuse that with an authenticated caller denied on scope
# (wallet_access_denied, insufficient_scope), whose denials are ordinary
# governed-loop traffic.
CREDENTIAL_REJECTED_HEADER = "X-Credential-Rejected"
_CREDENTIAL_REJECTED = {CREDENTIAL_REJECTED_HEADER: "1"}


@dataclass
class CredentialAcceptance:
    """Whether ``get_auth_context`` accepted credentials during one request."""

    accepted: bool = False


# The converse signal, for RateLimitMiddleware's shared pre-auth bucket. A
# request that never authenticates — a public route, a 404 — is never refused
# either, so the rejected marker alone let a caller pick a fresh per-key
# bucket by inventing an X-API-Key value per request; the limiter therefore
# keeps the reservation unless this comes back accepted. A ContextVar rather
# than request state because get_auth_context is also called without a
# Request; a mutable holder rather than a flag because call_next runs the app
# in a child task whose context is a copy, so only a shared object carries the
# answer back out.
CREDENTIAL_ACCEPTANCE: ContextVar[CredentialAcceptance | None] = ContextVar(
    "credential_acceptance", default=None
)


async def get_auth_context(
    api_key: str | None = Security(api_key_header),
    authorization: Annotated[str | None, Header()] = None,
    _bearer: HTTPAuthorizationCredentials | None = Security(bearer_header),
) -> AuthContext:
    """
    Validate credentials and return caller context.

    Checks in order:
    1. Authorization: Bearer <jwt> (modern, short-lived)
    2. X-API-Key header (legacy, long-lived keys)

    Settings are read per call, not from the module-level singleton: tests and
    the Postgres CI job rebind VALID_API_KEYS and clear the settings cache after
    this module is already imported, and a captured `settings` would keep
    serving the stale key list.
    """
    # HTTPBearer declares the OpenAPI alternative; raw Authorization stays
    # authoritative so malformed headers cannot fall back to an API key.
    context = await _resolve_auth_context(api_key, authorization)
    acceptance = CREDENTIAL_ACCEPTANCE.get()
    if acceptance is not None:
        acceptance.accepted = True
    return context


async def _resolve_auth_context(
    api_key: str | None,
    authorization: str | None,
) -> AuthContext:
    """Authenticate the caller; ``get_auth_context`` records the acceptance."""
    settings = get_settings()

    # A presented Authorization header is authoritative. Never fall back to a
    # concurrently supplied API key when its scheme, shape, or token is invalid:
    # doing so would let an invalid bearer credential authenticate as a different
    # principal than the caller intended.
    if authorization is not None:
        token = _parse_bearer_authorization(authorization)
        # Enterprise IGA reroute — deliberately narrow. is_iga_issuer_token
        # peeks ONLY at the token's unverified `iss` claim and is True solely
        # when it names an issuer pinned in IGA_TRUSTED_ISSUERS; with IGA
        # disabled (the default) it is always False, so every bearer keeps
        # today's internal-JWT handling byte for byte. When an enterprise
        # bearer rides alongside an X-API-Key, the API key is the
        # authenticating credential (the recursive call below runs the
        # standard API-key path unchanged) and the bearer is carried as
        # human-identity attribution that the IGA enforcement points fully
        # VERIFY (pinned key, algorithm allowlist, audience, issuer, expiry)
        # before any trust decision — the unverified peek here only routes,
        # it never authenticates. An enterprise bearer with NO accompanying
        # API key is not an API credential: it falls through to
        # _auth_from_jwt and fails with 401 exactly as before.
        if api_key and is_iga_issuer_token(token):
            context = await _resolve_auth_context(api_key, None)
            return replace(context, enterprise_bearer_token=token)
        return await _auth_from_jwt(token)

    # Preserve the pre-header direct-call/X-API-Key compatibility path.
    if api_key and api_key.startswith("Bearer "):
        token = api_key[7:].strip()
        return await _auth_from_jwt(token)

    if api_key is None:
        detail = {
            "error": "missing_credentials",
            "message": "X-API-Key or Authorization: Bearer header is required.",
            "docs": "/docs",
        }
        # On a local instance that opted into self-serve dev keys, point a
        # bootstrapping agent at the credential it can actually mint here.
        # Gated so no production surface ever advertises it (production-like
        # environments refuse to boot with the flag set anyway).
        if settings.ENABLE_DEV_KEY_SELF_PROVISION and not (
            is_production_like_environment(settings.ENVIRONMENT)
        ):
            detail["self_provision"] = (
                "This local instance allows self-serve dev keys: POST "
                "/v1/dev-keys/self-provision (empty body) to mint a "
                "wallet-scoped key, then send it as X-API-Key."
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
        )

    # RED TEAM FIX: Reject empty or whitespace-only keys.
    stripped = api_key.strip()
    if not stripped or len(stripped) < 8:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_api_key",
                "message": "API key must be at least 8 characters.",
            },
        )

    # A JWT-shaped value (a raw token without "Bearer ") is verified exactly as
    # the Authorization header would verify it, and never falls through to
    # API-key lookup: a token that fails to parse is already a 401, and an
    # unexpected failure (a liveness check that could not run) must surface
    # here as it does there, not be retried as a key — or, in DEBUG open
    # mode, as a bootstrap admin.
    if stripped.count(".") == 2 and len(stripped) > 50:
        return await _auth_from_jwt(stripped)

    valid_keys = [k.strip() for k in settings.VALID_API_KEYS.split(",") if k.strip()]

    if any(hmac.compare_digest(stripped, key) for key in valid_keys):
        return AuthContext(
            source="env",
            raw_key=stripped,
            is_bootstrap_admin=True,
        )

    # Static development/training keys: bootstrap-admin power in
    # local-compatible environments only. The environment gate is
    # defense-in-depth — validate_trust_mode_guardrails already refuses to
    # boot a production-like deployment with STATIC_DEV_API_KEYS set — so a
    # leaked dev key is worthless against production even if the guardrail
    # were bypassed. Exempt from rotation by design: see
    # docs/static-dev-api-keys.md.
    static_dev_keys = (
        []
        if is_production_like_environment(settings.ENVIRONMENT)
        else _parse_static_dev_keys(settings.STATIC_DEV_API_KEYS)
    )
    if any(hmac.compare_digest(stripped, key) for key in static_dev_keys):
        return AuthContext(
            source="static-dev",
            raw_key=stripped,
            is_bootstrap_admin=True,
        )

    try:
        from ..services.api_key_service import get_api_key_service

        db_key = await get_api_key_service().validate_key(stripped)
    except RuntimeError:
        db_key = None

    if db_key:
        return AuthContext(
            source="db",
            raw_key=stripped,
            key_id=db_key.key_id,
            wallet_id=db_key.wallet_id,
            is_bootstrap_admin=False,
        )

    # Configured static dev keys close DEBUG open mode the same way
    # VALID_API_KEYS does: once keys exist, an unknown key fails closed
    # instead of authenticating as an anonymous bootstrap admin.
    if valid_keys or settings.STATIC_DEV_API_KEYS.strip() or not settings.DEBUG:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "invalid_api_key",
                "message": "The provided API key is not authorized.",
            },
            headers=dict(_CREDENTIAL_REJECTED),
        )

    if is_production_like_environment(settings.ENVIRONMENT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "invalid_api_key",
                "message": "The provided API key is not authorized.",
            },
            headers=dict(_CREDENTIAL_REJECTED),
        )

    return AuthContext(
        source="env",
        raw_key=stripped,
        is_bootstrap_admin=True,
    )


async def reject_anonymous_production_catalog(request: Request) -> None:
    """Refuse unauthenticated tool catalogs on production-like boots.

    Local-compatible environments keep anonymous discovery so quickstart
    and the in-repo stranger test can bootstrap. A hosted production-like
    origin must not advertise invokable tools (or their schemas) to a
    stranger: the same credentials that authorize invoke are required to
    list them. Receipt verification keys stay on the unauthenticated
    well-known documents.
    """
    settings = get_settings()
    if not is_production_like_environment(settings.ENVIRONMENT):
        return
    await get_auth_context(
        api_key=request.headers.get(settings.API_KEY_HEADER.lower())
        or request.headers.get("x-api-key"),
        authorization=request.headers.get("authorization"),
    )


def _parse_static_dev_keys(configured: str) -> list[str]:
    """Parse STATIC_DEV_API_KEYS, honoring only ``amw_dev_``-prefixed entries.

    Non-prefixed entries are configuration mistakes (most dangerously, a live
    bootstrap key in the wrong variable) and never authenticate.
    """
    return [
        key
        for key in (k.strip() for k in configured.split(","))
        if key.startswith(STATIC_DEV_KEY_PREFIX)
    ]


def _parse_bearer_authorization(authorization: str) -> str:
    """Accept exactly ``Bearer <nonempty-token>`` with no extra whitespace."""
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        token = ""
    else:
        token = authorization[len(prefix) :]

    if not token or token != token.strip() or any(char.isspace() for char in token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_token",
                "message": "Authorization header must be exactly 'Bearer <token>'.",
            },
        )
    return token


async def _auth_from_jwt(token: str) -> AuthContext:
    """Verify JWT and return AuthContext."""
    from .jwt import get_jwt_service, JWTError

    jwt_svc = get_jwt_service()
    try:
        payload = jwt_svc.verify_access_token(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_token",
                "message": str(e),
            },
        ) from e

    if payload.key_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "unbound_access_token",
                "message": (
                    "This access token is not bound to an API key; authenticate "
                    "with an active API key."
                ),
            },
        )

    from ..services.api_key_service import get_api_key_service

    if not await get_api_key_service().consume_derived_key_use(
        payload.key_id, payload.sub
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "no_active_api_key",
                "message": (
                    "The API key that issued this access token is no longer active."
                ),
            },
        )

    return AuthContext(
        source="jwt",
        # Not a credential: a stable, non-secret handle for the API key that
        # minted this token. A truncated token cannot serve: every EdDSA JWT
        # opens with the same encoded header, so all JWT callers shared it.
        raw_key=f"jwt:{payload.key_id}",
        wallet_id=payload.sub,
        key_id=payload.key_id,
        is_bootstrap_admin=False,
        scopes=payload.scopes,
    )


async def verify_api_key(
    api_key: str | None = Security(api_key_header),
) -> str:
    """
    Validate the provided API key.
    Returns the raw key on success for backwards-compatible dependencies.
    """
    context = await get_auth_context(api_key)
    return context.raw_key


async def get_enterprise_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> EnterprisePrincipal | None:
    """Optional enterprise IGA identity layer (app.core.oidc_iga).

    Returns None — leaving every existing auth path untouched — when the
    Authorization header is absent or not Bearer-shaped, when IGA is disabled
    (IGA_TRUSTED_ISSUERS empty), or when the bearer token's issuer is not an
    IGA-trusted issuer (internal EdDSA JWTs land here and continue through
    get_auth_context unchanged). A token FROM a trusted enterprise issuer
    that fails verification raises 401 with the IGAError reason: a bad
    enterprise token must never fall through to another auth path.
    """
    from .oidc_iga import parse_enterprise_token, token_issuer_is_trusted

    # Settings are re-read per call for the same reason get_auth_context
    # documents: tests rebind IGA_* env vars after this module is imported.
    settings = get_settings()
    if not settings.IGA_TRUSTED_ISSUERS.strip():
        return None
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    if not token:
        return None

    try:
        if not token_issuer_is_trusted(token):
            return None
        return parse_enterprise_token(token)
    except IGAError as exc:
        # Covers both a failed verification of an IGA-issuer token and a
        # malformed IGA configuration while the layer is enabled: fail closed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": exc.reason,
                "message": str(exc),
            },
        ) from exc


def require_enterprise_tool_access(tool_name: str):
    """Dependency factory: enforce IGA group->PolicyBundle grants for a tool.

    The returned dependency 403s with the IGA decision reason when an
    enterprise principal is present but no mapped, active PolicyBundle grants
    ``tool_name`` (or a runtime cap is exhausted). When no enterprise
    principal is presented it returns None and enforces nothing — the layer
    is optional and API-key/JWT callers are governed by the existing paths.
    """

    async def _enterprise_tool_access(
        principal: EnterprisePrincipal | None = Depends(get_enterprise_principal),
    ) -> IGADecision | None:
        if principal is None:
            return None
        from .oidc_iga import enforce_tool_call

        decision = await enforce_tool_call(principal, tool_name)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": decision.reason,
                    "message": f"Enterprise access to {tool_name!r} is denied.",
                },
            )
        return decision

    return _enterprise_tool_access
