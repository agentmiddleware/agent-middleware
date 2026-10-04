"""Exercise real urllib redirect processors with in-memory transports only."""

from email.message import Message
from io import BytesIO
import urllib.request
from urllib.response import addinfourl

import pytest

from scripts import adversarial_battery as battery


@pytest.fixture
def transport(monkeypatch):
    calls = []
    responses = []

    def response(handler, request):
        calls.append(
            (request.full_url, request.get_method(), request.get_header("X-api-key"))
        )
        status, body, location = responses[0] if len(calls) == 1 else (200, b"{}", None)
        headers = Message()
        if location:
            headers["Location"] = location
        result = addinfourl(BytesIO(body), headers, request.full_url, status)
        result.msg = "synthetic"
        return result

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", response)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", response)
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(battery, "API_URL", "https://selected.invalid")
    return calls, responses


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    ["http://untrusted.invalid/collect", "https://untrusted.invalid/collect", "/moved"],
)
def test_credentialed_requests_never_follow_redirects(
    transport, method, status, location
):
    calls, responses = transport
    responses.append((status, b'{"detail":"redirect refused"}', location))
    actual, body = battery.req(
        method,
        "/v1/audit/summary",
        key="synthetic-canary",
        body={"synthetic": True} if method == "POST" else None,
    )
    assert actual == status
    assert body == {"detail": "redirect refused"}
    assert calls == [
        ("https://selected.invalid/v1/audit/summary", method, "synthetic-canary")
    ]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"ok":true}', {"ok": True}),
        (403, b'{"detail":"denied"}', {"detail": "denied"}),
        (500, b"unavailable", "unavailable"),
    ],
)
def test_ordinary_response_status_and_body_are_preserved(
    transport, status, body, expected
):
    calls, responses = transport
    responses.append((status, body, None))
    assert battery.req("GET", "/synthetic", key="synthetic-canary") == (
        status,
        expected,
    )
    assert len(calls) == 1
