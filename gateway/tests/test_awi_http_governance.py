"""AWI HTTP high-risk routes must require permit → meter → receipt."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, LedgerEntryModel, PermitModel
from app.main import app
from app.services import awi_rag_engine as awi_rag_engine_module
from app.services.awi_rag_engine import AWIRAGEngine
from app.services.idempotency import MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

RAG_QUERY_ENDPOINT = "POST /v1/awi/rag/query"
RAG_QUERY_PAYLOAD = {"query": "laptops", "top_k": 3}
HEADER_SOURCE = "Idempotency-Key header"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_rag_query_denied_without_permit(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    resp = await client.post(
        "/v1/awi/rag/query",
        json={"query": "laptops", "top_k": 3},
        headers={
            **provisioned["agent_headers"],
            "X-Wallet-Id": provisioned["agent_wallet_id"],
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "permit_required"


@pytest.mark.anyio
async def test_rag_query_succeeds_with_permit_and_receipt(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-http-rag",
    )
    resp = await client.post(
        "/v1/awi/rag/query",
        json={"query": "laptops", "top_k": 3},
        headers={
            **provisioned["agent_headers"],
            "X-Wallet-Id": provisioned["agent_wallet_id"],
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "awi-http-rag-1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "laptops"
    assert "results" in body
    assert body["receipt"]["permit_id"] == permit["permit_id"]
    assert body["receipt"]["outcome"] == "success"
    assert body["receipt"]["signature"]

    receipt_resp = await client.get(
        f"/v1/receipts/{body['receipt']['receipt_id']}",
        headers=provisioned["agent_headers"],
    )
    assert receipt_resp.status_code == 200, receipt_resp.text
    receipt = receipt_resp.json()
    assert Decimal(str(receipt["credits_authorized"])) == Decimal("3")
    assert Decimal(str(receipt["credits_charged"])) == Decimal("3")
    assert receipt["ledger_entry_id"]


@pytest.mark.anyio
async def test_rag_query_insufficient_funds_aborts_and_replays(client, clean_database):
    """402 closes the idempotency key; same key replays 402 (not 409 in-progress)."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-broke",
    )

    # Leave the agent with 2 credits (rag costs 3) after permit creation.
    transfer = await client.post(
        "/v1/billing/transfer",
        params={
            "from_wallet_id": provisioned["agent_wallet_id"],
            "to_wallet_id": provisioned["sponsor_wallet_id"],
            "amount": 998,
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert transfer.status_code == 200, transfer.text

    headers = {
        **provisioned["agent_headers"],
        "X-Wallet-Id": provisioned["agent_wallet_id"],
        "X-Permit-Id": permit["permit_id"],
        "Idempotency-Key": "awi-http-broke-1",
    }
    payload = {"query": "laptops", "top_k": 3}

    first = await client.post("/v1/awi/rag/query", json=payload, headers=headers)
    assert first.status_code == 402, first.text
    assert first.json()["detail"]["error"] == "insufficient_funds"

    second = await client.post("/v1/awi/rag/query", json=payload, headers=headers)
    assert second.status_code == 402, second.text
    assert second.json()["detail"]["error"] == "insufficient_funds"


@pytest.mark.anyio
async def test_execute_denied_without_wallet_scoped_session(client, clean_database):
    """Sessions without wallet_id cannot enter the governed execute path."""
    create = await client.post(
        "/v1/awi/sessions",
        json={"target_url": "https://example.com"},
        headers={"X-API-Key": "test-key"},
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]

    resp = await client.post(
        "/v1/awi/execute",
        json={
            "session_id": session_id,
            "action": "navigate_to",
            "parameters": {"url": "https://example.com/next"},
        },
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "wallet_required"


@pytest.mark.anyio
async def test_awi_route_keys_its_debit_to_the_idempotency_record(
    client, clean_database
):
    """The governed AWI route must key its debit to the request's identity.

    This path used to call ``money.charge()`` with no ``operation_key``, so it
    had neither the ``uq_ledger_wallet_operation_key`` constraint nor the
    adopt-the-existing-debit recovery the governed MCP path relies on. A charge
    that committed but whose acknowledgement was lost took the failure branch,
    which releases the permit budget and *completes* the idempotency record as
    ``charge_failed`` -- so the caller retried under a fresh key and paid twice
    for one logical action.

    Asserted on the real route rather than on ``money.charge`` directly,
    because the defect was the wiring: the mechanism already existed and this
    caller simply did not use it.
    """
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-opkey",
    )
    resp = await client.post(
        "/v1/awi/rag/query",
        json={"query": "laptops", "top_k": 3},
        headers={
            **provisioned["agent_headers"],
            "X-Wallet-Id": provisioned["agent_wallet_id"],
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "awi-opkey-1",
        },
    )
    assert resp.status_code == 200, resp.text

    factory = get_session_factory()
    async with factory() as session:
        record_id = (
            await session.execute(
                select(IdempotencyRecordModel.record_id).where(
                    IdempotencyRecordModel.wallet_id == provisioned["agent_wallet_id"],
                    IdempotencyRecordModel.idempotency_key == "awi-opkey-1",
                )
            )
        ).scalar_one()
        keyed = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.operation_key == record_id)
            )
        ).scalar_one()
        unkeyed = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(
                    LedgerEntryModel.wallet_id == provisioned["agent_wallet_id"],
                    # Debits are written as action="debit" with a negative
                    # amount (LedgerEntryModel: "Positive = credit, negative =
                    # debit"). Select by sign so this counts real debits.
                    LedgerEntryModel.amount < 0,
                    LedgerEntryModel.operation_key.is_(None),
                )
            )
        ).scalar_one()

    assert keyed == 1, (
        "the AWI debit is not keyed to its idempotency record, so a retry "
        "after a lost acknowledgement would debit again"
    )
    assert unkeyed == 0, "a governed AWI charge landed with no operation_key"


