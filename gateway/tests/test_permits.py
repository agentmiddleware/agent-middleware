from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.time import utc_now
from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app
from app.schemas.trust import (
    ActionPermitFields,
    PermitCreateRequest,
    PermitRequestCreate,
)
from app.services.idempotency import get_idempotency_service
from app.services.permits import PermitError, get_permit_service
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_permit_cannot_name_foreign_subject_wallet(client, clean_database):
    """An issuer may only encumber its own wallet or one it funds.

    Regression: issuance authorized only the issuer, so a wallet holder could
    mint a signed permit naming an unrelated victim wallet as subject (and probe
    that wallet's balance via the creation error). Verified against production.
    """
    attacker = await provision_agent_wallet(client)
    victim = await provision_agent_wallet(client)

    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": attacker["agent_wallet_id"],
            "subject_wallet_id": victim["agent_wallet_id"],
            "allowed_tools": ["x"],
            "scopes": ["tool:x:invoke", "billing:charge"],
            "max_credits": 5,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**attacker["agent_headers"], "Idempotency-Key": "foreign-subject-1"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "subject_wallet_access_denied"


@pytest.mark.anyio
async def test_sponsor_may_issue_permit_for_its_agent(client, clean_database):
    """The legitimate delegation flow (issuer funds subject) still works."""
    provisioned = await provision_agent_wallet(client)
    sponsor_key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": provisioned["sponsor_wallet_id"], "key_name": "sponsor"},
        headers=BOOTSTRAP_HEADERS,
    )
    sponsor_headers = {"X-API-Key": sponsor_key.json()["api_key"]}

    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": provisioned["sponsor_wallet_id"],
            "subject_wallet_id": provisioned["agent_wallet_id"],
            "allowed_tools": ["x"],
            "scopes": ["tool:x:invoke", "billing:charge"],
            "max_credits": 5,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**sponsor_headers, "Idempotency-Key": "sponsor-for-agent-1"},
    )
    assert resp.status_code == 201


@pytest.mark.anyio
async def test_signed_permit_verifies_for_wallet_tool_and_budget(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="trust-echo",
    )

    verify_resp = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": provisioned["agent_wallet_id"],
            "tool": "trust-echo",
            "estimated_credits": 2,
        },
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True


@pytest.mark.anyio
async def test_reconcile_budgets_repairs_orphaned_reservation_on_terminal_permit(
    client, clean_database
):
    """A crashed reservation is reclaimed once the permit can no longer admit a
    charge (here: expired). Resetting a terminal permit can't cause over-spend
    because validate_for_action already rejects it."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="trust-echo",
    )

    factory = get_session_factory()
    # Reserved budget with no success receipt, permit now expired and idle.
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.spent_credits = Decimal("9")
        model.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        model.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.add(model)
        await session.commit()

    corrected = await get_permit_service().reconcile_budgets(idle_seconds=900)
    assert corrected == 1

    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        assert model.spent_credits == Decimal("0")


@pytest.mark.anyio
async def test_reconcile_budgets_never_resets_a_live_active_permit(
    client, clean_database
):
    """Safety property: an idle-but-still-chargeable (active, unexpired) permit
    is NEVER downward-reset, even past the idle window -- a long-running
    governed call looks identical to a crash, and resetting its live
    reservation would let a concurrent request over-spend past max_credits."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="trust-echo",
    )

    factory = get_session_factory()
    # Active, unexpired, but idle for over an hour (mimicking a slow call).
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.spent_credits = Decimal("9")
        model.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.add(model)
        await session.commit()

    corrected = await get_permit_service().reconcile_budgets(idle_seconds=900)
    assert corrected == 0

    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        # Reservation preserved -> no over-spend window.
        assert model.spent_credits == Decimal("9")


@pytest.mark.anyio
async def test_reconcile_budgets_leaves_recent_reservation_untouched(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="trust-echo",
    )

    factory = get_session_factory()
    # A reservation made just now (recent updated_at) is in-flight, not orphaned.
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.spent_credits = Decimal("9")
        model.updated_at = datetime.now(timezone.utc)
        session.add(model)
        await session.commit()

    corrected = await get_permit_service().reconcile_budgets(idle_seconds=900)
    assert corrected == 0

    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        assert model.spent_credits == Decimal("9")


