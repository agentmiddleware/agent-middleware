"""Enterprise IGA bridge: OIDC access tokens -> PolicyBundle grants.

Scope, stated honestly:

- Verifies enterprise OIDC access tokens (Okta, Microsoft Entra ID) against
  operator-configured issuer keys. Verification keys are PINNED in
  configuration (``IGA_TRUSTED_ISSUERS`` carries the JWKS document or a
  public-key PEM inline) — there is NO network JWKS fetch, so a compromised
  IdP hostname cannot rotate keys under us and key rotation is an explicit
  configuration change.
- Maps enterprise groups/roles (Okta ``groups`` claim; Entra ``roles``,
  falling back to ``groups``) to wallet-scoped PolicyBundle definitions via
  ``IGA_GROUP_POLICY_MAP`` and enforces per-principal runtime call caps
  (lifetime ``max_uses`` and a sliding velocity window).
- Cap counters are IN-PROCESS and PER-INSTANCE — the same honesty as
  app/services/velocity_monitor.py owes its DB counters: they reset on
  restart and are not shared across replicas. They bound abuse on a single
  instance; a fleet-wide cap needs a shared store and is out of scope here.

PolicyBundleModel deliberately carries no max_uses/velocity columns; the
runtime caps live in the mapping config and are enforced here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import jwt

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# Providers this bridge understands. The provider selects which token claim
# carries group membership.
_KNOWN_PROVIDERS = ("okta", "entra")

# Issuer-host suffixes used to infer the provider when the operator does not
# set one explicitly. Custom vanity domains cannot be inferred — the operator
# must set "provider" for those, and config parsing fails closed otherwise.
_OKTA_HOST_SUFFIXES = (".okta.com", ".oktapreview.com", ".okta-emea.com")
_ENTRA_HOSTS = ("login.microsoftonline.com", "sts.windows.net")


class IGAError(RuntimeError):
    """IGA failure with a stable snake_case ``reason``.

    Messages never contain raw token material.
    """

    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class EnterprisePrincipal:
    """A verified enterprise (human) identity from a trusted OIDC issuer."""

    subject: str
    provider: str  # "okta" | "entra"
    issuer: str
    email: str | None = None
    # Missing/empty group claims are NOT an error at parse time — an empty
    # tuple simply matches no grants and enforcement blocks with
    # iga_no_matching_role.
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class IGAGrant:
    """One enterprise group's mapped PolicyBundle plus its runtime caps."""

    group: str
    policy_id: str
    max_uses: int | None = None
    velocity_window_seconds: int | None = None
    velocity_max_calls: int | None = None


@dataclass(eq=False)
class IGAUseReservation:
    """Process-local identity for one consumption; never exposed to callers."""

    counter_key: tuple[str, str, str, str, str]
    recorded_at: float
    released: bool = False


@dataclass(frozen=True)
class IGADecision:
    allowed: bool
    reason: str
    group: str | None = None
    policy_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    reservation: IGAUseReservation | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class _IssuerConfig:
    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    provider: str
    jwks: dict[str, Any] | None = None
    public_key_pem: str | None = None


# --- Configuration (parsed lazily, fail-closed) -----------------------------
#
# Settings are re-read on every call rather than captured at import: tests and
# operators rebind the env vars and clear the settings cache after this module
# is imported (same constraint get_auth_context documents).


def _infer_provider(issuer: str) -> str | None:
    host = issuer.split("://", 1)[-1].split("/", 1)[0].lower()
    if any(host == s.lstrip(".") or host.endswith(s) for s in _OKTA_HOST_SUFFIXES):
        return "okta"
    if any(host == h or host.endswith("." + h) for h in _ENTRA_HOSTS):
        return "entra"
    return None


