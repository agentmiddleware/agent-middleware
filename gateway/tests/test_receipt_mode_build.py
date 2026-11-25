"""Receipts sign the enforcement mode and gateway build they were issued under."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import ReceiptModel
from app.main import app
from app.services import receipts as receipts_module
from app.services.receipts import ReceiptService, get_receipt_service
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

BUILD = "e70595326d57e9ac3f8e2eacff53536212f57c1b"
OTHER_BUILD = "e3bf41bc6ddbb245b8205b1329be6497a0462074"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _issue(
    client, monkeypatch, *, build: str | None, tool: str, mode: str | None = "enforce"
):
    monkeypatch.setattr(receipts_module, "get_build_commit_sha", lambda: build)
    monkeypatch.setattr(receipts_module, "receipt_enforcement_mode", lambda: mode)
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=tool,
        idem_key=f"permit-{tool}",
    )
    return await get_receipt_service().create_receipt(
        permit_id=permit["permit_id"],
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool=tool,
        request_payload={"v": 1},
        response_payload=None,
        ledger_entry_id=None,
        credits_authorized=Decimal("1"),
        credits_charged=Decimal("0"),
        outcome="denied",
        reason_code="permit_tool_not_allowed",
        audit_event_id=None,
    )


async def _mutate(receipt_id: str, **fields) -> None:
    async with get_session_factory()() as session:
        model = await session.get(ReceiptModel, receipt_id)
        for key, value in fields.items():
            setattr(model, key, value)
        session.add(model)
        await session.commit()


def _verify(receipt_id: str):
    return get_receipt_service().verify_receipt(receipt_id)


@pytest.mark.anyio
async def test_new_receipt_signs_mode_and_build(client, clean_database, monkeypatch):
    receipt = await _issue(client, monkeypatch, build=BUILD, tool="mode-build-echo")
    assert receipt.enforcement_mode == "enforce"
    assert receipt.gateway_build == BUILD
    ok, _, _ = await _verify(receipt.receipt_id)
    assert ok
    async with get_session_factory()() as session:
        model = await session.get(ReceiptModel, receipt.receipt_id)
        signed = json.loads(
            await ReceiptService().signing_input_for_model(model, session=session)
        )
    assert signed["enforcement_mode"] == "enforce"
    assert signed["gateway_build"] == BUILD


@pytest.mark.anyio
async def test_unknown_build_is_omitted_but_mode_signed(
    client, clean_database, monkeypatch
):
    receipt = await _issue(client, monkeypatch, build=None, tool="mode-nobuild-echo")
    assert receipt.gateway_build is None
    assert receipt.enforcement_mode == "enforce"
    assert (await _verify(receipt.receipt_id))[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fields",
    [
        {"enforcement_mode": "permissive"},
        {"gateway_build": OTHER_BUILD},
        {"enforcement_mode": None},
        {"gateway_build": None},
    ],
)
async def test_tampering_breaks_verification(
    client, clean_database, monkeypatch, fields
):
    receipt = await _issue(client, monkeypatch, build=BUILD, tool="mode-tamper-echo")
    await _mutate(receipt.receipt_id, **fields)
    assert not (await _verify(receipt.receipt_id))[0]


@pytest.mark.anyio
async def test_legacy_receipt_without_fields_still_verifies(
    client, clean_database, monkeypatch
):
    """A receipt signed before 052 has neither field and must keep verifying."""
    receipt = await _issue(
        client, monkeypatch, build=None, tool="mode-legacy-echo", mode=None
    )
    assert receipt.enforcement_mode is None and receipt.gateway_build is None
    assert (await _verify(receipt.receipt_id))[0]
    # Adding a field after the fact is tampering too.
    await _mutate(receipt.receipt_id, enforcement_mode="enforce")
    assert not (await _verify(receipt.receipt_id))[0]


def test_permissive_mode_reported(monkeypatch):
    class _S:
        TRUST_MODE_ENABLED = False
        ALLOW_LEGACY_UNPERMITTED_MCP = True

    monkeypatch.setattr(receipts_module, "get_settings", lambda: _S())
    assert receipts_module.receipt_enforcement_mode() == "permissive"


def test_enforce_mode_reported(monkeypatch):
    class _S:
        TRUST_MODE_ENABLED = True
        ALLOW_LEGACY_UNPERMITTED_MCP = False

    monkeypatch.setattr(receipts_module, "get_settings", lambda: _S())
    assert receipts_module.receipt_enforcement_mode() == "enforce"