@pytest.mark.anyio
async def test_verify_does_not_leak_permit_to_unauthorized_caller(
    client,
    clean_database,
):
    owner = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=owner["agent_wallet_id"],
        key_id=owner["key_id"],
        tool_name="trust-echo",
    )

    # A second, unrelated agent provisions its own wallet/key.
    other_sponsor = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Other",
            "email": "other-verify@example.com",
            "initial_credits": 5000,
            "require_kyc": False,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    other_agent = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": other_sponsor.json()["wallet_id"],
            "agent_id": "other-verify-agent",
            "budget_credits": 500,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    other_key = await client.post(
        "/v1/api-keys",
        json={"wallet_id": other_agent.json()["wallet_id"], "key_name": "rt"},
        headers=BOOTSTRAP_HEADERS,
    )
    other_headers = {"X-API-Key": other_key.json()["api_key"]}

    # Omitting wallet_id must NOT let an unrelated caller read the permit.
    resp = await client.post(
        "/v1/permits/verify",
        json={"permit_id": permit["permit_id"], "tool": "trust-echo"},
        headers=other_headers,
    )
    assert resp.status_code == 403
    assert (
        "permit" not in resp.text or resp.json().get("detail") == "permit_access_denied"
    )


@pytest.mark.anyio
async def test_permit_rejects_out_of_scope_tool(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="allowed-tool",
    )

    verify_resp = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": provisioned["agent_wallet_id"],
            "tool": "blocked-tool",
            "estimated_credits": 2,
        },
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is False
    assert verify_resp.json()["reason"] == "permit_tool_not_allowed"


