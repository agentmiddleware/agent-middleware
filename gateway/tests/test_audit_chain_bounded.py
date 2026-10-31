"""Audit chain verification is bounded, and says so when it is partial."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import AuditChainHeadModel, ControlPlaneAuditEventModel
from app.main import app
from app.services import audit_chain
from app.core.time import utc_now
from app.services.audit_chain import (
    audit_payload,
    verify_audit_chain,
    verify_audit_event_link,
)
from app.services.signing_keys import get_signing_key_service, sha256_hex
from app.services.audit_log import record_audit_event
from tests.test_evidence_bundle import _invoke_governed_tool
from tests.test_trust_helpers import provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _set_cap(monkeypatch, cap: int) -> None:
    monkeypatch.setattr(get_settings(), "AUDIT_VERIFY_MAX_EVENTS", cap)


async def _seed(wallet_id: str | None, count: int) -> None:
    for n in range(count):
        await record_audit_event(
            event="trust.bounded", wallet_id=wallet_id, metadata={"n": n}
        )


async def _walk(wallet_id: str, **kwargs):
    """Follow next_after_seq to the end; return every verdict in order."""
    verdicts = []
    after_seq = None
    after_event_id = None
    for _ in range(100):
        verdict = await verify_audit_chain(
            wallet_id=wallet_id,
            after_seq=after_seq,
            after_event_id=after_event_id,
            **kwargs,
        )
        verdicts.append(verdict)
        if verdict.complete or not verdict.valid or verdict.next_after_seq is None:
            return verdicts
        after_seq = verdict.next_after_seq
        after_event_id = verdict.next_after_event_id
    raise AssertionError("cursor did not terminate")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _CountingSession:
    """Wraps a session and records how many audit event rows each query loads."""

    loads: list[int] = []

    def __init__(self, inner):
        self.inner = inner

    async def __aenter__(self):
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.inner.__aexit__(*args)

    async def execute(self, stmt, *args, **kwargs):
        rows = (await self.inner.execute(stmt, *args, **kwargs)).all()
        type(self).loads.append(
            sum(
                1
                for row in rows
                if len(row) and isinstance(row[0], ControlPlaneAuditEventModel)
            )
        )

        return _Rows(rows)


@pytest.mark.anyio
async def test_long_chain_is_walked_in_bounded_pages(clean_database, monkeypatch):
    wallet_id = "wlt-bounded-long"
    await _seed(wallet_id, 3000)
    _set_cap(monkeypatch, 500)

    factory = get_session_factory()

    class Counting(_CountingSession):
        loads: list[int] = []

    monkeypatch.setattr(
        audit_chain, "get_session_factory", lambda: lambda: Counting(factory())
    )
    first = await verify_audit_chain(wallet_id=wallet_id)

    assert sum(Counting.loads) <= 500
    assert max(Counting.loads) <= audit_chain._VERIFY_PAGE_SIZE
    assert first.valid is True
    assert first.complete is False
    assert first.checked_events == 500
    assert first.next_after_seq is not None
    monkeypatch.setattr(audit_chain, "get_session_factory", lambda: factory)

    # Following the cursor covers the whole chain; only the last verdict is
    # complete, and only it ran the head and tail checks.
    verdicts = await _walk(wallet_id)
    assert [v.complete for v in verdicts] == [False] * 5 + [True]
    assert all(v.valid for v in verdicts)
    assert sum(v.checked_events for v in verdicts) == 3000
    assert all(v.next_after_seq is not None for v in verdicts[:-1])
    assert verdicts[-1].next_after_seq is None


@pytest.mark.anyio
async def test_chain_exactly_at_the_cap_is_complete(clean_database, monkeypatch):
    wallet_id = "wlt-bounded-exact"
    await _seed(wallet_id, 12)
    _set_cap(monkeypatch, 12)
    result = await verify_audit_chain(wallet_id=wallet_id)
    assert result.valid and result.complete and result.checked_events == 12
    assert result.next_after_seq is None


@pytest.mark.anyio
async def test_deleted_tail_is_found_only_when_the_walk_reaches_the_head(
    clean_database, monkeypatch
):
    wallet_id = "wlt-bounded-tail"
    await _seed(wallet_id, 25)
    factory = get_session_factory()
    async with factory() as session:
        last = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.seq.desc())
                )
            )
            .scalars()
            .first()
        )
        await session.delete(last)
        await session.commit()

    verdicts = await _walk(wallet_id, limit=10)
    # The capped spans are internally sound and say they are incomplete.
    assert [(v.valid, v.complete) for v in verdicts[:-1]] == [(True, False)] * 2
    assert verdicts[-1].valid is False
    assert verdicts[-1].reason == "audit_chain_truncated"
    assert verdicts[-1].complete is True


@pytest.mark.anyio
async def test_missing_head_is_detected(clean_database):
    wallet_id = "wlt-bounded-head"
    await _seed(wallet_id, 8)
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            delete(AuditChainHeadModel).where(
                AuditChainHeadModel.wallet_key == wallet_id
            )
        )
        await session.commit()
    result = await verify_audit_chain(wallet_id=wallet_id, limit=3)
    assert result.valid is False
    assert result.reason == "audit_chain_head_missing"
    assert result.complete is False


@pytest.mark.anyio
async def test_deleted_middle_event_is_detected_on_the_page_that_spans_it(
    clean_database,
):
    wallet_id = "wlt-bounded-hole"
    await _seed(wallet_id, 20)
    factory = get_session_factory()
    async with factory() as session:
        victim = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel).where(
                        ControlPlaneAuditEventModel.wallet_id == wallet_id,
                        ControlPlaneAuditEventModel.seq == 12,
                    )
                )
            )
            .scalars()
            .one()
        )
        await session.delete(victim)
        await session.commit()
    verdicts = await _walk(wallet_id, limit=5)
    assert verdicts[0].valid and verdicts[1].valid
    bad = verdicts[-1]
    assert bad.valid is False
    assert bad.reason == "audit_previous_hash_mismatch"


@pytest.mark.anyio
async def test_tamper_inside_a_capped_span_is_detected(clean_database):
    wallet_id = "wlt-bounded-tamper"
    await _seed(wallet_id, 20)
    factory = get_session_factory()
    async with factory() as session:
        event = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel).where(
                        ControlPlaneAuditEventModel.wallet_id == wallet_id,
                        ControlPlaneAuditEventModel.seq == 3,
                    )
                )
            )
            .scalars()
            .one()
        )
        event.metadata_json = '{"n": 666}'
        session.add(event)
        await session.commit()
    result = await verify_audit_chain(wallet_id=wallet_id, limit=5)
    assert result.valid is False
    assert result.reason == "audit_payload_hash_mismatch"
    assert result.broken_event_id == event.event_id


@pytest.mark.anyio
async def test_cursor_and_limit_edges(clean_database, monkeypatch):
    wallet_id = "wlt-bounded-edges"
    await _seed(wallet_id, 10)
    _set_cap(monkeypatch, 4)
    # A limit above the maximum is clamped to it.
    clamped = await verify_audit_chain(wallet_id=wallet_id, limit=10**6)
    assert clamped.checked_events == 4 and clamped.complete is False
    # A cursor that names no event cannot anchor a walk.
    missing = await verify_audit_chain(wallet_id=wallet_id, after_seq=9999)
    assert missing.valid is False
    assert missing.reason == "audit_cursor_event_missing"
    # Resuming mid-chain verifies the link to the cursor event.
    resumed = await verify_audit_chain(wallet_id=wallet_id, after_seq=8)
    assert resumed.valid and resumed.complete and resumed.checked_events == 2
    # A cursor spans one chain; a global walk refuses it.
    with pytest.raises(ValueError, match="audit_after_seq_requires_wallet"):
        await verify_audit_chain(wallet_id=None, after_seq=1)


@pytest.mark.anyio
async def test_global_walk_applies_the_cap_per_wallet(clean_database, monkeypatch):
    await _seed("wlt-global-a", 6)
    await _seed("wlt-global-b", 3)
    _set_cap(monkeypatch, 4)
    result = await verify_audit_chain(wallet_id=None)
    assert result.valid is True
    assert result.complete is False
    assert result.checked_events == 4 + 3


@pytest.mark.anyio
async def test_verify_chain_endpoint_pages_and_rejects_bad_input(
    client, clean_database
):
    owner = await provision_agent_wallet(client)
    other = await provision_agent_wallet(client)
    await _seed(owner["agent_wallet_id"], 6)

    first = await client.post(
        "/v1/audit/verify-chain", json={"limit": 3}, headers=owner["agent_headers"]
    )
    assert first.status_code == 200
    body = first.json()
    assert body["valid"] is True and body["complete"] is False
    assert body["checked_events"] == 3
    cursor = body["next_after_seq"]
    assert cursor is not None
    done = None
    for _ in range(10):
        resp = await client.post(
            "/v1/audit/verify-chain",
            json={"after_seq": cursor, "limit": 3},
            headers=owner["agent_headers"],
        )
        assert resp.status_code == 200
        done = resp.json()
        if done["complete"]:
            break
        cursor = done["next_after_seq"]
    assert done is not None and done["complete"] is True and done["valid"] is True

    # limit below 1 and a negative cursor are rejected.
    for payload in ({"limit": 0}, {"after_seq": -1}):
        bad = await client.post(
            "/v1/audit/verify-chain", json=payload, headers=owner["agent_headers"]
        )
        assert bad.status_code == 422
    # A limit above the maximum is clamped, not an error.
    clamped = await client.post(
        "/v1/audit/verify-chain",
        json={"limit": 10**9},
        headers=owner["agent_headers"],
    )
    assert clamped.status_code == 200

    # Wallet A's key cannot verify wallet B's chain.
    cross = await client.post(
        "/v1/audit/verify-chain",
        json={"wallet_id": other["agent_wallet_id"]},
        headers=owner["agent_headers"],
    )
    assert cross.status_code == 403
    # A cursor with no wallet cannot be used for a global walk.
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS

    global_cursor = await client.post(
        "/v1/audit/verify-chain",
        json={"after_seq": 1},
        headers=BOOTSTRAP_HEADERS,
    )
    assert global_cursor.status_code == 422
    # Same list shape as the documented HTTPValidationError.
    detail = global_cursor.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "after_seq"]


@pytest.mark.anyio
async def test_receipt_evidence_does_not_walk_the_wallet_chain(
    client, clean_database, monkeypatch
):
    owner = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, owner)
    await _seed(owner["agent_wallet_id"], 30)

    async def _forbidden(*args, **kwargs):
        raise AssertionError("evidence must not walk the whole wallet chain")

    monkeypatch.setattr(audit_chain, "verify_audit_chain", _forbidden)

    for path in (
        f"/v1/receipts/{receipt['receipt_id']}/evidence",
        f"/v1/evidence/{receipt['receipt_id']}",
    ):
        resp = await client.get(path, headers=owner["agent_headers"])
        assert resp.status_code == 200, resp.text
    detail = (
        await client.get(
            f"/v1/receipts/{receipt['receipt_id']}/evidence",
            headers=owner["agent_headers"],
        )
    ).json()
    check = next(c for c in detail["checks"] if c["name"] == "audit_chain")
    assert check["status"] == "passed"
    assert check["details"]["chain_walk"] == "not_run"
    assert check["details"]["checked_events"] == 1
    assert detail["audit_chain"]["valid"] is True
    assert detail["audit_chain"]["complete"] is False


@pytest.mark.anyio
async def test_receipt_evidence_catches_tamper_of_its_own_event(client, clean_database):
    owner = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, owner)
    factory = get_session_factory()
    async with factory() as session:
        event = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel).where(
                        ControlPlaneAuditEventModel.event_id
                        == receipt["audit_event_id"]
                    )
                )
            )
            .scalars()
            .one()
        )
        event.error = "tampered"
        session.add(event)
        await session.commit()
    resp = await client.get(
        f"/v1/receipts/{receipt['receipt_id']}/evidence",
        headers=owner["agent_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    check = next(c for c in body["checks"] if c["name"] == "audit_chain")
    assert check["status"] == "failed"
    assert check["reason"] == "audit_payload_hash_mismatch"
    assert body["valid"] is False


async def _seed_legacy(wallet_id: str, count: int) -> list[str]:
    """Insert a chain signed before seq existed: every row stores seq=0."""
    service = get_signing_key_service()
    key = await service.ensure_active_key()
    start = utc_now()
    previous_hash = None
    rows = []
    for n in range(count):
        model = ControlPlaneAuditEventModel(
            event_id=f"audit-legacy-{n:05d}",
            created_at=start + timedelta(milliseconds=n),
            seq=0,
            event="trust.legacy",
            wallet_id=wallet_id,
            ok=True,
            metadata_json="{}",
        )
        model.previous_hash = previous_hash
        payload = audit_payload(
            event_id=model.event_id,
            created_at=model.created_at,
            event=model.event,
            wallet_id=wallet_id,
            tool=None,
            endpoint=None,
            auth_source=None,
            key_id=None,
            policy_decision_id=None,
            request_id=None,
            ok=True,
            error=None,
            metadata_json="{}",
            previous_hash=previous_hash,
        )
        payload_hash = sha256_hex(payload)
        payload["payload_hash"] = payload_hash
        signature, signature_key_id, _ = service.sign_payload_with_key_id(
            payload, key.key_id
        )
        model.payload_hash = payload_hash
        model.signature = signature
        model.signature_key_id = signature_key_id
        model.chain_hash = sha256_hex(
            {
                "previous_hash": previous_hash,
                "payload_hash": payload_hash,
                "signature": signature,
            }
        )
        previous_hash = model.chain_hash
        rows.append(model)
    factory = get_session_factory()
    async with factory() as session:
        session.add_all(rows)
        session.add(
            AuditChainHeadModel(
                wallet_key=wallet_id,
                last_seq=0,
                last_chain_hash=previous_hash,
                updated_at=utc_now(),
            )
        )
        await session.commit()
    return [row.event_id for row in rows]


@pytest.mark.anyio
async def test_legacy_seq_zero_chain_longer_than_a_page_pages_cleanly(
    clean_database, monkeypatch
):
    wallet_id = "wlt-bounded-legacy"
    count = audit_chain._VERIFY_PAGE_SIZE * 2 + 40
    event_ids = await _seed_legacy(wallet_id, count)
    _set_cap(monkeypatch, audit_chain._VERIFY_PAGE_SIZE + 30)

    verdicts = await _walk(wallet_id)
    assert all(v.valid for v in verdicts), verdicts
    assert verdicts[-1].complete is True
    assert sum(v.checked_events for v in verdicts) == count
    # The cursor lands inside the seq=0 run, so it must name the event.
    assert verdicts[0].next_after_seq == 0
    assert verdicts[0].next_after_event_id is not None
    # A bare seq=0 cursor cannot say where in the run to resume.
    ambiguous = await verify_audit_chain(wallet_id=wallet_id, after_seq=0)
    assert ambiguous.valid is False
    assert ambiguous.reason == "audit_cursor_ambiguous"
    assert event_ids  # seeded


@pytest.mark.anyio
async def test_single_event_link_resolves_legacy_predecessor_by_hash(clean_database):
    wallet_id = "wlt-bounded-legacy-link"
    await _seed_legacy(wallet_id, 3)
    factory = get_session_factory()
    async with factory() as session:
        events = (
            (
                await session.execute(
                    select(ControlPlaneAuditEventModel)
                    .where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
                    .order_by(ControlPlaneAuditEventModel.created_at)
                )
            )
            .scalars()
            .all()
        )
    for event in events:
        assert (await verify_audit_event_link(event)).valid is True
    # Delete the predecessor: events[2].previous_hash now names no row, which
    # reaches the link check itself rather than failing the signed payload.
    async with factory() as session:
        await session.execute(
            delete(ControlPlaneAuditEventModel).where(
                ControlPlaneAuditEventModel.event_id == events[1].event_id
            )
        )
        await session.commit()
    result = await verify_audit_event_link(events[2])
    assert result.valid is False
    assert result.reason == "audit_previous_hash_mismatch"
