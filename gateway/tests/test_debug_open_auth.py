"""DEBUG open mode must not hand bootstrap admin to remote peers.

With DEBUG=true and no keys configured, an unknown API key authenticates as
bootstrap admin so a local checkout works with zero setup. That fallback is
bound to loopback HTTP peers: a demo bound to a shared interface must refuse
remote callers instead of promoting them. Direct in-process callers (no
request) keep the previous local behavior.
"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.auth import (
    _is_loopback_client,
    debug_open_auth_armed,
    get_auth_context,
)
from app.core.config import get_settings

UNKNOWN_KEY = "some-unknown-caller-key"


@pytest.fixture
def open_mode_settings():
    """Arm DEBUG open mode: DEBUG on, no keys, local environment."""
    settings = get_settings()
    saved = {
        name: getattr(settings, name)
        for name in ("ENVIRONMENT", "DEBUG", "VALID_API_KEYS", "STATIC_DEV_API_KEYS")
    }
    settings.ENVIRONMENT = "local"
    settings.DEBUG = True
    settings.VALID_API_KEYS = ""
    settings.STATIC_DEV_API_KEYS = ""
    try:
        yield settings
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


def _request_from(host: str | None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/permits",
        "headers": [],
        "client": (host, 51234) if host is not None else None,
    }
    return Request(scope)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.5", "::1", "localhost", "testclient"],
)
async def test_debug_open_mode_allows_loopback_peers(open_mode_settings, host):
    context = await get_auth_context(api_key=UNKNOWN_KEY, request=_request_from(host))
    assert context.is_bootstrap_admin is True


async def test_debug_open_mode_refuses_remote_peer(open_mode_settings):
    with pytest.raises(HTTPException) as exc_info:
        await get_auth_context(api_key=UNKNOWN_KEY, request=_request_from("203.0.0.5"))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "invalid_api_key"


async def test_debug_open_mode_refuses_private_network_peer(open_mode_settings):
    with pytest.raises(HTTPException) as exc_info:
        await get_auth_context(api_key=UNKNOWN_KEY, request=_request_from("10.0.0.9"))
    assert exc_info.value.status_code == 403


async def test_debug_open_mode_direct_call_keeps_local_behavior(open_mode_settings):
    context = await get_auth_context(api_key=UNKNOWN_KEY)
    assert context.is_bootstrap_admin is True


async def test_configured_keys_close_open_mode_even_on_loopback(open_mode_settings):
    open_mode_settings.VALID_API_KEYS = "the-configured-bootstrap-key"
    with pytest.raises(HTTPException) as exc_info:
        await get_auth_context(api_key=UNKNOWN_KEY, request=_request_from("127.0.0.1"))
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (None, True),
        ("127.0.0.1", True),
        ("127.0.0.5", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("testclient", True),
        ("203.0.0.5", False),
        ("10.0.0.9", False),
        ("192.168.1.20", False),
        ("not-a-host!!", False),
    ],
)
def test_is_loopback_client(host, expected):
    assert _is_loopback_client(host) is expected


def test_debug_open_auth_armed_matrix(open_mode_settings):
    assert debug_open_auth_armed(open_mode_settings) is True
    open_mode_settings.DEBUG = False
    assert debug_open_auth_armed(open_mode_settings) is False
    open_mode_settings.DEBUG = True
    open_mode_settings.VALID_API_KEYS = "a-configured-key"
    assert debug_open_auth_armed(open_mode_settings) is False
    open_mode_settings.VALID_API_KEYS = ""
    open_mode_settings.STATIC_DEV_API_KEYS = "amw_dev_local-only"
    assert debug_open_auth_armed(open_mode_settings) is False
    open_mode_settings.STATIC_DEV_API_KEYS = ""
    open_mode_settings.ENVIRONMENT = "production"
    assert debug_open_auth_armed(open_mode_settings) is False