@pytest.mark.anyio
async def test_permit_create_rejects_in_progress_idempotency_key(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    request_payload = {
        "issuer_wallet_id": provisioned["agent_wallet_id"],
        "subject_wallet_id": provisioned["agent_wallet_id"],
        "subject_key_id": provisioned["key_id"],
        "allowed_tools": ["in-progress-permit-tool"],
        "scopes": ["tool:in-progress-permit-tool:invoke", "billing:charge"],
        "max_credits": 50,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }
    await get_idempotency_service().begin(
        wallet_id=provisioned["agent_wallet_id"],
        endpoint="/v1/permits",
        idempotency_key="permit-in-progress-key",
        request_payload=PermitCreateRequest(**request_payload).model_dump(
            mode="json",
            exclude={"repeat_window_seconds", *ActionPermitFields.model_fields},
        ),
    )

    resp = await client.post(
        "/v1/permits",
        json=request_payload,
        headers={"X-API-Key": "test-key", "Idempotency-Key": "permit-in-progress-key"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "idempotency_in_progress"


@pytest.mark.anyio
async def test_permit_service_rejects_invalid_issuance_requests(
    client,
    clean_database,
):
    provisioned = await provision_agent_wallet(client)
    service = get_permit_service()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    base_request = {
        "issuer_wallet_id": provisioned["agent_wallet_id"],
        "subject_wallet_id": provisioned["agent_wallet_id"],
        "subject_key_id": provisioned["key_id"],
        "allowed_tools": ["service-issue-tool"],
        "scopes": ["tool:service-issue-tool:invoke", "billing:charge"],
        "max_credits": Decimal("10"),
        "expires_at": expires_at,
    }

    invalid_cases = [
        (
            {
                **base_request,
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
            },
            "permit_expired_at_creation",
        ),
        (
            {**base_request, "issuer_wallet_id": "missing-wallet"},
            "issuer_wallet_not_found",
        ),
        (
            {**base_request, "subject_wallet_id": "missing-wallet"},
            "subject_wallet_not_found",
        ),
        (
            {**base_request, "max_credits": Decimal("100000")},
            "permit_budget_exceeds_wallet_balance",
        ),
    ]

    for payload, reason in invalid_cases:
        with pytest.raises(PermitError) as exc_info:
            await service.create_permit(PermitCreateRequest(**payload))
        assert exc_info.value.reason == reason

    # A zero budget is refused by the schema now; the service keeps its own
    # guard for a request built without validation.
    zero_budget = {**base_request, "max_credits": Decimal("0")}
    with pytest.raises(ValidationError):
        PermitCreateRequest(**zero_budget)
    with pytest.raises(PermitError) as exc_info:
        await service.create_permit(PermitCreateRequest.model_construct(**zero_budget))
    assert exc_info.value.reason == "max_credits_must_be_positive"


@pytest.mark.anyio
async def test_permit_service_budget_lifecycle_and_filters(
    client,
    clean_database,
    enforce_naive_utc_datetime_columns,
):
    provisioned = await provision_agent_wallet(client)
    service = get_permit_service()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    permit = await service.create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=["service-budget-tool"],
            scopes=["tool:service-budget-tool:invoke"],
            max_credits=Decimal("10"),
            expires_at=expires_at.replace(tzinfo=None),
        )
    )

    assert "billing:charge" in permit.scopes
    fetched = await service.get_permit(permit.permit_id)
    assert fetched and fetched.permit_id == permit.permit_id

    permits, total = await service.list_permits(
        wallet_id=provisioned["agent_wallet_id"],
        status="active",
        subject_key_id=provisioned["key_id"],
        created_after=datetime.now(timezone.utc) - timedelta(minutes=5),
        created_before=datetime.now(timezone.utc) + timedelta(minutes=5),
        expires_after=datetime.now(timezone.utc),
        expires_before=expires_at + timedelta(minutes=5),
    )
    assert total == 1
    assert permits[0].permit_id == permit.permit_id

    await service.reserve_budget(permit.permit_id, Decimal("4"))
    validation = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="service-budget-tool",
        estimated_credits=Decimal("7"),
        key_id=provisioned["key_id"],
    )
    assert validation.allowed is False
    assert validation.reason == "permit_budget_exceeded"

    await service.release_budget(permit.permit_id, Decimal("2"))
    validation = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="service-budget-tool",
        estimated_credits=Decimal("7"),
        key_id=provisioned["key_id"],
    )
    assert validation.allowed is True

    with pytest.raises(PermitError) as missing_reserve:
        await service.reserve_budget("permit-missing", Decimal("1"))
    assert missing_reserve.value.reason == "permit_not_found"
    await service.release_budget("permit-missing", Decimal("1"))

    revoked = await service.revoke_permit(permit.permit_id)
    assert revoked.status == "revoked"
    assert revoked.revoked_at is not None
    validation = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="service-budget-tool",
        estimated_credits=Decimal("1"),
        key_id=provisioned["key_id"],
    )
    assert validation.allowed is False
    assert validation.reason == "permit_revoked"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("terminal_state", "expected_reason"),
    [
        ("revoked", "permit_revoked"),
        ("expired", "permit_expired"),
    ],
)
async def test_authorize_and_reserve_rechecks_terminal_state_after_stale_precheck(
    client,
    clean_database,
    terminal_state,
    expected_reason,
):
    """A stale precheck cannot authorize a later budget reservation.

    This models a revocation or expiry racing with an invocation after an
    earlier read-only permit check. The final locked operation must recheck the
    complete authorization state before it mutates ``spent_credits``.
    """
    provisioned = await provision_agent_wallet(client)
    service = get_permit_service()
    permit = await service.create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=["final-authorization-tool"],
            scopes=["tool:final-authorization-tool:invoke", "billing:charge"],
            max_credits=Decimal("10"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
    )

    stale_precheck = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="final-authorization-tool",
        estimated_credits=Decimal("2"),
        key_id=provisioned["key_id"],
    )
    assert stale_precheck.allowed is True

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        if terminal_state == "revoked":
            model.status = "revoked"
            model.revoked_at = utc_now()
        else:
            model.expires_at = utc_now() - timedelta(seconds=1)
        session.add(model)
        await session.commit()

    final_authorization = await service.authorize_and_reserve(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="final-authorization-tool",
        estimated_credits=Decimal("2"),
        key_id=provisioned["key_id"],
    )

    assert final_authorization.allowed is False
    assert final_authorization.reason == expected_reason
    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        assert model.spent_credits == Decimal("0")


@pytest.mark.anyio
async def test_concurrent_reservations_never_exceed_cap(client, clean_database):
    """Parallel reservations against one permit can never over-spend its cap.

    Regression for the SQLite over-spend: authorize_and_reserve guarded the
    check-and-reserve with ``SELECT ... FOR UPDATE``, which SQLAlchemy silently
    drops on SQLite. N concurrent reservations therefore all read the same
    ``spent_credits``, all passed the cap check, and their increments clobbered
    each other -- admitting far more than the cap allowed. The guarded
    conditional UPDATE enforces the cap atomically on every engine, so only
    ``floor(max_credits / cost)`` reservations may ever succeed and
    ``spent_credits`` never crosses ``max_credits``.
    """
    import asyncio

    provisioned = await provision_agent_wallet(client)
    service = get_permit_service()
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="trust-echo",
        max_credits=6,
    )

    cost = Decimal("2")
    concurrency = 12

    async def reserve_once():
        return await service.authorize_and_reserve(
            permit_id=permit["permit_id"],
            wallet_id=provisioned["agent_wallet_id"],
            tool_name="trust-echo",
            estimated_credits=cost,
            key_id=provisioned["key_id"],
        )

    results = await asyncio.gather(*(reserve_once() for _ in range(concurrency)))

    allowed = [r for r in results if r.allowed]
    denied = [r for r in results if not r.allowed]
    assert len(allowed) == 3  # floor(6 / 2)
    assert all(r.reason == "permit_budget_exceeded" for r in denied)

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        assert model is not None
        # Exactly the three admitted reservations, never past the cap.
        assert model.spent_credits == Decimal("6")
        assert model.spent_credits <= model.max_credits


