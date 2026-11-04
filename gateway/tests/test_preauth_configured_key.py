"""A configured env key is not charged to the shared per-address pre-auth bucket.

Behind a shared proxy address, other callers' invalid keys fill the address's
pre-auth bucket. A key recognized from ``VALID_API_KEYS`` before authentication
skips that bucket only. Everything else keeps the reservation: unknown keys,
blank keys, partial matches, database keys, JWTs and bearers.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
from app.core import rate_limiter as rate_limiter_module
from app.core.rate_limiter import RateLimitMiddleware

CONFIGURED = "configured-key-0001"
PEER = ("203.0.113.10", 5000)
OTHER_PEER = ("203.0.113.11", 5000)


@pytest.fixture
def configured_keys(monkeypatch):
    """Configure VALID_API_KEYS, with blank entries the parser must drop."""

    def configure(value: str) -> None:
        monkeypatch.setenv("VALID_API_KEYS", value)
        get_settings.cache_clear()

    configure(f" {CONFIGURED} ,, ,configured-key-0002")
    try:
        yield configure
    finally:
        get_settings.cache_clear()


def _app() -> FastAPI:
    api = FastAPI()

    @api.get("/v1/wallets")
    async def _wallets(auth: AuthContext = Depends(get_auth_context)):
        return {"source": auth.source}

    return api


def _limited(limit: int = 2) -> RateLimitMiddleware:
    limiter = RateLimitMiddleware(_app(), requests_per_minute=limit)
    limiter._redis_url = ""
    assert limiter.preauth_limit == limit * 10
    return limiter


def _client(limiter: RateLimitMiddleware, peer=PEER) -> AsyncClient:
    transport = ASGITransport(app=limiter, client=peer)
    return AsyncClient(transport=transport, base_url="http://test")


async def _exhaust_address(http: AsyncClient, limiter: RateLimitMiddleware) -> None:
    """Fill the address's pre-auth bucket with rotating invalid keys."""
    for index in range(limiter.preauth_limit):
        response = await http.get(
            "/v1/wallets", headers={"X-API-Key": f"invalid-key-{index:03d}"}
        )
        assert response.status_code == 403
    over = await http.get("/v1/wallets", headers={"X-API-Key": "invalid-key-over"})
    assert over.status_code == 429


@pytest.mark.anyio
async def test_unknown_key_is_still_refused_when_the_address_is_exhausted(
    configured_keys,
) -> None:
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        unknown = await http.get(
            "/v1/wallets", headers={"X-API-Key": "unknown-key-00001"}
        )
    assert unknown.status_code == 429
    assert "without accepted credentials" in unknown.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": ""},
        {"X-API-Key": "   "},
        {"X-API-Key": ","},
    ],
    ids=["missing", "empty", "blank", "comma"],
)
async def test_blank_or_missing_key_never_matches_an_empty_configured_entry(
    configured_keys, headers
) -> None:
    """VALID_API_KEYS above holds blank entries; they must match nothing."""
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        response = await http.get("/v1/wallets", headers=headers)
    assert response.status_code == 429


@pytest.mark.anyio
@pytest.mark.parametrize(
    "presented",
    [
        CONFIGURED[:-1],
        CONFIGURED + "x",
        CONFIGURED.upper(),
        f"{CONFIGURED},configured-key-0002",
        CONFIGURED + "é",
    ],
    ids=["prefix", "superset", "case", "comma-joined", "non-ascii-superset"],
)
async def test_partial_or_altered_configured_key_keeps_the_reservation(
    configured_keys, presented
) -> None:
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        response = await http.get(
            "/v1/wallets", headers={"X-API-Key": presented.encode("utf-8")}
        )
    assert response.status_code == 429


@pytest.mark.anyio
async def test_configured_key_gets_through_an_exhausted_address(
    configured_keys,
) -> None:
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        accepted = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
        padded = await http.get(
            "/v1/wallets", headers={"X-API-Key": f"  {CONFIGURED}  "}
        )
    assert accepted.status_code == 200
    assert accepted.json() == {"source": "env"}
    assert padded.status_code == 200


@pytest.mark.anyio
async def test_per_key_bucket_still_limits_a_configured_key(configured_keys) -> None:
    """The skip is for the address bucket only; the key's own limit stands."""
    limiter = _limited(limit=2)
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        statuses = [
            (
                await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
            ).status_code
            for _ in range(3)
        ]
        refused = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
    assert statuses == [200, 200, 429]
    assert "without accepted credentials" not in refused.text


