"""Permit requests: an agent asks, a human approves, the middleware mints.

Covers the contract of request -> notify -> approve -> mint -> poll:

- Simulated mode mints immediately in local/dev and is refused outright in a
  production-like environment (same fail-closed rule as the invoke gate).
- Real mode drives Sentinel: pending polls return 202 with no permit;
  approval mints exactly the requested terms; rejection and local expiry are
  terminal and mint nothing.
- The permit is minted from the STORED terms, so an approved request cannot be
  re-aimed by a later call.
- Minting happens exactly once: concurrent pollers, and a mint claim abandoned
  by a crashed worker, both converge on the single reserved permit.
- Authorization: only the subject wallet, the issuer wallet, or a bootstrap
  admin can read a request; an unrelated wallet cannot.
- The approver card renders the same terms on the hosted page, HTML-escaped.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.services.permit_requests as permit_requests_module
from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import PermitModel, PermitRequestModel
from app.main import app
from app.services.permit_requests import PermitRequestService
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

TOOLS = ["fetch-url", "summarize", "translate", "send-email"]
PERMIT_EXPIRES_AT = datetime(2035, 1, 1, tzinfo=timezone.utc).isoformat()
JUSTIFICATION = "Draft and send the weekly digest to the ops list."


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class FakeSentinel:
    """Stands in for SentinelClient; the decision is scripted per test."""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.created: list[dict] = []
        self.polls = 0
        self.fail_with: Exception | None = None
        self.approval_url = "https://sentinel.test/a/abc123"

    async def create_approval(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        self.created.append(kwargs)
        return {
            "action_id": f"act_{uuid.uuid4().hex[:16]}",
            "status": "pending",
            "approval_url": self.approval_url,
        }

    async def get_approval(self, action_id: str):
        if self.fail_with is not None:
            raise self.fail_with
        self.polls += 1
        payload = {"action_id": action_id, "status": self.status}
        if self.status in {"approved", "rejected"}:
            payload["decided_by"] = "issuer@example.com"
            payload["reason"] = f"scripted {self.status}"
        return payload


@pytest.fixture
def fresh_service(monkeypatch):
    """Give each test an isolated PermitRequestService instance."""
    service = PermitRequestService()
    monkeypatch.setattr(permit_requests_module, "_service", service)
    return service


@pytest.fixture
def sentinel(fresh_service, monkeypatch):
    fake = FakeSentinel()
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: fake)
    return fake


def _sentinel_env(monkeypatch, *, simulated: bool, configured: bool = True):
    settings = get_settings()
    monkeypatch.setattr(settings, "SIMULATION_MODE_HUMAN_APPROVAL", simulated)
    monkeypatch.setattr(
        settings, "SENTINEL_API_URL", "https://sentinel.test" if configured else ""
    )
    monkeypatch.setattr(
        settings, "SENTINEL_API_KEY", "sk_test_" + "0" * 64 if configured else ""
    )
    monkeypatch.setattr(settings, "SENTINEL_APPROVERS", "")
    monkeypatch.setattr(settings, "PERMIT_REQUEST_TIMEOUT_SECONDS", 3600)
    return settings


def _payload(agent, **overrides) -> dict:
    body = {
        "issuer_wallet_id": agent["sponsor_wallet_id"],
        "subject_wallet_id": agent["agent_wallet_id"],
        "allowed_tools": TOOLS,
        "max_credits": 20,
        # Fixed, not now-relative: retrying an idempotency key must resend
        # byte-identical terms, or it is a different ask (and a 409).
        "expires_at": PERMIT_EXPIRES_AT,
        "justification": JUSTIFICATION,
    }
    body.update(overrides)
    return body


async def _request(client, agent, *, idem: str = "preq-1", **overrides):
    return await client.post(
        "/v1/permit-requests",
        json=_payload(agent, **overrides),
        headers={**agent["agent_headers"], "Idempotency-Key": idem},
    )


async def _load(request_id: str) -> PermitRequestModel:
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        assert model is not None
        return model


@pytest.mark.asyncio
async def test_simulated_request_mints_immediately_in_dev(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=True)
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "development")
    agent = await provision_agent_wallet(client)

    resp = await _request(client, agent)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["simulated"] is True
    assert body["permit_id"]
    assert body["permit"]["allowed_tools"] == TOOLS
    assert Decimal(str(body["permit"]["max_credits"])) == Decimal("20")
    # Simulation never pages a real human.
    assert sentinel.created == []


@pytest.mark.asyncio
async def test_simulated_request_refused_in_production_like_environment(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=True)
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production")
    agent = await provision_agent_wallet(client)

    resp = await _request(client, agent)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "human_approval_not_configured"


@pytest.mark.asyncio
async def test_simulated_request_banked_in_dev_cannot_mint_in_production(
    client, clean_database, monkeypatch, sentinel
):
    """A dev-simulated request must not mint once the environment is real."""
    settings = _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        model.simulated = True
        session.add(model)
        await session.commit()

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "human_approval_not_configured"
    async with factory() as session:
        assert (await session.execute(select(PermitModel))).scalars().all() == []


@pytest.mark.asyncio
async def test_real_mode_without_sentinel_config_refuses(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False, configured=False)
    agent = await provision_agent_wallet(client)

    resp = await _request(client, agent)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "human_approval_not_configured"


@pytest.mark.asyncio
async def test_pending_then_approved_mints_the_requested_terms(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)

    created = await _request(client, agent)
    assert created.status_code == 202, created.text
    body = created.json()
    request_id = body["request_id"]
    assert body["status"] == "pending"
    assert body["permit"] is None
    assert body["poll_url"].endswith(f"/v1/permit-requests/{request_id}")
    # The human was paged once, with the terms they are being asked about.
    assert len(sentinel.created) == 1
    paged = sentinel.created[0]["arguments"]
    assert paged["allowed_tools"] == TOOLS
    assert paged["max_credits"] == "20"
    assert paged["justification"] == JUSTIFICATION

    poll = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert poll.status_code == 202
    assert poll.json()["status"] == "pending"
    assert poll.json()["permit"] is None

    sentinel.status = "approved"
    decided = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert decided.status_code == 200, decided.text
    settled = decided.json()
    assert settled["status"] == "approved"
    assert settled["decided_by"] == "issuer@example.com"
    permit = settled["permit"]
    assert permit["permit_id"] == settled["permit_id"]
    assert permit["allowed_tools"] == TOOLS
    assert permit["issuer_wallet_id"] == agent["sponsor_wallet_id"]
    assert permit["subject_wallet_id"] == agent["agent_wallet_id"]
    assert Decimal(str(permit["max_credits"])) == Decimal("20")
    assert permit["status"] == "active"
    # Scopes were derived from the requested tools, plus billing:charge.
    assert set(permit["scopes"]) == {f"tool:{tool}:invoke" for tool in TOOLS} | {
        "billing:charge"
    }

    # The minted permit is a real permit on the normal read path.
    fetched = await client.get(
        f"/v1/permits/{permit['permit_id']}", headers=agent["agent_headers"]
    )
    assert fetched.status_code == 200
    assert fetched.json()["permit_id"] == permit["permit_id"]


@pytest.mark.asyncio
async def test_repeated_polls_after_approval_mint_one_permit(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    sentinel.status = "approved"
    first = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    second = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert first.json()["permit_id"] == second.json()["permit_id"]

    factory = get_session_factory()
    async with factory() as session:
        permits = (await session.execute(select(PermitModel))).scalars().all()
    assert len(permits) == 1
    # A settled request stops polling Sentinel entirely.
    polls_after_decision = sentinel.polls
    await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert sentinel.polls == polls_after_decision


@pytest.mark.asyncio
async def test_rejection_is_terminal_and_mints_nothing(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    sentinel.status = "rejected"
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["permit_id"] is None
    assert body["permit"] is None

    # A later approval in Sentinel cannot revive it.
    sentinel.status = "approved"
    again = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert again.json()["status"] == "rejected"
    assert again.json()["permit"] is None

    factory = get_session_factory()
    async with factory() as session:
        assert (await session.execute(select(PermitModel))).scalars().all() == []


@pytest.mark.asyncio
async def test_decision_after_local_expiry_is_not_honored(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    # Sentinel keeps a timed-out approval "pending" forever; the window is
    # ours to enforce. Age the row past it, then let Sentinel say "approved".
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        model.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        session.add(model)
        await session.commit()

    sentinel.status = "approved"
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"
    assert resp.json()["permit"] is None
    assert resp.json()["reason"] == "approval_window_elapsed"
    # Expiry is checked before spending a poll on Sentinel.
    assert sentinel.polls == 0


@pytest.mark.asyncio
async def test_same_idempotency_key_pages_the_human_once(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)

    first = await _request(client, agent, idem="preq-dup")
    second = await _request(client, agent, idem="preq-dup")
    assert first.json()["request_id"] == second.json()["request_id"]
    assert len(sentinel.created) == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_terms_conflicts(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)

    await _request(client, agent, idem="preq-swap")
    swapped = await _request(client, agent, idem="preq-swap", max_credits=500)
    assert swapped.status_code == 409
    assert swapped.json()["detail"] == "permit_request_terms_conflict"
    # The human still only ever saw the original ask.
    assert len(sentinel.created) == 1


@pytest.mark.asyncio
async def test_approved_request_mints_stored_terms_not_polled_terms(
    client, clean_database, monkeypatch, sentinel
):
    """An approved request cannot be re-aimed after the human decided."""
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    # A second ask with wider scope is a separate, still-pending request.
    wider = await _request(
        client,
        agent,
        idem="preq-2",
        allowed_tools=[*TOOLS, "wire-transfer"],
        max_credits=500,
    )
    assert wider.json()["status"] == "pending"

    sentinel.status = "approved"
    settled = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    permit = settled.json()["permit"]
    assert permit["allowed_tools"] == TOOLS
    assert "wire-transfer" not in permit["allowed_tools"]
    assert Decimal(str(permit["max_credits"])) == Decimal("20")


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_window_elapsed", [False, True])
async def test_interrupted_mint_is_resumed_without_double_minting(
    client,
    clean_database,
    monkeypatch,
    sentinel,
    fresh_service,
    approval_window_elapsed,
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    # Simulate a worker that took the mint claim and died before minting.
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=600)
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        model.status = "minting"
        model.decided_by = "issuer@example.com"
        model.decided_at = stale
        model.mint_started_at = stale
        if approval_window_elapsed:
            # The claim was valid when acquired; recovering it after the
            # approval window closes must not discard already granted authority.
            model.expires_at = stale + timedelta(seconds=300)
        session.add(model)
        await session.commit()

    resumed = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "approved"
    minted = resumed.json()["permit_id"]
    assert minted

    # The reserved id is what got minted, so a re-run adopts rather than
    # issuing a second permit carrying the same authority.
    stored = await _load(request_id)
    assert minted == stored.reserved_permit_id
    async with factory() as session:
        permits = (await session.execute(select(PermitModel))).scalars().all()
    assert len(permits) == 1


@pytest.mark.asyncio
async def test_fresh_mint_claim_is_not_stolen_from_a_live_worker(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        model.status = "minting"
        model.mint_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(model)
        await session.commit()

    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "minting"
    async with factory() as session:
        assert (await session.execute(select(PermitModel))).scalars().all() == []


@pytest.mark.asyncio
async def test_mint_failure_is_terminal_with_a_reason(
    client, clean_database, monkeypatch, sentinel
):
    """A human approved terms the wallet can no longer support."""
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent, max_credits=900)).json()["request_id"]

    # Drain the subject wallet below the approved budget.
    factory = get_session_factory()
    async with factory() as session:
        from app.db.models import WalletModel

        wallet = await session.get(WalletModel, agent["agent_wallet_id"])
        wallet.balance = Decimal("1")
        session.add(wallet)
        await session.commit()

    sentinel.status = "approved"
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["reason"] == "permit_budget_exceeds_wallet_balance"
    assert body["permit"] is None


@pytest.mark.asyncio
async def test_sentinel_outage_on_create_is_retryable(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)

    sentinel.fail_with = httpx.ConnectError("sentinel down")
    down = await _request(client, agent, idem="preq-retry")
    assert down.status_code == 503
    assert down.json()["detail"] == "human_approval_unavailable"
    # Nothing was banked, so the same key works once Sentinel recovers.
    factory = get_session_factory()
    async with factory() as session:
        assert (await session.execute(select(PermitRequestModel))).scalars().all() == []

    sentinel.fail_with = None
    recovered = await _request(client, agent, idem="preq-retry")
    assert recovered.status_code == 202


@pytest.mark.asyncio
async def test_sentinel_outage_on_poll_does_not_settle_the_request(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    sentinel.fail_with = httpx.ConnectError("sentinel down")
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 503
    assert (await _load(request_id)).status == "pending"


@pytest.mark.asyncio
async def test_issuer_may_request_only_from_a_funding_wallet(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)

    resp = await _request(
        client, agent, idem="preq-foreign", issuer_wallet_id=stranger["agent_wallet_id"]
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "issuer_wallet_access_denied"


@pytest.mark.asyncio
async def test_unrelated_wallet_cannot_read_a_request(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    denied = await client.get(
        f"/v1/permit-requests/{request_id}", headers=stranger["agent_headers"]
    )
    assert denied.status_code == 403

    allowed = await client.get(
        f"/v1/permit-requests/{request_id}", headers=BOOTSTRAP_HEADERS
    )
    assert allowed.status_code == 202


@pytest.mark.asyncio
async def test_approval_card_page_shows_the_reviewed_terms(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (
        await _request(
            client,
            agent,
            justification="Send <b>weekly</b> digest & invoices",
        )
    ).json()["request_id"]

    card = await client.get(
        f"/v1/permit-requests/{request_id}/card", headers=agent["agent_headers"]
    )
    assert card.status_code == 200
    assert card.headers["content-type"].startswith("text/html")
    assert card.headers["cache-control"] == "no-store"
    html = card.text
    # Scope, budget, and justification are the decision.
    assert "20" in html
    for tool in TOOLS:
        assert tool in html
    assert agent["agent_wallet_id"] in html
    assert sentinel.approval_url in html
    # Approver-supplied text is escaped, never rendered as markup.
    assert "&lt;b&gt;weekly&lt;/b&gt;" in html
    assert "<b>weekly</b>" not in html

    stranger = await provision_agent_wallet(client)
    denied = await client.get(
        f"/v1/permit-requests/{request_id}/card", headers=stranger["agent_headers"]
    )
    assert denied.status_code == 403


class _RecordingNotifications:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_email(self, **kwargs) -> None:
        self.sent.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approval_url",
    [
        "javascript:alert(document.domain)",
        " JaVaScRiPt:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "http://sentinel.test/a/abc123",
    ],
)
async def test_non_https_sentinel_approval_url_is_not_stored_or_linked(
    client, clean_database, monkeypatch, sentinel, approval_url
):
    import app.services.notifications as notifications_module

    settings = _sentinel_env(monkeypatch, simulated=False)
    monkeypatch.setattr(settings, "SENTINEL_APPROVERS", "approver@example.com")
    mailer = _RecordingNotifications()
    monkeypatch.setattr(
        notifications_module, "get_notification_service", lambda: mailer
    )
    sentinel.approval_url = approval_url
    agent = await provision_agent_wallet(client)
    created = await _request(client, agent)
    assert created.status_code == 202
    request_id = created.json()["request_id"]

    # The Sentinel response is not trusted to choose the link scheme: the
    # row keeps no URL, so neither surface can turn it into a clickable link.
    assert (await _load(request_id)).approval_url is None

    assert len(mailer.sent) == 1
    email = mailer.sent[0]
    for rendered in (email["html"], email["body"]):
        assert approval_url.strip() not in rendered
        assert "<a " not in rendered
    assert "Approve or reject from the Sentinel" in email["html"]

    card = await client.get(
        f"/v1/permit-requests/{request_id}/card", headers=agent["agent_headers"]
    )
    assert card.status_code == 200
    assert approval_url.strip() not in card.text
    assert "<a " not in card.text

    stranger = await provision_agent_wallet(client)
    denied = await client.get(
        f"/v1/permit-requests/{request_id}/card", headers=stranger["agent_headers"]
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_request_hash_binds_every_reviewed_term(
    client, clean_database, monkeypatch, sentinel
):
    """Changing any reviewed term changes the hash the approval binds to."""
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    await _request(client, agent, idem="preq-hash")
    base = (
        await _load(
            (await _request(client, agent, idem="preq-hash")).json()["request_id"]
        )
    ).request_hash

    variants = {
        "tools": {"allowed_tools": [*TOOLS, "wire-transfer"]},
        "credits": {"max_credits": 21},
        "justification": {"justification": "something else entirely"},
        "gate": {"requires_human_approval": True},
    }
    for name, override in variants.items():
        resp = await _request(client, agent, idem=f"preq-hash-{name}", **override)
        assert resp.status_code == 202, resp.text
        model = await _load(resp.json()["request_id"])
        assert model.request_hash != base, name


@pytest.mark.asyncio
async def test_requested_gate_flag_rides_onto_the_minted_permit(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent, requires_human_approval=True)).json()[
        "request_id"
    ]

    sentinel.status = "approved"
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.json()["permit"]["requires_human_approval"] is True


@pytest.mark.asyncio
async def test_stored_scopes_survive_as_json_on_the_row(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    model = await _load(request_id)
    assert json.loads(model.allowed_tools_json) == TOOLS
    assert "billing:charge" in json.loads(model.scopes_json)
    assert model.reserved_permit_id.startswith("permit-")
    assert model.permit_id is None


@pytest.mark.asyncio
async def test_wallet_can_list_its_own_requests_without_advancing_them(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    stranger = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]
    await _request(client, stranger, idem="preq-stranger")

    sentinel.status = "approved"
    listed = await client.get("/v1/me/permit-requests", headers=agent["agent_headers"])
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["requests"][0]["request_id"] == request_id
    # Listing is a read: it must not poll Sentinel or mint the permit.
    assert body["requests"][0]["status"] == "pending"
    assert sentinel.polls == 0
    assert (await _load(request_id)).status == "pending"

    filtered = await client.get(
        "/v1/me/permit-requests?status=rejected", headers=agent["agent_headers"]
    )
    assert filtered.json()["total"] == 0

    assert (
        await client.get("/v1/me/permit-requests", headers=BOOTSTRAP_HEADERS)
    ).status_code == 403


@pytest.mark.asyncio
async def test_tampered_request_terms_cannot_mint_after_approval(
    client, clean_database, monkeypatch, sentinel
):
    """An attacker who modifies the stored terms after approval cannot escalate authority.

    The minted permit must carry exactly the terms the human reviewed. If the stored
    terms are tampered with between approval and mint, the integrity check must fail
    and no permit may be issued.
    """
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    # Simulate an attacker who gains DB access and tries to escalate authority
    # by modifying the stored terms after the human approved.
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        # Attacker tries to widen the tool scope and increase the budget.
        model.allowed_tools_json = json.dumps([*TOOLS, "wire-transfer", "root-shell"])
        model.max_credits = Decimal("9999")
        # Attacker leaves the request_hash intact (hoping it won't be checked).
        session.add(model)
        await session.commit()

    sentinel.status = "approved"
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 200
    body = resp.json()
    # The tampered request must fail to mint.
    assert body["status"] == "failed"
    assert body["reason"] == "permit_request_terms_integrity_violation"
    assert body["permit"] is None
    assert body["permit_id"] is None

    # No permit was minted — the attacker gained nothing.
    async with factory() as session:
        assert (await session.execute(select(PermitModel))).scalars().all() == []


@pytest.mark.asyncio
async def test_agent_cannot_escalate_via_poll_body_injection(
    client, clean_database, monkeypatch, sentinel
):
    """An agent cannot escalate by injecting wider terms into the poll request.

    The poll endpoint takes no body — only the request_id path parameter. Even
    if an attacker tried to inject terms via headers or query params, minting
    reads from the stored row, not from the poll request.
    """
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    sentinel.status = "approved"
    # Attacker tries to inject escalated terms via the poll (pointless, but verify).
    escalated_headers = {
        **agent["agent_headers"],
        "X-Escalated-Tools": json.dumps([*TOOLS, "wire-transfer"]),
        "X-Escalated-Credits": "9999",
    }
    resp = await client.get(
        f"/v1/permit-requests/{request_id}",
        headers=escalated_headers,
    )
    assert resp.status_code == 200
    permit = resp.json()["permit"]
    # The minted permit carries exactly the reviewed terms.
    assert permit["allowed_tools"] == TOOLS
    assert Decimal(str(permit["max_credits"])) == Decimal("20")
    assert "wire-transfer" not in permit["allowed_tools"]


@pytest.mark.asyncio
async def test_coherent_tampering_of_terms_and_hash_fails_anchor_check(
    client, clean_database, monkeypatch, sentinel
):
    """An attacker who tampers with both terms AND request_hash cannot mint.

    Even if an attacker recomputes request_hash to match the tampered terms,
    the original_request_hash anchor detects the coherent tampering and refuses
    to mint.
    """
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    request_id = (await _request(client, agent)).json()["request_id"]

    # Attacker escalates the terms AND updates request_hash to match.
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitRequestModel, request_id)
        escalated_tools = [*TOOLS, "wire-transfer", "root-shell"]
        escalated_credits = Decimal("9999")
        model.allowed_tools_json = json.dumps(escalated_tools)
        model.max_credits = escalated_credits
        # Attacker recomputes the hash to match the new terms.
        from app.services.permit_requests import permit_request_hash

        model.request_hash = permit_request_hash(
            issuer_wallet_id=model.issuer_wallet_id,
            subject_wallet_id=model.subject_wallet_id,
            allowed_tools=escalated_tools,
            scopes=json.loads(model.scopes_json),
            max_credits=escalated_credits,
            permit_expires_at=model.permit_expires_at,
            requires_human_approval=model.requires_human_approval,
            justification=model.justification,
        )
        # Attacker leaves original_request_hash intact — it's the anchor.
        session.add(model)
        await session.commit()

    sentinel.status = "approved"
    resp = await client.get(
        f"/v1/permit-requests/{request_id}", headers=agent["agent_headers"]
    )
    assert resp.status_code == 200
    body = resp.json()
    # The anchor check must catch the coherent tampering.
    assert body["status"] == "failed"
    assert body["reason"] == "permit_request_hash_anchor_violation"
    assert body["permit"] is None
    assert body["permit_id"] is None

    # No permit was minted.
    async with factory() as session:
        from sqlalchemy import select
        from app.db.models import PermitModel

        assert (await session.execute(select(PermitModel))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_credits",
    ["1.123456789", "0.000000001", "1000000000000", "100000000000.12345678"],
)
async def test_request_max_credits_outside_storage_scale_is_refused(
    client, clean_database, monkeypatch, sentinel, max_credits
):
    """permit_requests.max_credits is Numeric(20, 8), like the permit it mints.

    A 9-decimal ask was hashed for the human at full precision, then stored
    rounded, so the approved request failed its own integrity check at mint;
    0.000000001 passed the ``> 0`` guard and was stored as zero. Both are a
    422 now, before a row is written or a human is paged.
    """
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)

    resp = await _request(client, agent, idem="preq-scale", max_credits=max_credits)
    assert resp.status_code == 422, resp.text
    assert sentinel.created == []
    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(select(PermitRequestModel))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_request_at_full_storage_scale_mints_a_verifiable_permit(
    client, clean_database, monkeypatch, sentinel
):
    _sentinel_env(monkeypatch, simulated=True)
    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "development")
    agent = await provision_agent_wallet(client)

    resp = await _request(client, agent, idem="preq-scale-ok", max_credits="1.12345678")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved", body
    permit = body["permit"]
    assert Decimal(str(permit["max_credits"])) == Decimal("1.12345678")

    verify = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": agent["agent_wallet_id"],
            "tool": TOOLS[0],
            "estimated_credits": "1",
        },
        headers=agent["agent_headers"],
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True, verify.json()


@pytest.mark.anyio
async def test_malformed_sentinel_response_never_logs_the_approval_url(
    client, clean_database, monkeypatch, sentinel, caplog
):
    """A malformed create response is logged by shape, never by value.

    The response may still carry approval_url, a magic-link credential, so
    the error log names only the payload's keys.
    """
    _sentinel_env(monkeypatch, simulated=False)
    token = "SENTINEL-MAGIC-LINK-TOKEN-7f3a"

    async def malformed_create(**kwargs):
        return {"status": "pending", "approval_url": f"https://sentinel.test/a/{token}"}

    monkeypatch.setattr(sentinel, "create_approval", malformed_create)
    agent = await provision_agent_wallet(client)
    with caplog.at_level(logging.ERROR, logger="app.services.permit_requests"):
        created = await _request(client, agent)
    assert created.status_code == 503, created.text
    assert "sentinel_permit_request_malformed" in caplog.text
    assert "approval_url" in caplog.text
    assert token not in caplog.text
    assert "sentinel.test" not in caplog.text


class IdempotentSentinel(FakeSentinel):
    """Reuse a provider action on the documented Idempotency-Key boundary."""

    def __init__(self):
        super().__init__()
        self.actions = {}
        self.decisions = {}
        self.lose_response_once = False

    async def create_approval(self, **kwargs):
        key = kwargs["idempotency_key"]
        if key not in self.actions:
            self.actions[key] = await super().create_approval(**kwargs)
            self.decisions[self.actions[key]["action_id"]] = "pending"
        if self.lose_response_once:
            self.lose_response_once = False
            raise httpx.ReadTimeout("synthetic lost approval-create response")
        return self.actions[key]

    async def get_approval(self, action_id):
        return {"action_id": action_id, "status": self.decisions[action_id]}


@pytest.mark.asyncio
async def test_new_client_key_needs_distinct_human_approval(
    client, clean_database, monkeypatch, fresh_service
):
    _sentinel_env(monkeypatch, simulated=False)
    provider = IdempotentSentinel()
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: provider)
    agent = await provision_agent_wallet(client)
    first = await _request(client, agent, idem="first-ask")
    assert first.status_code == 202
    first_model = await _load(first.json()["request_id"])
    provider.decisions[first_model.sentinel_action_id] = "approved"
    approved = await client.get(
        f"/v1/permit-requests/{first_model.request_id}", headers=agent["agent_headers"]
    )
    assert approved.json()["status"] == "approved"

    second = await _request(client, agent, idem="second-ask")
    assert second.status_code == 202
    second_model = await _load(second.json()["request_id"])
    assert second_model.sentinel_action_id != first_model.sentinel_action_id
    assert len(provider.actions) == 2
    still_pending = await client.get(
        f"/v1/permit-requests/{second_model.request_id}", headers=agent["agent_headers"]
    )
    assert still_pending.json()["status"] == "pending"
    async with get_session_factory()() as session:
        assert len((await session.execute(select(PermitModel))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_remote_created_but_response_lost_reuses_approval_on_retry(
    client, clean_database, monkeypatch, fresh_service
):
    _sentinel_env(monkeypatch, simulated=False)
    provider = IdempotentSentinel()
    provider.lose_response_once = True
    monkeypatch.setattr(fresh_service, "_sentinel", lambda: provider)
    agent = await provision_agent_wallet(client)
    first = await _request(client, agent, idem="lost-create")
    assert first.status_code == 503
    async with get_session_factory()() as session:
        assert not (await session.execute(select(PermitRequestModel))).scalars().all()
    retry = await _request(client, agent, idem="lost-create")
    assert retry.status_code == 202
    assert len(provider.actions) == 1
    model = await _load(retry.json()["request_id"])
    assert (
        model.sentinel_action_id == next(iter(provider.actions.values()))["action_id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected", "pending"])
async def test_provider_poll_crossing_deadline_expires_without_minting(
    client, clean_database, monkeypatch, sentinel, decision
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    response = await _request(client, agent, idem="deadline-poll")
    model = await _load(response.json()["request_id"])

    async def late_decision(action_id):
        monkeypatch.setattr(
            permit_requests_module,
            "utc_now",
            lambda: model.expires_at + timedelta(seconds=1),
        )
        return {"action_id": action_id, "status": decision}

    monkeypatch.setattr(sentinel, "get_approval", late_decision)
    result = await client.get(
        f"/v1/permit-requests/{model.request_id}", headers=agent["agent_headers"]
    )
    assert result.json()["status"] == "expired"
    assert result.json()["reason"] == "approval_window_elapsed"
    assert result.json()["permit_id"] is None
    async with get_session_factory()() as session:
        assert not (await session.execute(select(PermitModel))).scalars().all()


@pytest.mark.asyncio
async def test_mint_claim_checks_persisted_deadline_with_stale_pending_model(
    client, clean_database, monkeypatch, sentinel, fresh_service
):
    _sentinel_env(monkeypatch, simulated=False)
    agent = await provision_agent_wallet(client)
    response = await _request(client, agent, idem="deadline-claim")
    stale = await _load(response.json()["request_id"])
    async with get_session_factory()() as session:
        model = await session.get(PermitRequestModel, stale.request_id)
        model.expires_at = permit_requests_module.utc_now() - timedelta(seconds=1)
        await session.commit()
    result = await fresh_service._decide(stale, "approved")
    assert result.status == "expired"
    async with get_session_factory()() as session:
        assert not (await session.execute(select(PermitModel))).scalars().all()