@pytest.mark.anyio
async def test_permit_service_validation_denial_reasons(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    service = get_permit_service()
    permit = await service.create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=["service-validate-tool"],
            scopes=["tool:service-validate-tool:invoke", "billing:charge"],
            max_credits=Decimal("10"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
    )

    cases = [
        {
            "permit_id": "permit-missing",
            "wallet_id": provisioned["agent_wallet_id"],
            "tool_name": "service-validate-tool",
            "estimated_credits": Decimal("1"),
            "key_id": provisioned["key_id"],
            "reason": "permit_not_found",
        },
        {
            "permit_id": permit.permit_id,
            "wallet_id": other["agent_wallet_id"],
            "tool_name": "service-validate-tool",
            "estimated_credits": Decimal("1"),
            "key_id": provisioned["key_id"],
            "reason": "permit_wallet_mismatch",
        },
        {
            "permit_id": permit.permit_id,
            "wallet_id": provisioned["agent_wallet_id"],
            "tool_name": "service-validate-tool",
            "estimated_credits": Decimal("1"),
            "key_id": "wrong-key",
            "reason": "permit_key_mismatch",
        },
        {
            "permit_id": permit.permit_id,
            "wallet_id": provisioned["agent_wallet_id"],
            "tool_name": "other-tool",
            "estimated_credits": Decimal("1"),
            "key_id": provisioned["key_id"],
            "reason": "permit_tool_not_allowed",
        },
    ]

    for case in cases:
        reason = case.pop("reason")
        validation = await service.validate_for_action(**case)
        assert validation.allowed is False
        assert validation.reason == reason

    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        model.allowed_tools_json = json.dumps([])
        model.scopes_json = json.dumps(["billing:charge"])
        session.add(model)
        await session.commit()

    validation = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="service-validate-tool",
        estimated_credits=Decimal("1"),
        key_id=provisioned["key_id"],
    )
    assert validation.allowed is False
    assert validation.reason == "permit_scope_missing"

    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        model.scopes_json = json.dumps(
            ["tool:service-validate-tool:invoke", "billing:charge"]
        )
        model.signature = "tampered"
        session.add(model)
        await session.commit()

    validation = await service.validate_for_action(
        permit_id=permit.permit_id,
        wallet_id=provisioned["agent_wallet_id"],
        tool_name="service-validate-tool",
        estimated_credits=Decimal("1"),
        key_id=provisioned["key_id"],
    )
    assert validation.allowed is False
    assert validation.reason == "permit_signature_invalid"


async def test_reserve_budget_refuses_an_expired_permit(
    client,
    clean_database,
    enforce_naive_utc_datetime_columns,
):
    """reserve_budget is a second reservation path and must enforce expiry.

    An expired permit keeps ``status="active"`` in storage, so a guarded update
    without an expiry term happily spends against it. ``authorize_and_reserve``
    grew that term; this path is the one that did not have it.
    """
    provisioned = await provision_agent_wallet(client)
    service = get_permit_service()
    permit = await service.create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=["expiry-reserve-tool"],
            scopes=["tool:expiry-reserve-tool:invoke"],
            max_credits=Decimal("10"),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).replace(
                tzinfo=None
            ),
        )
    )

    # Cross expires_at without touching status, exactly as the clock would.
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            model = await session.get(PermitModel, permit.permit_id)
            assert model is not None
            model.expires_at = utc_now() - timedelta(seconds=1)
            session.add(model)

    with pytest.raises(PermitError) as excinfo:
        await service.reserve_budget(permit.permit_id, Decimal("1"))
    assert str(excinfo.value) == "permit_expired"

    # And no budget moved.
    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        assert model.spent_credits == Decimal("0")


