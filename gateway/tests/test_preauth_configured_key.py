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

from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
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
async def test_key_that_is_not_in_the_env_list_is_unchanged(configured_keys) -> None:
    """A database or static dev key is not recognized pre-auth: still charged."""
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        database_shaped = await http.get(
            "/v1/wallets", headers={"X-API-Key": "amw_live_0123456789abcdef"}
        )
        static_dev = await http.get(
            "/v1/wallets", headers={"X-API-Key": "amw_dev_0123456789abcdef"}
        )
    assert database_shaped.status_code == 429
    assert static_dev.status_code == 429


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {CONFIGURED}"},
        {"Authorization": "Bearer x.y.z", "X-API-Key": CONFIGURED},
        {"X-API-Key": f"Bearer {CONFIGURED}"},
        {"X-API-Key": "e" * 30 + "." + "f" * 30 + "." + "g" * 30},
    ],
    ids=["bearer", "bearer-plus-key", "bearer-in-key-header", "jwt-shaped"],
)
async def test_bearers_and_jwts_are_unchanged(configured_keys, headers) -> None:
    limiter = _limited()
    async with _client(limiter) as http:
        await _exhaust_address(http, limiter)
        response = await http.get("/v1/wallets", headers=headers)
    assert response.status_code == 429


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
