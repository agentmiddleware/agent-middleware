"""Sentinel origin and credential safety at every dispatch boundary."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.sentinel_target import (
    SentinelTargetError,
    normalize_sentinel_origin,
    sentinel_api_key_is_valid,
    sentinel_health_url,
)
from app.services.human_approval import (
    HumanApprovalService,
    HumanApprovalUnavailableError,
    SentinelClient,
    human_approval_configured,
)
from app.services.permit_requests import PermitRequestService


@pytest.mark.parametrize(
    ("value", "allow_loopback", "expected"),
    [
        ("https://SENTINEL.example:443/", False, "https://sentinel.example"),
        ("https://8.8.8.8:8443", False, "https://8.8.8.8:8443"),
        ("http://127.0.0.1:8000/", True, "http://127.0.0.1:8000"),
        ("http://[::1]:8000", True, "http://[::1]:8000"),
    ],
)
def test_normalize_sentinel_origin_accepts_only_explicit_safe_shapes(
    value,
    allow_loopback,
    expected,
):
    assert normalize_sentinel_origin(value, allow_loopback=allow_loopback) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@sentinel.example",
        "https://sentinel.example?",
        "https://sentinel.example#",
        "https://sentinel.example/?",
        "https://sentinel.example/#",
        "https://sentinel.example/api",
        "https://sentinel.example:",
        "https://sentinel.example:0",
        "https://sentinel.example:65536",
        "http://sentinel.example",
        "ftp://sentinel.example",
        "https://169.254.169.254",
        "https://10.0.0.1",
        "https://224.0.0.1",
        "https://239.255.255.250",
        "https://[::]",
        "https://[::ffff:127.0.0.1]",
        "https://[::ffff:8.8.8.8]",
        "https://[::127.0.0.1]",
        "https://[::169.254.169.254]",
        "https://[64:ff9b::10.0.0.1]",
        "https://[fec0::1]",
        "https://[ff02::1]",
        "https://[ff0e::1]",
        "https://2130706433",
        "https://0x7f000001",
        "https://0177.0.0.1",
        "https://127.1",
        "https://redis",
        "https://metadata.google.internal",
        "https://sentinel.internal",
        "https://sentinel.local",
        "https://[v1.sentinel.example]",
        "https://[fe80::1%25en0]",
        "https://faß.de",
        " https://sentinel.example",
        "https://sentinel.example/\n",
    ],
)
def test_normalize_sentinel_origin_rejects_unsafe_remote_targets(value):
    with pytest.raises(SentinelTargetError):
        normalize_sentinel_origin(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8000",
        "https://localhost",
        "https://service.localhost",
        "https://127.0.0.1:8443",
    ],
)
def test_loopback_requires_explicit_local_opt_in(value):
    with pytest.raises(SentinelTargetError):
        normalize_sentinel_origin(value)
    assert normalize_sentinel_origin(value, allow_loopback=True)


def test_health_url_uses_the_same_normalized_origin():
    assert (
        sentinel_health_url("https://SENTINEL.example:443/")
        == "https://sentinel.example/health"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk_live_safe", True),
        ("", False),
        ("   ", False),
        (" sk_live_safe", False),
        ("sk_live_safe\n", False),
        ("sk_live safe", False),
        ("sk_live_safé", False),
    ],
)
def test_sentinel_api_key_shape_is_fail_closed(value, expected):
    assert sentinel_api_key_is_valid(value) is expected


@pytest.mark.parametrize(
    "service_factory",
    [HumanApprovalService, PermitRequestService],
    ids=["invoke_approval", "permit_request"],
)
def test_unsafe_production_origin_denies_before_http_client_creation(
    monkeypatch,
    service_factory,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "https://169.254.169.254")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "sk_live_never_send")

    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: pytest.fail(
            "unsafe configuration must fail before an HTTP client exists"
        ),
    )

    assert human_approval_configured() is False
    with pytest.raises(HumanApprovalUnavailableError):
        service_factory()._sentinel()


def test_sentinel_client_binds_key_only_to_normalized_origin(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    sentinel = SentinelClient(
        "https://SENTINEL.example:443/",
        "sk_live_safe",
    )
    assert sentinel.client is not None
    assert captured["base_url"] == "https://sentinel.example"
    assert captured["headers"] == {"Authorization": "Bearer sk_live_safe"}
    assert captured["follow_redirects"] is False


@pytest.mark.anyio
async def test_sentinel_client_dispatches_one_exact_credentialed_request(monkeypatch):
    requests = []
    import httpx

    real_client = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"action_id": "action-safe"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    sentinel = SentinelClient("https://SENTINEL.example:443/", "sk_live_safe")
    try:
        result = await sentinel.create_approval(
            function_name="test.operation",
            arguments={"value": "redacted"},
            risk_level="high",
            approvers=[],
            timeout_seconds=60,
            idempotency_key="idempotency-safe",
        )
    finally:
        await sentinel.close()

    assert result == {"action_id": "action-safe"}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://sentinel.example/v1/approvals"
    assert request.headers["authorization"] == "Bearer sk_live_safe"
    assert "sk_live_safe" not in request.content.decode()


@pytest.mark.anyio
async def test_sentinel_client_never_follows_a_credentialed_redirect(monkeypatch):
    requests = []
    import httpx

    real_client = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.example/collect"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )

    sentinel = SentinelClient("https://api.pauseapi.app", "sk_live_safe")
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await sentinel.get_approval("action-safe")
    finally:
        await sentinel.close()

    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.pauseapi.app/v1/approvals/action-safe"


@pytest.mark.parametrize(
    "service_factory",
    [HumanApprovalService, PermitRequestService],
    ids=["invoke_approval", "permit_request"],
)
def test_invalid_key_denies_before_http_client_creation(
    monkeypatch,
    service_factory,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "https://api.pauseapi.app")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", " sk_live_never_send")

    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid credentials must fail before an HTTP client exists"
        ),
    )

    assert human_approval_configured() is False
    with pytest.raises(HumanApprovalUnavailableError):
        service_factory()._sentinel()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "key", "expected"),
    [
        ("", "", {"status": "not_configured"}),
        (
            "https://api.pauseapi.app",
            "",
            {"status": "down", "reason": "human_approval_unavailable"},
        ),
        (
            "",
            "sk_live_orphaned",
            {"status": "down", "reason": "human_approval_unavailable"},
        ),
        (
            "https://api.pauseapi.app",
            " sk_live_bad",
            {"status": "down", "reason": "human_approval_unavailable"},
        ),
        (
            "https://127.0.0.1",
            "sk_live_loopback",
            {"status": "down", "reason": "human_approval_unavailable"},
        ),
    ],
    ids=["absent", "missing_key", "missing_url", "invalid_key", "loopback"],
)
async def test_sentinel_health_distinguishes_absent_and_partial_configuration(
    monkeypatch,
    url,
    key,
    expected,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SENTINEL_API_URL", url)
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", key)

    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: pytest.fail(
            "absent or partial configuration must not create a client"
        ),
    )

    from app.core.health import _check_sentinel

    assert await _check_sentinel({"human_approval": False}) == expected


def test_production_loopback_is_not_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "https://127.0.0.1")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "sk_live_safe")

    assert human_approval_configured() is False


def test_local_loopback_can_be_configured_explicitly_by_environment(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "SENTINEL_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(settings, "SENTINEL_API_KEY", "sk_test_safe")

    assert human_approval_configured() is True