def _scale_permit_body(wallet_id: str, key_id: str, **overrides) -> dict:
    body = {
        "issuer_wallet_id": wallet_id,
        "subject_wallet_id": wallet_id,
        "subject_key_id": key_id,
        "allowed_tools": ["scale-tool"],
        "scopes": ["tool:scale-tool:invoke", "billing:charge"],
        "max_credits": 5,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }
    body.update(overrides)
    return body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "max_credits",
    [0, -1, "-0.00000001", "1.123456789", "0.000000001", "1000000000000"],
)
async def test_permit_create_refuses_max_credits_outside_storage_scale(
    client, clean_database, max_credits
):
    """max_credits must be positive and fit permits.max_credits Numeric(20, 8).

    The permit is signed before it is persisted, so a value the column rounds
    (1.123456789 -> 1.12345679) minted a permit whose signature never
    verified, and 0.000000001 passed the service's ``> 0`` guard only to be
    stored as zero. Zero and negatives were a 400 from that guard; all of
    these are now a 422 at the boundary, before anything is signed or stored.
    """
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    resp = await client.post(
        "/v1/permits",
        json=_scale_permit_body(
            wallet_id, provisioned["key_id"], max_credits=max_credits
        ),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "scale-bad-1"},
    )
    assert resp.status_code == 422, resp.text
    _, total = await get_permit_service().list_permits(wallet_id=wallet_id)
    assert total == 0

    # The refused body never claimed the idempotency key: a corrected retry
    # under the same key mints normally.
    retry = await client.post(
        "/v1/permits",
        json=_scale_permit_body(wallet_id, provisioned["key_id"]),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "scale-bad-1"},
    )
    assert retry.status_code == 201, retry.text


@pytest.mark.anyio
async def test_permit_out_of_scale_body_from_unauthenticated_caller_is_401(
    client, clean_database
):
    """Bounds checking never runs ahead of authentication."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    resp = await client.post(
        "/v1/permits",
        json=_scale_permit_body(
            wallet_id, provisioned["key_id"], max_credits="1.123456789"
        ),
        headers={"Idempotency-Key": "scale-anon-1"},
    )
    assert resp.status_code == 401, resp.text
    _, total = await get_permit_service().list_permits(wallet_id=wallet_id)
    assert total == 0


@pytest.mark.anyio
async def test_permit_at_full_storage_scale_mints_and_verifies(client, clean_database):
    """Eight decimals and a 64-character nonce are the column limits, and
    still accepted: the stored permit verifies against its own signature."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    nonce = "n" * 64

    resp = await client.post(
        "/v1/permits",
        json=_scale_permit_body(
            wallet_id, provisioned["key_id"], max_credits="1.12345678", nonce=nonce
        ),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "scale-ok-1"},
    )
    assert resp.status_code == 201, resp.text
    permit = resp.json()
    assert Decimal(str(permit["max_credits"])) == Decimal("1.12345678")
    assert permit["nonce"] == nonce

    verify_resp = await client.post(
        "/v1/permits/verify",
        json={
            "permit_id": permit["permit_id"],
            "wallet_id": wallet_id,
            "tool": "scale-tool",
            "estimated_credits": "1",
        },
        headers=provisioned["agent_headers"],
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["valid"] is True, verify_resp.json()


@pytest.mark.parametrize("amount", ["0.00000001", "999999999999"])
def test_permit_credit_fields_accept_lossless_small_and_large_values(amount):
    """Boundary values must survive both Numeric(20, 8) and SQLite storage."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    permit = PermitCreateRequest(
        issuer_wallet_id="w",
        subject_wallet_id="w",
        max_credits=amount,
        aggregate_value_cap=amount,
        expires_at=expires_at,
    )
    assert permit.max_credits == Decimal(amount)
    assert permit.aggregate_value_cap == Decimal(amount)
    ask = PermitRequestCreate(
        issuer_wallet_id="w",
        subject_wallet_id="w",
        allowed_tools=["t"],
        max_credits=amount,
        expires_at=expires_at,
        justification="range",
    )
    assert ask.max_credits == Decimal(amount)


@pytest.mark.anyio
@pytest.mark.parametrize("nonce", ["x" * 65, "x" * 300], ids=["65-chars", "300-chars"])
async def test_permit_create_refuses_nonce_longer_than_column(
    client, clean_database, nonce
):
    """permits.nonce is String(64): a longer client nonce is a 422 here, not
    a truncation error (500) from Postgres at insert."""
    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]

    resp = await client.post(
        "/v1/permits",
        json=_scale_permit_body(wallet_id, provisioned["key_id"], nonce=nonce),
        headers={**provisioned["agent_headers"], "Idempotency-Key": "nonce-long-1"},
    )
    assert resp.status_code == 422, resp.text
    _, total = await get_permit_service().list_permits(wallet_id=wallet_id)
    assert total == 0