async def _index_tenant_memory(
    client: AsyncClient, tenant: dict[str, Any], *, product: str, idem: str
) -> str:
    """Create a session for ``tenant`` and index one memory through the route."""
    session = await client.post(
        "/v1/awi/sessions",
        json={
            "target_url": "https://example.com",
            "wallet_id": tenant["agent_wallet_id"],
        },
        headers=tenant["agent_headers"],
    )
    assert session.status_code == 201, session.text
    permit = await create_tool_permit(
        client,
        wallet_id=tenant["agent_wallet_id"],
        key_id=tenant["key_id"],
        tool_name="awi_memory_index",
        max_credits=50,
        idem_key=f"permit-{idem}",
    )
    resp = await client.post(
        "/v1/awi/rag/index",
        json={
            "session_id": session.json()["session_id"],
            "session_type": "shopping",
            "action_history": [
                {"action": "add_to_cart", "parameters": {"product": product}}
            ],
        },
        headers={
            **tenant["agent_headers"],
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": idem,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["memory_id"]


@pytest.mark.proof
@pytest.mark.anyio
async def test_rag_query_scopes_to_caller_before_top_k(
    client, clean_database, monkeypatch
):
    """Another tenant's better matches must not crowd the caller out of
    ``top_k``, nor have their access counters bumped by the caller's query."""
    engine = AWIRAGEngine(embedding_model="mock-embedding")
    monkeypatch.setattr(awi_rag_engine_module, "_rag_engine", engine)
    caller = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    own_memory = await _index_tenant_memory(
        client, caller, product="caller-widget", idem="awi-topk-own"
    )
    other_memory = await _index_tenant_memory(
        client, other, product="other-tenant-widget", idem="awi-topk-other"
    )
    stored = engine._memories[other_memory]
    # Identical to the other tenant's embedding text: it scores 1.0 and so
    # outranks the caller's own memory in an unscoped search.
    query = engine._prepare_embedding_text(
        stored.session_type,
        stored.action_sequence,
        stored.page_summaries,
        stored.key_entities,
        stored.user_intent,
    )
    permit = await create_tool_permit(
        client,
        wallet_id=caller["agent_wallet_id"],
        key_id=caller["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-topk-query",
    )

    resp = await client.post(
        "/v1/awi/rag/query",
        json={"query": query, "top_k": 1, "similarity_threshold": 0.0},
        headers={
            **caller["agent_headers"],
            "X-Wallet-Id": caller["agent_wallet_id"],
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "awi-topk-query-1",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["memory_id"] for r in body["results"]] == [own_memory]
    assert body["total_found"] == 1
    assert other_memory not in resp.text
    assert stored.access_count == 0


# ── Client idempotency-key contract ──────────────────────────────────────────
# The governed AWI routes hold the ``Idempotency-Key`` header to the same
# contract as the MCP surfaces (``app.services.idempotency``): every line the
# client sent is validated, never coerced, must agree with every other line,
# and is stored verbatim as the replay identity.


async def _provision_rag_caller(
    client: AsyncClient, *, permit_idem_key: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """A funded agent wallet plus an ``awi_rag_query`` permit.

    Returns the provisioned wallet and the governed headers *without* an
    ``Idempotency-Key`` so each test supplies exactly the value under test.
    """
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key=permit_idem_key,
    )
    headers = {
        **provisioned["agent_headers"],
        "X-Wallet-Id": provisioned["agent_wallet_id"],
        "X-Permit-Id": permit["permit_id"],
    }
    return provisioned, headers


async def _rag_query_state(wallet_id: str) -> tuple[list[str], int]:
    """(stored replay identities for the rag route, wallet debit count).

    A debit is a negative ledger amount -- the invariant the ledger model
    states and the reconciler relies on -- rather than an action label.
    """
    factory = get_session_factory()
    async with factory() as session:
        keys = (
            (
                await session.execute(
                    select(IdempotencyRecordModel.idempotency_key).where(
                        IdempotencyRecordModel.wallet_id == wallet_id,
                        IdempotencyRecordModel.endpoint == RAG_QUERY_ENDPOINT,
                    )
                )
            )
            .scalars()
            .all()
        )
        debits = (
            await session.execute(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(
                    LedgerEntryModel.wallet_id == wallet_id,
                    LedgerEntryModel.amount < 0,
                )
            )
        ).scalar_one()
    return sorted(keys), debits


def _assert_invalid_key(detail: dict[str, Any], *, reason_code: str) -> None:
    assert detail["error"] == "invalid_idempotency_key"
    assert detail["reason_code"] == reason_code
    assert HEADER_SOURCE in detail["source"]
    assert detail["message"].startswith("invalid_idempotency_key")
    assert detail["remediation"]["type"] == "retry_with_valid_idempotency_key"
    assert detail["tool"] == "awi_rag_query"
    assert "receipt" not in detail


@pytest.mark.anyio
async def test_absent_idempotency_key_is_required(client, clean_database):
    """No header at all keeps the route's own ``idempotency_key_required``."""
    provisioned, headers = await _provision_rag_caller(
        client, permit_idem_key="permit-awi-key-absent"
    )
    resp = await client.post(
        "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=headers
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "idempotency_key_required"
    assert detail["tool"] == "awi_rag_query"
    assert await _rag_query_state(provisioned["agent_wallet_id"]) == ([], 0)


@pytest.mark.anyio
async def test_invalid_idempotency_key_is_refused_before_permit_presence(
    client, clean_database
):
    """A supplied-but-unusable key is reported even when the permit is missing.

    Key resolution used to sit behind the ``permit_required`` guard, so a
    request that got both headers wrong saw a bare 403 and never learned its
    key was unusable. The MCP surfaces check the key first; so does this.
    A request with *neither* header still gets ``permit_required``
    (``test_rag_query_denied_without_permit``).
    """
    provisioned, headers = await _provision_rag_caller(
        client, permit_idem_key="permit-awi-key-first"
    )
    del headers["X-Permit-Id"]
    headers["Idempotency-Key"] = "x" * (MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH + 1)
    resp = await client.post(
        "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=headers
    )
    assert resp.status_code == 400, resp.text
    _assert_invalid_key(resp.json()["detail"], reason_code="idempotency_key_too_long")
    assert await _rag_query_state(provisioned["agent_wallet_id"]) == ([], 0)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("header_value", "reason_code"),
    [
        ("", "idempotency_key_blank"),
        ("   ", "idempotency_key_blank"),
        ("x" * (MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH + 1), "idempotency_key_too_long"),
        ("awi\x00key", "idempotency_key_control_characters"),
        ("awi\x7fkey", "idempotency_key_control_characters"),
        # Latin-1 "é": not UTF-8, so it has no unambiguous reading as a key.
        (b"awi-\xe9-key", "idempotency_key_not_utf8"),
    ],
)
async def test_malformed_idempotency_key_is_refused_before_any_effect(
    client, clean_database, header_value, reason_code
):
    """A present-but-unusable key is refused under the shared contract.

    ``begin_awi_http_governed`` used to accept any non-blank header as-is: it
    applied no length cap, so a key wider than the 128-character store column
    passed permit validation and reached the database, and it never checked
    for control characters. Two identical retries must both be refused with
    the machine-actionable payload the MCP surfaces return, and with nothing
    written and nothing charged.
    """
    provisioned, headers = await _provision_rag_caller(
        client, permit_idem_key="permit-awi-key-bad"
    )
    headers["Idempotency-Key"] = header_value

    for _ in range(2):
        resp = await client.post(
            "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=headers
        )
        assert resp.status_code == 400, resp.text
        _assert_invalid_key(resp.json()["detail"], reason_code=reason_code)

    assert await _rag_query_state(provisioned["agent_wallet_id"]) == ([], 0)


@pytest.mark.anyio
async def test_conflicting_idempotency_key_lines_are_refused(client, clean_database):
    """Two header lines naming different keys is an ambiguity, refused as on MCP.

    The handlers used to read the key through a single-value ``Header``
    parameter, which surfaces only the first line, so a second line carrying
    a different key was never seen: the first key silently chose the replay
    identity and the action ran and was charged. Identical repeated lines
    collapse to one key and still work.
    """
    provisioned, headers = await _provision_rag_caller(
        client, permit_idem_key="permit-awi-key-dup"
    )
    conflicting = [
        *headers.items(),
        ("Idempotency-Key", "awi-dup-a"),
        ("Idempotency-Key", "awi-dup-b"),
    ]
    for _ in range(2):
        resp = await client.post(
            "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=conflicting
        )
        assert resp.status_code == 400, resp.text
        _assert_invalid_key(
            resp.json()["detail"], reason_code="idempotency_key_conflict"
        )
    assert await _rag_query_state(provisioned["agent_wallet_id"]) == ([], 0)

    repeated = [
        *headers.items(),
        ("Idempotency-Key", "awi-dup-same"),
        ("Idempotency-Key", "awi-dup-same"),
    ]
    resp = await client.post(
        "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=repeated
    )
    assert resp.status_code == 200, resp.text
    assert await _rag_query_state(provisioned["agent_wallet_id"]) == (
        ["awi-dup-same"],
        1,
    )


@pytest.mark.anyio
async def test_padded_idempotency_key_is_a_distinct_identity(client, clean_database):
    """The key is stored exactly as sent: ``' k'`` and ``'k'`` are two records.

    The old code stored ``idempotency_key.strip()``, so the two collapsed into
    one replay identity and the second call replayed the first receipt instead
    of running. Stripping was dropped deliberately: the Python SDK trims
    client-side before sending and no first-party caller sends a padded key,
    so the only server-side effect was to merge distinct client keys. The MCP
    surfaces store verbatim; the AWI routes now do the same.
    """
    provisioned, headers = await _provision_rag_caller(
        client, permit_idem_key="permit-awi-key-pad"
    )
    padded = await client.post(
        "/v1/awi/rag/query",
        json=RAG_QUERY_PAYLOAD,
        headers={**headers, "Idempotency-Key": " awi-pad-1"},
    )
    bare = await client.post(
        "/v1/awi/rag/query",
        json=RAG_QUERY_PAYLOAD,
        headers={**headers, "Idempotency-Key": "awi-pad-1"},
    )
    assert padded.status_code == 200, padded.text
    assert bare.status_code == 200, bare.text
    assert (
        padded.json()["receipt"]["receipt_id"] != bare.json()["receipt"]["receipt_id"]
    )
    assert await _rag_query_state(provisioned["agent_wallet_id"]) == (
        [" awi-pad-1", "awi-pad-1"],
        2,
    )


@pytest.mark.anyio
async def test_valid_idempotency_key_at_store_width_replays_same_receipt(
    client, clean_database
):
    """Positive control: a usable key -- here exactly the store width -- replays."""
    provisioned, headers = await _provision_rag_caller(
        client, permit_idem_key="permit-awi-key-ok"
    )
    key = "k" * MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH
    headers["Idempotency-Key"] = key

    first = await client.post(
        "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=headers
    )
    second = await client.post(
        "/v1/awi/rag/query", json=RAG_QUERY_PAYLOAD, headers=headers
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert (
        first.json()["receipt"]["receipt_id"] == second.json()["receipt"]["receipt_id"]
    )
    assert (
        first.json()["receipt"]["ledger_entry_id"]
        == second.json()["receipt"]["ledger_entry_id"]
    )
    assert await _rag_query_state(provisioned["agent_wallet_id"]) == ([key], 1)


@pytest.mark.anyio
async def test_awi_contended_charge_releases_the_key_before_effects(
    client, clean_database, monkeypatch
):
    """Definitive ledger contention precedes effects and permits a safe retry."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="awi_rag_query",
        max_credits=50,
        idem_key="permit-awi-contended",
    )
    headers = {
        **provisioned["agent_headers"],
        "X-Wallet-Id": provisioned["agent_wallet_id"],
        "X-Permit-Id": permit["permit_id"],
        "Idempotency-Key": "awi-contended-1",
    }

    real_flush = AsyncSession.flush

    async def locked_flush(session, *args, **kwargs):
        raise OperationalError(
            "UPDATE wallets ...", {}, Exception("database is locked")
        )

    monkeypatch.setattr(AsyncSession, "flush", locked_flush)
    resp = await client.post(
        "/v1/awi/rag/query",
        json={"query": "laptops", "top_k": 3},
        headers=headers,
    )
    monkeypatch.setattr(AsyncSession, "flush", real_flush)

    assert resp.status_code == 503, resp.text
    # Not flattened into charge_failed: an operator can still see it was a lost
    # write conflict and not a substantive failure of the charge itself.
    assert resp.json()["detail"]["error"] == "ledger_write_contended"

    factory = get_session_factory()
    # The reservation is the other half of this branch. Nothing was charged, so
    # nothing may stay reserved against the permit before a safe retry.
    async with factory() as session:
        reserved = (
            await session.execute(
                select(PermitModel.spent_credits).where(
                    PermitModel.permit_id == permit["permit_id"]
                )
            )
        ).scalar_one()
    assert Decimal(str(reserved)) == Decimal("0")

    # Nothing ran and compensation succeeded, so this key can retry.
    async with factory() as session:
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(IdempotencyRecordModel)
                .where(
                    IdempotencyRecordModel.wallet_id == provisioned["agent_wallet_id"],
                    IdempotencyRecordModel.idempotency_key == "awi-contended-1",
                )
            )
        ).scalar_one()
    assert remaining == 0

    # The retry now executes once after its debit succeeds.
    retry = await client.post(
        "/v1/awi/rag/query",
        json={"query": "laptops", "top_k": 3},
        headers=headers,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["receipt"]["outcome"] == "success"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["raised", "structured_zero", "structured_partial"])
async def test_execute_dom_bridge_failure_retains_charge_and_replays(
    client, clean_database, monkeypatch, failure
):
    """Browser failure is receipted as uncertain and cannot redispatch on replay.

    Even zero completed commands may mean the first command took effect before
    raising. Admission is retained because refunding would assume no effect.
    """
    from app.services.awi_session import get_awi_session_manager

    provisioned = await provision_agent_wallet(client)
    wallet_id = provisioned["agent_wallet_id"]
    headers = provisioned["agent_headers"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=provisioned["key_id"],
        tool_name="awi_execute",
        max_credits=50,
        idem_key="permit-awi-dom-failure",
    )
    create = await client.post(
        "/v1/awi/sessions",
        json={"target_url": "https://example.com", "wallet_id": wallet_id},
        headers=headers,
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]

    manager = get_awi_session_manager()
    manager._dom_sessions[session_id] = "dom-session-under-test"

    calls = 0

    async def _bridge_raises(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if failure == "raised":
            raise RuntimeError("playwright target closed")
        return {
            "success": False,
            "commands_executed": 1 if failure == "structured_partial" else 0,
            "error": "synthetic command failure",
        }

    monkeypatch.setattr(manager, "_execute_via_dom_bridge", _bridge_raises)
    try:
        exec_headers = {
            **headers,
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "awi-dom-failure-1",
        }
        body = {
            "session_id": session_id,
            "action": "navigate_to",
            "parameters": {"url": "https://example.com/next"},
        }
        resp = await client.post("/v1/awi/execute", json=body, headers=exec_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "error"
        assert data["error"].startswith("dom_bridge_failed")
        assert data["effect_status"] == "unknown"
        assert data["receipt"]["outcome"] == "delivery_uncertain"
        assert data["receipt"]["ledger_entry_id"]

        receipt_resp = await client.get(
            f"/v1/receipts/{data['receipt']['receipt_id']}", headers=headers
        )
        assert receipt_resp.status_code == 200
        receipt = receipt_resp.json()
        assert receipt["outcome"] == "delivery_uncertain"
        assert Decimal(str(receipt["credits_charged"])) == Decimal("3")
        assert receipt["reason_code"] == "dom_bridge_failed"

        factory = get_session_factory()
        async with factory() as session:
            debits = (
                await session.execute(
                    select(func.count())
                    .select_from(LedgerEntryModel)
                    .where(
                        LedgerEntryModel.wallet_id == wallet_id,
                        LedgerEntryModel.amount < 0,
                    )
                )
            ).scalar_one()
            spent = (
                await session.execute(
                    select(PermitModel.spent_credits).where(
                        PermitModel.permit_id == permit["permit_id"]
                    )
                )
            ).scalar_one()
        assert debits == 1, "an uncertain DOM action must keep its debit"
        assert Decimal(str(spent or 0)) == Decimal("3")

        # Same key replays the identical typed failure without re-running.
        replay = await client.post("/v1/awi/execute", json=body, headers=exec_headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt"]["receipt_id"] == data["receipt"]["receipt_id"]
        assert calls == 1
        async with factory() as session:
            debits_after = (
                await session.execute(
                    select(func.count())
                    .select_from(LedgerEntryModel)
                    .where(
                        LedgerEntryModel.wallet_id == wallet_id,
                        LedgerEntryModel.amount < 0,
                    )
                )
            ).scalar_one()
        assert debits_after == 1
    finally:
        manager._dom_sessions.pop(session_id, None)
