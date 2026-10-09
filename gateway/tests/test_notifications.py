"""Focused tests for app.services.notifications.NotificationService.

The service sends alerts via Slack webhooks and the Resend email API.
All HTTP is faked with httpx.MockTransport, so no network is used.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.services import notifications
from app.services.notifications import (
    NotificationService,
    _failure_status,
    get_notification_service,
)

WEBHOOK = "https://hooks.slack.com/services/T/B/sentinel"
API_KEY = "re_test_key"


def _make_service(handler, slack_url=WEBHOOK, api_key=API_KEY):
    service = NotificationService()
    service._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service._slack_webhook_url = slack_url
    service._resend_api_key = api_key
    service._from_email = "alerts@example.com"
    return service


def test_failure_status_returns_code_for_http_error():
    request = httpx.Request("POST", WEBHOOK)
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
    assert _failure_status(exc) == 429


def test_failure_status_returns_none_for_transport_error():
    request = httpx.Request("POST", WEBHOOK)
    assert _failure_status(httpx.ConnectError("down", request=request)) is None


async def test_wallet_frozen_alert_sends_slack_and_email():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    service = _make_service(handler)
    try:
        await service.send_wallet_frozen_alert(
            wallet_id="w-1",
            reason="anomalous_spend",
            sponsor_email="sponsor@example.com",
            wallet_owner="Ada",
        )
    finally:
        await service.close()

    assert len(seen) == 2
    slack_body = seen[0].read().decode()
    assert "Anomalous spend velocity detected" in slack_body
    assert ":rotating_light:" in slack_body
    assert "w-1" in slack_body

    import json

    email_body = json.loads(seen[1].read().decode())
    assert email_body["to"] == "sponsor@example.com"
    assert "w-1" in email_body["subject"] and "Frozen" in email_body["subject"]
    assert "Anomalous spend velocity detected" in email_body["text"]
    assert "Ada" in email_body["text"]
    assert seen[1].headers["authorization"] == f"Bearer {API_KEY}"


async def test_wallet_frozen_alert_falls_back_for_unknown_reason():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    service = _make_service(handler, api_key="")
    try:
        await service.send_wallet_frozen_alert(wallet_id="w-2", reason="weird_cause")
    finally:
        await service.close()

    assert len(seen) == 1
    assert "Wallet frozen: weird_cause" in seen[0].read().decode()


async def test_alerts_are_silent_noop_when_nothing_is_configured():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    service = _make_service(handler, slack_url="", api_key="")
    try:
        await service.send_wallet_frozen_alert(
            wallet_id="w-3", reason="fraud_detected", sponsor_email="s@example.com"
        )
        await service.send_low_balance_warning(
            wallet_id="w-3",
            current_balance=Decimal("10"),
            threshold=Decimal("100"),
            sponsor_email="s@example.com",
        )
        await service.send_payment_failed_alert(
            wallet_id="w-3", error_message="declined", payment_intent_id="pi_1"
        )
    finally:
        await service.close()

    assert calls == []


async def test_low_balance_warning_mentions_balance_and_threshold():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    service = _make_service(handler)
    try:
        await service.send_low_balance_warning(
            wallet_id="w-4",
            current_balance=Decimal("25.5"),
            threshold=Decimal("100"),
            sponsor_email="sponsor@example.com",
        )
    finally:
        await service.close()

    assert len(seen) == 2
    slack_body = seen[0].read().decode()
    assert ":warning:" in slack_body
    assert "25.5" in slack_body and "100" in slack_body

    import json

    email_body = json.loads(seen[1].read().decode())
    assert "25.5" in email_body["text"] and "100" in email_body["text"]


async def test_payment_failed_alert_carries_payment_intent_and_skips_email():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    service = _make_service(handler)
    try:
        await service.send_payment_failed_alert(
            wallet_id="w-5", error_message="card declined", payment_intent_id="pi_9"
        )
    finally:
        await service.close()

    assert len(seen) == 1
    assert str(seen[0].url) == WEBHOOK
    body = seen[0].read().decode()
    assert "card declined" in body
    assert "pi_9" in body


async def test_kyc_alerts_cover_approved_rejected_required():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    service = _make_service(handler)
    try:
        await service.send_kyc_approved_alert(
            wallet_id="w-6", sponsor_email="sponsor@example.com"
        )
        await service.send_kyc_rejected_alert(
            wallet_id="w-6", reason="blurry photo", sponsor_email="sponsor@example.com"
        )
        await service.send_kyc_required_alert(wallet_id="w-6")
    finally:
        await service.close()

    assert len(seen) == 5
    combined = b" ".join(r.read() for r in seen).decode()
    assert "Identity Verified" in combined
    assert "blurry photo" in combined
    assert "Verification Required" in combined
    assert ":information_source:" in combined


async def test_public_send_email_includes_html_alternative():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "e1"})

    service = _make_service(handler, slack_url="")
    try:
        await service.send_email(
            to="sponsor@example.com",
            subject="hello",
            body="plain",
            html="<p>plain</p>",
        )
    finally:
        await service.close()

    import json

    assert len(seen) == 1
    payload = json.loads(seen[0].read().decode())
    assert payload["text"] == "plain"
    assert payload["html"] == "<p>plain</p>"
    assert payload["from"] == "alerts@example.com"


async def test_security_alert_maps_emergency_to_critical_urgency():
    urgencies: list[str] = []

    async def fake_slack(title, message, **fields):
        urgencies.append(fields.get("urgency"))

    service = NotificationService()
    service._slack_webhook_url = WEBHOOK
    monkey = pytest.MonkeyPatch()
    monkey.setattr(service, "_send_slack_alert", fake_slack)
    try:
        await service.send_security_alert("w-7", "emergency_key_revocation", "revoked")
        await service.send_security_alert("w-7", "suspicious_login", "odd login")
    finally:
        monkey.undo()
        await service.close()

    assert urgencies == ["critical", "high"]


async def test_failed_sends_are_swallowed_not_raised(caplog):
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        if "slack" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(401, text="nope")

    service = _make_service(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="app.services.notifications"):
            await service.send_wallet_frozen_alert(
                wallet_id="w-8",
                reason="manual_freeze",
                sponsor_email="sponsor@example.com",
            )
            await service.send_low_balance_warning(
                wallet_id="w-8",
                current_balance=Decimal("1"),
                threshold=Decimal("10"),
                sponsor_email="sponsor@example.com",
            )
    finally:
        await service.close()

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) >= 2


async def test_get_notification_service_returns_singleton(monkeypatch):
    monkeypatch.setattr(notifications, "_notification_service", None)
    first = get_notification_service()
    try:
        assert get_notification_service() is first
    finally:
        monkeypatch.setattr(notifications, "_notification_service", None)
        await first.close()
