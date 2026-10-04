"""A reservation given back must be given back whole.

``PermitService.authorize_and_reserve`` takes two things in one guarded write:
the credits, and -- when the permit configures ``max_calls_per_tool`` for the
tool -- one use of that cap. Its documented compensation partner is
``release_tool_call``, and the reason both must move together is in that
docstring: an action that fails after reserving and returns only the credits
leaves the use consumed, so "a capped permit's legitimate retry is denied
``permit_max_calls_exceeded`` with no receipt behind the consumed use".

The insufficient-funds denial is the sharpest case, because that denial exists
to be acted on: the caller is told to fund the wallet and try again. On a permit
capped at one call, the retry it invites was refused for a call that never ran,
never charged and never dispatched.

The counter is asserted directly as well as behaviourally. A test that only
re-calls proves the retry works; reading ``tool_call_counts_json`` proves *why*,
and would catch a future change that made the retry pass for some other reason.
And the cap itself is pinned: a compensation that over-refunds would turn
``max_calls_per_tool`` into no limit at all, which is a worse defect than the
one being fixed here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.database import get_session_factory
from app.db.models import PermitModel, WalletModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.quotes import get_quote_service
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

TOOL_COST = 2.0


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def capped_tool():
    """A local tool that counts its own executions."""
    tool_name = "capped-echo"
    runs = {"count": 0}

    def echo(message: str = "ok") -> dict[str, Any]:
        runs["count"] += 1
        return {"message": message}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Capped echo",
        description="Permit reservation unwind test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


async def _create_capped_permit(
    client: AsyncClient,
    *,
    wallet_id: str,
    key_id: str | None,
    tool_name: str,
    max_calls: int = 1,
    idem_key: str,
) -> str:
    """Create a permit that caps this tool at ``max_calls`` calls."""
    resp = await client.post(
        "/v1/permits",
        json={
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": key_id,
            "allowed_tools": [tool_name],
            "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
            "max_credits": 50,
            "max_calls_per_tool": {tool_name: max_calls},
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        },
        headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": idem_key},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["permit_id"])


async def _calls_made(permit_id: str, tool_name: str) -> int:
    """Read the permit's per-tool counter straight from storage."""
    async with get_session_factory()() as session:
        permit = (
            await session.execute(
                select(PermitModel).where(PermitModel.permit_id == permit_id)
            )
        ).scalar_one()
        counts = json.loads(permit.tool_call_counts_json or "{}")
    return int(counts.get(tool_name, 0))


async def _set_balance(wallet_id: str, balance: str) -> None:
    async with get_session_factory()() as session:
        wallet = (
            await session.execute(
                select(WalletModel).where(WalletModel.wallet_id == wallet_id)
            )
        ).scalar_one()
        wallet.balance = Decimal(balance)
        await session.commit()


def _call_body(
    *,
    tool_name: str,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": f"call-{idempotency_key}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"message": "hello"},
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


@pytest.mark.anyio
async def test_insufficient_funds_gives_the_capped_permits_call_back(
    client: AsyncClient, clean_database: None, capped_tool
) -> None:
    """The denial that says "fund the wallet and retry" must leave a retry to make."""
    tool_name, runs = capped_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit_id = await _create_capped_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="capped-permit-funds",
    )

    await _set_balance(wallet_id, "1")
    denied = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="capped-funds-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["error"]["message"] == "insufficient_funds"
    assert runs["count"] == 0

    # The use was never spent, so it is not left consumed.
    assert await _calls_made(permit_id, tool_name) == 0

    await _set_balance(wallet_id, "1000")
    retry = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="capped-funds-2",
        ),
        headers=ctx["agent_headers"],
    )
    assert retry.status_code == 200, retry.text
    assert "error" not in retry.json(), retry.text
    assert runs["count"] == 1
    assert await _calls_made(permit_id, tool_name) == 1