@pytest.mark.anyio
async def test_removing_the_key_from_settings_charges_it_again(
    configured_keys,
) -> None:
    """No stale bypass: the decision is made from settings on every request."""
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        before = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
        configured_keys("configured-key-0002")
        after = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
    assert before.status_code == 200
    assert after.status_code == 429
    assert "without accepted credentials" in after.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "shaped_key",
    ["Bearer configured-key-0003", "e" * 30 + "." + "f" * 30 + "." + "g" * 30],
    ids=["bearer-prefixed", "jwt-shaped"],
)
async def test_configured_values_that_auth_routes_elsewhere_are_not_skipped(
    configured_keys, shaped_key
) -> None:
    """Auth sends these shapes to JWT handling before the env-key match.

    Even when such a value is listed in VALID_API_KEYS, the limiter must not
    treat it as a recognized env key.
    """
    configured_keys(f"{CONFIGURED},{shaped_key}")
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        response = await http.get("/v1/wallets", headers={"X-API-Key": shaped_key})
    assert response.status_code == 429


@pytest.mark.anyio
async def test_configured_key_under_eight_characters_is_not_skipped(
    configured_keys,
) -> None:
    """Auth refuses keys under 8 characters with a 401, so they never skip."""
    configured_keys(f"{CONFIGURED},short12")
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        response = await http.get("/v1/wallets", headers={"X-API-Key": "short12"})
    assert response.status_code == 429


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("trusted", "expected_blocked_second"),
    [("203.0.113.50", False), ("198.51.100.1", True)],
    ids=["ingress-trusted", "ingress-not-trusted"],
)
async def test_forwarded_allow_ips_decides_whether_callers_share_a_bucket(
    configured_keys, monkeypatch, trusted, expected_blocked_second
) -> None:
    """The documented contract: uvicorn trusts X-Forwarded-For only from the
    addresses in FORWARDED_ALLOW_IPS, and the limiter reads the result.

    Two callers reach the gateway through one ingress at 203.0.113.50. With
    that address trusted they are separate clients; otherwise they share it.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    limiter = _limited()
    app = ProxyHeadersMiddleware(limiter, trusted_hosts=trusted)
    transport = ASGITransport(app=app, client=("203.0.113.50", 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        for index in range(limiter.preauth_limit):
            await http.get(
                "/v1/wallets",
                headers={
                    "X-API-Key": f"invalid-key-{index:03d}",
                    "X-Forwarded-For": "192.0.2.1",
                },
            )
        second_caller = await http.get(
            "/v1/wallets",
            headers={"X-API-Key": "invalid-key-fresh", "X-Forwarded-For": "192.0.2.2"},
        )
    assert (second_caller.status_code == 429) is expected_blocked_second


@pytest.mark.parametrize(
    ("presented", "expected", "equal"),
    [
        ("configured-key-0001", "configured-key-0001", True),
        ("configured-key-0001", "configured-key-0002", False),
        ("configured-key-000", "configured-key-0001", False),
        ("configured-key-00011", "configured-key-0001", False),
        ("configured-key-0001", "configured-key-0001\0", False),
        ("configured-key-0001\0", "configured-key-0001", False),
        ("café-key-0001", "café-key-0001", True),
        ("café-key-0001", "cafe-key-0001", False),
    ],
)
def test_equal_padded_matches_exactly(presented, expected, equal) -> None:
    from app.core.rate_limiter import _equal_padded

    assert _equal_padded(presented, expected) is equal


# ---------------------------------------------------------------------------
# Tests against a permissive app.
#
# The app below answers 200 to any request that reaches it and never calls the
# auth dependency, so every request's reservation is kept. A request that the
# limiter refuses is therefore a 429 and one it lets through is a 200, whatever
# the key is. That makes each "still refused" case below fail if the limiter
# were to skip the reservation for that value, which the real auth app cannot
# show for values it would reject anyway.
# ---------------------------------------------------------------------------


def _permissive_limited(limit: int = 2) -> RateLimitMiddleware:
    async def ok(_request):
        return PlainTextResponse("ok")

    limiter = RateLimitMiddleware(
        Starlette(routes=[Route("/v1/wallets", ok)]), requests_per_minute=limit
    )
    limiter._redis_url = ""
    return limiter


async def _exhaust_permissive(http: AsyncClient, limiter: RateLimitMiddleware) -> None:
    for index in range(limiter.preauth_limit):
        response = await http.get(
            "/v1/wallets", headers={"X-API-Key": f"invented-key-{index:03d}"}
        )
        assert response.status_code == 200
    over = await http.get("/v1/wallets", headers={"X-API-Key": "invented-key-over"})
    assert over.status_code == 429


@pytest.mark.anyio
async def test_values_the_limiter_does_not_recognize_are_refused_on_an_exhausted_address(
    configured_keys,
) -> None:
    """Database-shaped, static dev and look-alike keys keep the reservation."""
    limiter = _permissive_limited()
    async with _client(limiter) as http:
        await _exhaust_permissive(http, limiter)
        refused = {
            name: (
                await http.get("/v1/wallets", headers={"X-API-Key": value})
            ).status_code
            for name, value in {
                "database-shaped": "amw_live_0123456789abcdef",
                "static-dev": "amw_dev_0123456789abcdef",
                "unknown": "unknown-key-00001",
            }.items()
        }
        allowed = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
    assert refused == {"database-shaped": 429, "static-dev": 429, "unknown": 429}
    assert allowed.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {CONFIGURED}"},
        {"Authorization": "Bearer x.y.z", "X-API-Key": CONFIGURED},
        {"Authorization": "", "X-API-Key": CONFIGURED},
        {"Authorization": "Basic Zm9vOmJhcg==", "X-API-Key": CONFIGURED},
        {"X-API-Key": f"Bearer {CONFIGURED}"},
        {"X-API-Key": "e" * 30 + "." + "f" * 30 + "." + "g" * 30},
    ],
    ids=[
        "bearer",
        "bearer-plus-key",
        "empty-authorization",
        "non-bearer-authorization",
        "bearer-in-key-header",
        "jwt-shaped",
    ],
)
async def test_bearers_authorization_headers_and_jwts_are_unchanged(
    configured_keys, headers
) -> None:
    limiter = _permissive_limited()
    async with _client(limiter) as http:
        await _exhaust_permissive(http, limiter)
        response = await http.get("/v1/wallets", headers=headers)
    assert response.status_code == 429


@pytest.mark.anyio
@pytest.mark.parametrize("configured", ["", " ", ",", " , ,"], ids=repr)
async def test_empty_valid_api_keys_skips_nothing(configured_keys, configured) -> None:
    """The default (no keys configured) must never act as match-all."""
    configured_keys(configured)
    limiter = _permissive_limited()
    async with _client(limiter) as http:
        await _exhaust_permissive(http, limiter)
        responses = [
            await http.get("/v1/wallets", headers=headers)
            for headers in (
                {},
                {"X-API-Key": ""},
                {"X-API-Key": "   "},
                {"X-API-Key": "any-value-0001"},
                {"X-API-Key": CONFIGURED},
            )
        ]
    assert [r.status_code for r in responses] == [429] * 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "skipped"),
    [
        ("abcdefg", False),
        ("abcdefgh", True),
        ("a" * 24 + "." + "b" * 12 + "." + "c" * 12, True),
        ("a" * 24 + "." + "b" * 12 + "." + "c" * 13, False),
    ],
    ids=["seven-chars", "eight-chars", "two-dots-fifty", "two-dots-fifty-one"],
)
async def test_length_and_jwt_shape_boundaries(configured_keys, key, skipped) -> None:
    """Auth rejects under 8 characters and routes more than 50 with two dots to JWT."""
    configured_keys(f"{CONFIGURED},{key}")
    limiter = _permissive_limited()
    async with _client(limiter) as http:
        await _exhaust_permissive(http, limiter)
        response = await http.get("/v1/wallets", headers={"X-API-Key": key})
    assert response.status_code == (200 if skipped else 429)


@pytest.mark.anyio
async def test_duplicate_key_headers_use_the_first_value_like_auth(
    configured_keys,
) -> None:
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        first_valid = await http.get(
            "/v1/wallets",
            headers=[("X-API-Key", CONFIGURED), ("X-API-Key", "unknown-key-00001")],
        )
        first_invalid = await http.get(
            "/v1/wallets",
            headers=[("X-API-Key", "unknown-key-00001"), ("X-API-Key", CONFIGURED)],
        )
    assert first_valid.status_code == 200
    assert first_invalid.status_code == 429


@pytest.mark.anyio
async def test_the_configured_key_header_name_is_honored(
    configured_keys, monkeypatch
) -> None:
    monkeypatch.setattr(rate_limiter_module.settings, "API_KEY_HEADER", "x-custom-key")
    limiter = _permissive_limited()
    async with _client(limiter) as http:
        for index in range(limiter.preauth_limit):
            await http.get(
                "/v1/wallets", headers={"X-Custom-Key": f"invented-key-{index:03d}"}
            )
        in_custom = await http.get("/v1/wallets", headers={"X-Custom-Key": CONFIGURED})
        in_default = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
    assert in_custom.status_code == 200
    assert in_default.status_code == 429


@pytest.mark.anyio
async def test_an_undecodable_configured_entry_does_not_break_requests(
    configured_keys,
) -> None:
    """A lone surrogate from the process environment must not raise a 500."""
    configured_keys(f"\udcffundecodable-entry,{CONFIGURED}")
    limiter = _permissive_limited()
    async with _client(limiter) as http:
        await _exhaust_permissive(http, limiter)
        unknown = await http.get(
            "/v1/wallets", headers={"X-API-Key": "unknown-key-0001"}
        )
        configured = await http.get("/v1/wallets", headers={"X-API-Key": CONFIGURED})
    assert unknown.status_code == 429
    assert configured.status_code == 200
