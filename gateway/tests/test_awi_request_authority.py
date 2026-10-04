from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, LedgerEntryModel
from app.main import app
from app.services.awi_session import get_awi_session_manager
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize(
    "constraint",
    [
        "changed_arguments",
        "changed_dry_run",
        "changed_representation",
        "changed_permit",
        "forbidden_fields",
        "max_calls_per_tool",
        "dry_run_rejected",
    ],
)
async def test_awi_request_authority_is_bound(constraint, clean_database):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        actor = await provision_agent_wallet(client)
        create = await client.post(
            "/v1/awi/sessions",
            json={
                "target_url": "https://example.test",
                "wallet_id": actor["agent_wallet_id"],
            },
            headers=actor["agent_headers"],
        )
        assert create.status_code == 201
        permit_body = {
            "issuer_wallet_id": actor["agent_wallet_id"],
            "subject_wallet_id": actor["agent_wallet_id"],
            "subject_key_id": actor["key_id"],
            "allowed_tools": ["awi_execute"],
            "scopes": ["tool:awi_execute:invoke", "billing:charge"],
            "max_credits": 50,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        }
        if constraint == "forbidden_fields":
            permit_body["forbidden_fields"] = ["url"]
        if constraint == "max_calls_per_tool":
            permit_body["max_calls_per_tool"] = {"awi_execute": 1}
        permit_response = await client.post(
            "/v1/permits",
            json=permit_body,
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": "permit-synthetic"},
        )
        assert permit_response.status_code == 201, permit_response.text
        headers = {
            **actor["agent_headers"],
            "X-Permit-Id": permit_response.json()["permit_id"],
            "Idempotency-Key": "synthetic-first",
        }
        body = {
            "session_id": create.json()["session_id"],
            "action": "navigate_to",
            "parameters": {"url": "https://example.test/first"},
        }
        if constraint == "dry_run_rejected":
            body["dry_run"] = True
        first = await client.post("/v1/awi/execute", json=body, headers=headers)
        if constraint in ("forbidden_fields", "max_calls_per_tool", "dry_run_rejected"):
            session = await get_awi_session_manager().get_session(
                create.json()["session_id"]
            )
            assert session is not None
            assert session.step_count == 0
            async with get_session_factory()() as db:
                debits = await db.scalar(
                    select(func.count())
                    .select_from(LedgerEntryModel)
                    .where(
                        LedgerEntryModel.wallet_id == actor["agent_wallet_id"],
                        LedgerEntryModel.amount < 0,
                    )
                )
            assert debits == (1 if constraint == "dry_run_rejected" else 0)
        if constraint == "dry_run_rejected":
            assert first.status_code == 200
            assert first.json()["error"] == "dry_run_unsupported"
            assert first.json()["effect_status"] == "not_dispatched"
            assert first.json()["receipt"]["outcome"] == "failed_refunded"
            return
        if constraint == "max_calls_per_tool":
            assert first.status_code == 403
            assert first.json()["detail"]["error"] == "awi_call_limit_unsupported"
            return
        if constraint == "forbidden_fields":
            assert first.status_code == 403, (
                f"Forbidden url accepted HTTP{first.status_code}; receipt outcome={first.json().get('receipt', {}).get('outcome')}"
            )
            return
        assert first.status_code == 200, first.text
        replay = await client.post("/v1/awi/execute", json=body, headers=headers)
        assert replay.status_code == 200
        assert (
            replay.json()["receipt"]["receipt_id"]
            == first.json()["receipt"]["receipt_id"]
        )
        receipt = await client.get(
            "/v1/receipts/" + first.json()["receipt"]["receipt_id"],
            headers=actor["agent_headers"],
        )
        assert receipt.status_code == 200
        async with get_session_factory()() as db:
            record = (
                await db.execute(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.wallet_id == actor["agent_wallet_id"],
                        IdempotencyRecordModel.endpoint == "POST /v1/awi/execute",
                    )
                )
            ).scalar_one()
            assert record.request_hash == receipt.json()["request_hash"]
        if constraint == "changed_arguments":
            body["parameters"]["url"] = "https://example.test/different"
        elif constraint == "changed_dry_run":
            body["dry_run"] = True
        elif constraint == "changed_representation":
            body["representation_request"] = "summary"
        else:
            replacement = await client.post(
                "/v1/permits",
                json=permit_body,
                headers={
                    **BOOTSTRAP_HEADERS,
                    "Idempotency-Key": "permit-synthetic-replacement",
                },
            )
            assert replacement.status_code == 201
            headers["X-Permit-Id"] = replacement.json()["permit_id"]
        second = await client.post("/v1/awi/execute", json=body, headers=headers)
        assert second.status_code == 409


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", [{"include_raw_state": True}, {"similarity_threshold": 0.9}]
)
async def test_rag_replay_binds_response_controls(clean_database, change):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        actor = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=actor["agent_wallet_id"],
            key_id=actor["key_id"],
            tool_name="awi_rag_query",
            max_credits=50,
            idem_key="synthetic-rag-permit",
        )
        headers = {
            **actor["agent_headers"],
            "X-Permit-Id": permit["permit_id"],
            "X-Wallet-Id": actor["agent_wallet_id"],
            "Idempotency-Key": "synthetic-rag-query",
        }
        first = await client.post(
            "/v1/awi/rag/query", json={"query": "synthetic"}, headers=headers
        )
        assert first.status_code == 200, first.text
        changed = await client.post(
            "/v1/awi/rag/query", json={"query": "synthetic", **change}, headers=headers
        )
        assert changed.status_code == 409