def _trusted_issuers() -> dict[str, _IssuerConfig]:
    """Parse IGA_TRUSTED_ISSUERS. Empty string = IGA disabled ({}).

    Any malformed entry fails the WHOLE config closed (IGAError at use time,
    never at import): a partially-honored trust roster is worse than none.
    """
    raw = get_settings().IGA_TRUSTED_ISSUERS.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise IGAError(
            "iga_config_invalid", "IGA_TRUSTED_ISSUERS is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise IGAError(
            "iga_config_invalid", "IGA_TRUSTED_ISSUERS must be a JSON object"
        )

    issuers: dict[str, _IssuerConfig] = {}
    for issuer, entry in parsed.items():
        if (
            not isinstance(issuer, str)
            or not issuer.strip()
            or not isinstance(entry, dict)
        ):
            raise IGAError(
                "iga_config_invalid", "issuer entries must map URL -> object"
            )

        audience = entry.get("audience")
        if not isinstance(audience, str) or not audience.strip():
            raise IGAError(
                "iga_config_invalid", f"issuer {issuer!r}: audience is required"
            )

        algorithms = entry.get("algorithms")
        if (
            not isinstance(algorithms, list)
            or not algorithms
            or not all(isinstance(a, str) and a.strip() for a in algorithms)
        ):
            raise IGAError(
                "iga_config_invalid",
                f"issuer {issuer!r}: algorithms must be a non-empty list of names",
            )
        # "none" means unsigned; it is never acceptable for a trust decision,
        # explicit configuration included.
        if any(a.strip().lower() == "none" for a in algorithms):
            raise IGAError(
                "iga_config_invalid",
                f"issuer {issuer!r}: algorithm 'none' is never allowed",
            )

        jwks = entry.get("jwks")
        pem = entry.get("public_key_pem")
        # Exactly one key source: both (ambiguous) and neither (unverifiable)
        # fail closed.
        if (jwks is None) == (pem is None):
            raise IGAError(
                "iga_config_invalid",
                f"issuer {issuer!r}: exactly one of jwks / public_key_pem is required",
            )
        if jwks is not None and not isinstance(jwks, dict):
            raise IGAError(
                "iga_config_invalid", f"issuer {issuer!r}: jwks must be an object"
            )
        if pem is not None and (not isinstance(pem, str) or not pem.strip()):
            raise IGAError(
                "iga_config_invalid",
                f"issuer {issuer!r}: public_key_pem must be a non-empty string",
            )

        provider = entry.get("provider")
        if provider is None:
            provider = _infer_provider(issuer)
        if provider not in _KNOWN_PROVIDERS:
            raise IGAError(
                "iga_config_invalid",
                f"issuer {issuer!r}: provider not inferable from host; "
                f"set provider to one of {list(_KNOWN_PROVIDERS)}",
            )

        issuers[issuer] = _IssuerConfig(
            issuer=issuer,
            audience=audience,
            algorithms=tuple(a.strip() for a in algorithms),
            provider=provider,
            jwks=jwks,
            public_key_pem=pem,
        )
    return issuers


def _group_policy_map() -> dict[str, IGAGrant]:
    """Parse IGA_GROUP_POLICY_MAP. Empty string = no grants ({})."""
    raw = get_settings().IGA_GROUP_POLICY_MAP.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise IGAError(
            "iga_config_invalid", "IGA_GROUP_POLICY_MAP is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise IGAError(
            "iga_config_invalid", "IGA_GROUP_POLICY_MAP must be a JSON object"
        )

    def _cap(entry: dict[str, Any], name: str) -> int | None:
        value = entry.get(name)
        if value is None:
            return None
        # bool is an int subclass; a JSON true/false here is a config mistake.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise IGAError(
                "iga_config_invalid", f"{name} must be a positive integer or null"
            )
        return value

    grants: dict[str, IGAGrant] = {}
    for group, entry in parsed.items():
        if not isinstance(group, str) or not group or not isinstance(entry, dict):
            raise IGAError(
                "iga_config_invalid", "group entries must map name -> object"
            )
        policy_id = entry.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise IGAError(
                "iga_config_invalid", f"group {group!r}: policy_id is required"
            )
        window = _cap(entry, "velocity_window_seconds")
        max_calls = _cap(entry, "velocity_max_calls")
        # A velocity cap needs both a window and a limit; half a cap enforces
        # nothing and hides the operator's mistake — fail closed instead.
        if (window is None) != (max_calls is None):
            raise IGAError(
                "iga_config_invalid",
                f"group {group!r}: velocity_window_seconds and velocity_max_calls "
                "must be set together",
            )
        grants[group] = IGAGrant(
            group=group,
            policy_id=policy_id,
            max_uses=_cap(entry, "max_uses"),
            velocity_window_seconds=window,
            velocity_max_calls=max_calls,
        )
    return grants


