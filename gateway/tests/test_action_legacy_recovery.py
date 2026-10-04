import json
from decimal import Decimal
import pytest
from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.services.mcp_dispatch_attempts import get_mcp_dispatch_attempt_service
from app.services.permits import get_permit_service
from app.services.signing_keys import get_signing_key_service
from app.services.mcp_dispatch_attempts import DispatchPrepareCommitUncertainError
from tests.test_governed_persistence import client as _client, _seed_governed_identity

client = _client


@pytest.mark.anyio
async def test_signed_legacy_cap_recovery(client, clean_database, monkeypatch):
    p, permit, _, begun = await _seed_governed_identity(
        client, suffix="review-legacy-cap"
    )
    tool = "partner-tool-review-legacy-cap"
    service = get_mcp_dispatch_attempt_service()
    kwargs = dict(
        idempotency_record_id=begun.record_id,
        wallet_id=p["agent_wallet_id"],
        permit_id=permit["permit_id"],
        key_id=p["key_id"],
        public_tool_id=tool,
        upstream_tool_name="remote_tool",
        upstream_origin="https://partner.example",
        request_hash=begun.request_hash,
        credits_authorized=Decimal("1.5"),
    )
    # Synthetic pre-atomic-worker persisted row: valid signed cap, reserved
    # money, but no atomic call-slot marker or counter.
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        model.max_calls_per_tool_json = json.dumps({tool: 1})
        model.spent_credits = Decimal("1.5")
        signature, kid, _ = await get_signing_key_service().sign_payload(
            get_permit_service()._unsigned_payload(model)
        )
        model.signature = signature
        model.key_id = kid
        await session.commit()
    prepared = await service.prepare(**kwargs)
    assert prepared.call_slot_reserved is False
    original = service._get_by_idempotency_record
    count = 0

    async def fail_first(session, record_id):
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("synthetic commit-recovery trigger")
        return await original(session, record_id)

    monkeypatch.setattr(service, "_get_by_idempotency_record", fail_first)
    with pytest.raises(
        DispatchPrepareCommitUncertainError,
        match="dispatch_prepare_constraint_unsupported",
    ):
        await service.authorize_reserve_and_prepare(**kwargs)
    async with get_session_factory()() as session:
        model = await session.get(PermitModel, permit["permit_id"])
    assert not json.loads(model.tool_call_counts_json or "{}")