@pytest.mark.anyio
async def test_a_lost_quote_gives_the_capped_permits_call_back(
    client: AsyncClient, clean_database: None, capped_tool, monkeypatch
) -> None:
    """Losing the quote race denies before anything runs, so nothing is spent."""
    tool_name, runs = capped_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit_id = await _create_capped_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="capped-permit-quote",
    )

    quote_service = get_quote_service()
    quote = await quote_service.create_quote(
        wallet_id=wallet_id,
        tool=tool_name,
        quoted_credits=Decimal(str(TOOL_COST)),
        category=ServiceCategory.AGENT_COMMS.value,
    )

    async def lose_quote_consume(
        quote_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> bool:
        del quote_id, idempotency_key
        return False

    monkeypatch.setattr(quote_service, "consume", lose_quote_consume)
    body = _call_body(
        tool_name=tool_name,
        wallet_id=wallet_id,
        permit_id=permit_id,
        idempotency_key="capped-quote-1",
    )
    body["params"]["mcpContext"]["quote_id"] = quote.quote_id
    denied = await client.post(
        "/mcp/messages",
        json=body,
        headers=ctx["agent_headers"],
    )
    monkeypatch.undo()

    assert denied.status_code == 200, denied.text
    assert denied.json()["error"]["message"] == "quote_already_consumed"
    assert runs["count"] == 0
    assert await _calls_made(permit_id, tool_name) == 0

    retry = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="capped-quote-2",
        ),
        headers=ctx["agent_headers"],
    )
    assert retry.status_code == 200, retry.text
    assert "error" not in retry.json(), retry.text
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_the_cap_still_denies_a_call_that_really_was_spent(
    client: AsyncClient, clean_database: None, capped_tool
) -> None:
    """The guard on the fix: compensation must not become a way past the cap.

    A release that ran on a path where the call really was consumed -- or one
    that decremented more than it should -- would turn ``max_calls_per_tool``
    into no limit at all. That is a worse defect than the one being fixed, so
    the limit is pinned here: one successful call against a cap of one, and the
    next call is refused.
    """
    tool_name, runs = capped_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit_id = await _create_capped_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="capped-permit-exhaust",
    )

    first = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="capped-exhaust-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert first.status_code == 200, first.text
    assert "error" not in first.json(), first.text
    assert await _calls_made(permit_id, tool_name) == 1

    second = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="capped-exhaust-2",
        ),
        headers=ctx["agent_headers"],
    )
    assert second.status_code == 200, second.text
    assert second.json()["error"]["message"] == "permit_max_calls_exceeded"
    # The tool ran exactly once: the refused call did not execute.
    assert runs["count"] == 1


@pytest.mark.anyio
async def test_a_freed_call_is_not_a_call_another_wallet_may_spend(
    client: AsyncClient, clean_database: None, capped_tool
) -> None:
    """Releasing a use widens nothing: the permit still binds to its subject.

    Negative path required for permit code by ``AGENTS.md``. A second wallet's
    key must not be able to reach the permit whose call was just handed back.
    """
    tool_name, runs = capped_tool
    ctx = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit_id = await _create_capped_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="capped-permit-cross",
    )

    await _set_balance(wallet_id, "1")
    denied = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key="capped-cross-1",
        ),
        headers=ctx["agent_headers"],
    )
    assert denied.json()["error"]["message"] == "insufficient_funds"
    assert await _calls_made(permit_id, tool_name) == 0

    # The freed use belongs to the permit's subject, not to whoever asks next.
    stolen = await client.post(
        "/mcp/messages",
        json=_call_body(
            tool_name=tool_name,
            wallet_id=other["agent_wallet_id"],
            permit_id=permit_id,
            idempotency_key="capped-cross-2",
        ),
        headers=other["agent_headers"],
    )
    assert stolen.status_code == 200, stolen.text
    assert "error" in stolen.json(), stolen.text
    assert stolen.json()["error"]["message"] != "insufficient_funds"
    assert runs["count"] == 0