# --- Token verification ------------------------------------------------------


def token_issuer_is_trusted(token: str) -> bool:
    """Unverified peek used ONLY to route the token to the right auth layer.

    The trust decision happens in :func:`parse_enterprise_token` with full
    verification. Returns False when IGA is disabled, the token cannot be
    parsed at all, or its ``iss`` is not configured — so internal EdDSA JWTs
    (iss "agent-middleware-api") fall through to the existing auth flow
    untouched. Raises IGAError only for malformed IGA configuration.
    """
    issuers = _trusted_issuers()
    if not issuers:
        return False
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return False
    iss = unverified.get("iss")
    return isinstance(iss, str) and iss in issuers


def is_iga_issuer_token(token: str) -> bool:
    """Routing predicate: does this bearer belong to the IGA layer at all?

    True ONLY when IGA_TRUSTED_ISSUERS is configured AND the token's
    UNVERIFIED ``iss`` claim names a configured issuer. The unverified peek
    is acceptable because this function only ROUTES the token to the right
    auth layer — it grants nothing; full verification (pinned key, algorithm
    allowlist, audience, issuer, expiry) still happens in
    :func:`parse_enterprise_token` before any trust decision.

    Fails closed: an unparseable token, a disabled IGA layer, or a malformed
    IGA configuration all return False, which leaves the bearer to the
    internal-JWT verifier (where it is rejected). A malformed configuration
    is additionally logged at error level (stable reason only) because it
    disables routing for the ENTIRE enterprise layer — silently, it would be
    indistinguishable from "IGA off". Never logs token material.
    """
    try:
        return token_issuer_is_trusted(token)
    except IGAError as exc:
        # Malformed IGA configuration: route nothing to the IGA layer. The
        # bearer then faces the internal-JWT verifier and fails closed (401);
        # parse_enterprise_token still surfaces the config error wherever the
        # layer is actually exercised. Log the stable reason ONLY — never the
        # token or any of its claims.
        logger.error(
            "IGA configuration unusable (%s): enterprise bearer routing is "
            "disabled; bearers fail closed against the internal-JWT verifier",
            exc.reason,
        )
        return False


def _resolve_verification_key(
    config: _IssuerConfig, header: dict[str, Any], alg: str
) -> Any:
    """Select the pinned verification key for this token's header.

    Key material comes exclusively from configuration; the token header only
    selects WHICH pinned key (by ``kid``) — it can never introduce one.
    """
    if config.public_key_pem is not None:
        return config.public_key_pem

    keys = config.jwks.get("keys") if config.jwks else None
    if not isinstance(keys, list) or not keys:
        raise IGAError("iga_config_invalid", "pinned JWKS has no 'keys' list")

    def _build(jwk_dict: dict[str, Any]) -> Any:
        try:
            return jwt.PyJWK.from_dict(jwk_dict, algorithm=alg).key
        except (jwt.exceptions.PyJWKError, jwt.exceptions.InvalidKeyError) as exc:
            # The pinned key itself is unusable — an operator problem, not a
            # caller problem.
            raise IGAError(
                "iga_config_invalid", "pinned JWK could not be loaded"
            ) from exc

    kid = header.get("kid")
    if kid is not None:
        for jwk_dict in keys:
            if isinstance(jwk_dict, dict) and jwk_dict.get("kid") == kid:
                return _build(jwk_dict)
        raise IGAError(
            "iga_signing_key_not_found", "no pinned key matches the token kid"
        )
    # No kid: unambiguous only when exactly one key is pinned.
    if len(keys) == 1 and isinstance(keys[0], dict):
        return _build(keys[0])
    raise IGAError(
        "iga_signing_key_not_found", "token has no kid and multiple keys are pinned"
    )


def _normalize_groups(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def parse_enterprise_token(token: str) -> EnterprisePrincipal:
    """Verify an enterprise OIDC access token and extract its principal.

    The unverified peek below selects the issuer configuration ONLY; the
    trust decision is the strict ``jwt.decode`` that follows, whose
    algorithm allowlist, audience, issuer, and key all come from pinned
    configuration — never from the token itself.
    """
    issuers = _trusted_issuers()
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise IGAError("iga_token_malformed", "token could not be parsed") from exc

    iss = unverified.get("iss")
    if not isinstance(iss, str) or not iss:
        raise IGAError("iga_token_malformed", "token carries no issuer claim")
    config = issuers.get(iss)
    if config is None:
        raise IGAError(
            "iga_issuer_not_trusted", "token issuer is not a configured IGA issuer"
        )

    # Fail fast on algorithm confusion (e.g. an HS256 token against an
    # RS256-only issuer) with a distinct reason. jwt.decode() below enforces
    # the same allowlist regardless — this check is not the only line.
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in config.algorithms:
        raise IGAError(
            "iga_algorithm_not_allowed", "token algorithm is not in the allowlist"
        )

    key = _resolve_verification_key(config, header, alg)

    try:
        claims = jwt.decode(
            token,
            key=key,
            # EXACTLY the configured allowlist — never taken from the header.
            algorithms=list(config.algorithms),
            audience=config.audience,
            issuer=config.issuer,
            # exp/nbf are verified by default when present; an enterprise
            # access token without exp is not acceptable.
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise IGAError("iga_token_expired") from exc
    except jwt.ImmatureSignatureError as exc:
        raise IGAError("iga_token_not_yet_valid") from exc
    except jwt.InvalidAudienceError as exc:
        raise IGAError("iga_audience_mismatch") from exc
    except jwt.InvalidIssuerError as exc:
        raise IGAError("iga_issuer_not_trusted") from exc
    except jwt.MissingRequiredClaimError as exc:
        if exc.claim == "aud":
            raise IGAError("iga_audience_mismatch") from exc
        raise IGAError(
            "iga_token_malformed", f"missing required claim {exc.claim}"
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise IGAError("iga_signature_invalid") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise IGAError("iga_algorithm_not_allowed") from exc
    except jwt.InvalidTokenError as exc:
        raise IGAError("iga_token_invalid") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise IGAError("iga_token_malformed", "token carries no subject claim")

    # Okta puts group membership in `groups`; Entra ID app roles arrive in
    # `roles`, with `groups` as the fallback for group-claims configurations.
    if config.provider == "okta":
        groups = _normalize_groups(claims.get("groups"))
    else:
        groups = _normalize_groups(claims.get("roles"))
        if not groups:
            groups = _normalize_groups(claims.get("groups"))

    email = claims.get("email")
    return EnterprisePrincipal(
        subject=subject,
        provider=config.provider,
        issuer=config.issuer,
        email=email if isinstance(email, str) else None,
        groups=groups,
    )


# --- Grant resolution and runtime enforcement --------------------------------


def resolve_policy_grants(principal: EnterprisePrincipal) -> list[IGAGrant]:
    """Grants = IGA_GROUP_POLICY_MAP ∩ principal.groups (config order).

    An empty list is a valid outcome — enforcement turns it into the
    iga_no_matching_role block.
    """
    mapping = _group_policy_map()
    member = set(principal.groups)
    return [grant for group, grant in mapping.items() if group in member]


# In-process, per-instance counters (see module docstring for the honesty
# about scope). Keyed by (issuer, subject, tool, group, policy_id): subjects
# are unique only within an issuer, so the issuer participates in the key,
# and each grant's caps bound the calls THAT grant authorized — a shared
# per-principal key would let one grant's short-window pruning erase the
# history a longer-window grant still needs, and would make two grants'
# max_uses budgets draw down a single counter. Two groups may map to the
# same policy_id with different caps, so the group participates too.
_CounterKey = tuple[str, str, str, str, str]
_lifetime_uses: dict[_CounterKey, int] = {}
_window_calls: dict[_CounterKey, deque[IGAUseReservation]] = {}
_counter_lock: asyncio.Lock = asyncio.Lock()

# Monotonic time source for the velocity window. Module-level indirection so
# tests can inject a fake clock; monotonic because wall-clock steps must not
# widen or collapse a velocity window.
_monotonic = time.monotonic

# Higher rank = more informative to the caller. A cap denial proves the
# principal HAD access and exhausted it; tool-not-allowed proves a live grant
# existed; inactive/not-found say only that the mapping is stale.
_REASON_RANK = {
    "iga_no_matching_role": 0,
    "iga_policy_not_found": 1,
    "iga_policy_inactive": 2,
    "iga_tool_not_allowed": 3,
    "iga_max_uses_exceeded": 4,
    "iga_velocity_exceeded": 4,
}


def reset_iga_counters() -> None:
    """Drop all in-process cap counters. Test hook.

    Also rebinds the lock: asyncio primitives bind to the first event loop
    that awaits them, and each test runs on a fresh loop.
    """
    global _counter_lock
    _counter_lock = asyncio.Lock()
    _lifetime_uses.clear()
    _window_calls.clear()


async def enforce_tool_call(
    principal: EnterprisePrincipal, tool_name: str, *, consume: bool = True
) -> IGADecision:
    """Decide whether this enterprise principal may invoke ``tool_name``.

    First grant whose bundle is active, allows the tool, and passes the
    runtime caps wins; the use is recorded atomically with the ALLOW under
    the counter lock. When every grant is exhausted, the most informative
    denial gathered is returned.
    """
    grants = resolve_policy_grants(principal)
    if not grants:
        # THE acceptance criterion: a principal lacking any mapped Okta/Entra
        # role is blocked outright.
        return IGADecision(
            allowed=False,
            reason="iga_no_matching_role",
            details={"groups": list(principal.groups)},
        )

    # Deferred import: keep this module import-light so app.core.auth can
    # import it without dragging the DB layer in at auth-import time.
    from app.services.policies import get_policy_bundle

    candidates: list[IGADecision] = []
    eligible: list[IGAGrant] = []
    for grant in grants:
        bundle = await get_policy_bundle(grant.policy_id)
        if bundle is None:
            candidates.append(
                IGADecision(
                    False, "iga_policy_not_found", grant.group, grant.policy_id, {}
                )
            )
            continue
        if not bundle.is_active:
            candidates.append(
                IGADecision(
                    False, "iga_policy_inactive", grant.group, grant.policy_id, {}
                )
            )
            continue
        # allowed_tools follows evaluate_wallet_policy's allow/deny semantics:
        # None (a NULL column) means unrestricted — any tool passes; a list is
        # a strict allowlist. A corrupt stored value never reads as None:
        # app/services/policies.py _decode_list returns [] for it, so it
        # denies every tool here just as evaluate_wallet_policy denies it
        # with policy_constraint_corrupt.
        allowed_tools = bundle.allowed_tools
        if allowed_tools is not None and tool_name not in allowed_tools:
            candidates.append(
                IGADecision(
                    False,
                    "iga_tool_not_allowed",
                    grant.group,
                    grant.policy_id,
                    {"tool": tool_name, "allowed_tools": allowed_tools},
                )
            )
            continue
        eligible.append(grant)

    if eligible and not consume:
        # Existing-action access checks current grants without spending another use.
        grant = eligible[0]
        return IGADecision(True, "allowed", grant.group, grant.policy_id, {})
    if eligible:
        # Check-and-record must be atomic: the same lock covers the cap read,
        # the decision, and the increment, so two concurrent calls cannot both
        # observe the last remaining use.
        async with _counter_lock:
            now = _monotonic()
            for grant in eligible:
                # Per-grant key: each grant's counters track only the calls
                # it authorized (see the _CounterKey comment above).
                counter_key: _CounterKey = (
                    principal.issuer,
                    principal.subject,
                    tool_name,
                    grant.group,
                    grant.policy_id,
                )
                used = _lifetime_uses.get(counter_key, 0)
                if grant.max_uses is not None and used >= grant.max_uses:
                    candidates.append(
                        IGADecision(
                            False,
                            "iga_max_uses_exceeded",
                            grant.group,
                            grant.policy_id,
                            {"used": used, "limit": grant.max_uses},
                        )
                    )
                    continue
                # Bound to locals so the None-narrowing survives into the
                # arithmetic below (config guarantees the pair is set together).
                window_seconds = grant.velocity_window_seconds
                max_calls = grant.velocity_max_calls
                has_velocity_cap = window_seconds is not None and max_calls is not None
                if window_seconds is not None and max_calls is not None:
                    window = _window_calls.get(counter_key)
                    if window is not None:
                        cutoff = now - float(window_seconds)
                        while window and window[0].recorded_at <= cutoff:
                            window.popleft()
                        if not window:
                            # Fully aged out: drop the key so idle principals
                            # do not pin empty deques for the life of the
                            # process. Re-created below on the next ALLOW.
                            del _window_calls[counter_key]
                            window = None
                    if window is not None and len(window) >= max_calls:
                        candidates.append(
                            IGADecision(
                                False,
                                "iga_velocity_exceeded",
                                grant.group,
                                grant.policy_id,
                                {
                                    "window_seconds": window_seconds,
                                    "calls_in_window": len(window),
                                    "limit": max_calls,
                                },
                            )
                        )
                        continue
                # ALLOW: record the use before releasing the lock. Window
                # history is recorded only for velocity-capped grants so the
                # per-key deque stays bounded by the cap itself.
                _lifetime_uses[counter_key] = used + 1
                reservation = IGAUseReservation(counter_key, now)
                if has_velocity_cap:
                    _window_calls.setdefault(counter_key, deque()).append(reservation)
                return IGADecision(
                    allowed=True,
                    reason="allowed",
                    group=grant.group,
                    policy_id=grant.policy_id,
                    details={"used": used + 1},
                    reservation=reservation,
                )

    # Every grant was exhausted: surface the most informative denial. max()
    # keeps the first of equally ranked candidates, preserving grant order.
    best = max(candidates, key=lambda d: _REASON_RANK.get(d.reason, 0))
    return best


async def release_tool_use(
    principal: EnterprisePrincipal,
    tool_name: str,
    *,
    group: str,
    policy_id: str,
    reservation: IGAUseReservation | None = None,
) -> None:
    """Compensate one recorded use whose action never dispatched.

    :func:`enforce_tool_call` records a use atomically with its ALLOW, but
    later pre-dispatch gates can still refuse the action — e.g. an
    insufficient-funds refusal that charges nothing and dispatches nothing.
    Without compensation a ``max_uses`` budget (and the velocity window)
    burns down on actions that never happened: a max_uses=1 principal who
    hits insufficient funds once would be locked out forever.

    ``group``/``policy_id`` identify the exact grant the ALLOW decision was
    issued under. The opaque reservation from that decision identifies the
    exact recorded use, including when later calls have completed or this use
    has already aged out. Missing, mismatched or already released identities
    fail closed without changing counters. The marker stays only with the
    request and its bounded velocity window; no unbounded reservation registry
    is retained.
    """
    key: _CounterKey = (
        principal.issuer,
        principal.subject,
        tool_name,
        group,
        policy_id,
    )
    async with _counter_lock:
        if (
            reservation is None
            or reservation.counter_key != key
            or reservation.released
        ):
            return
        reservation.released = True
        used = _lifetime_uses.get(key, 0)
        if used > 1:
            _lifetime_uses[key] = used - 1
        elif used == 1:
            del _lifetime_uses[key]
        window = _window_calls.get(key)
        if window:
            try:
                window.remove(reservation)
            except ValueError:
                pass  # This exact call already aged out; keep newer calls.
            if not window:
                del _window_calls[key]
