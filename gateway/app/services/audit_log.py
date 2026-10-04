from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, cast
import uuid

from sqlalchemy import desc, select
from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

from app.core.time import to_naive_utc
from app.db.database import get_session_factory
from app.db.models import ControlPlaneAuditEventModel

# Rows read to tally the one summary bucket SQL cannot group portably (policy
# reasons live in the metadata JSON). Every other summary count is a SQL
# aggregate over all matching events, so only that bucket can be partial.
AUDIT_SUMMARY_SCAN_LIMIT = 10_000


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    created_at: datetime
    event: str
    wallet_id: str | None
    tool: str | None
    endpoint: str | None
    auth_source: str | None
    key_id: str | None
    policy_decision_id: str | None
    request_id: str | None
    ok: bool
    error: str | None
    metadata: dict[str, Any]
    payload_hash: str | None
    previous_hash: str | None
    chain_hash: str | None
    signature: str | None
    signature_key_id: str | None


@dataclass(frozen=True)
class AuditEventCount:
    event: str
    wallet_id: str | None
    ok: bool
    count: int


def _to_event(row: ControlPlaneAuditEventModel) -> AuditEvent:
    metadata: dict[str, Any] = {}
    if row.metadata_json:
        try:
            decoded = json.loads(row.metadata_json)
        except json.JSONDecodeError:
            decoded = {}
        if isinstance(decoded, dict):
            metadata = decoded
    return AuditEvent(
        event_id=row.event_id,
        created_at=row.created_at,
        event=row.event,
        wallet_id=row.wallet_id,
        tool=row.tool,
        endpoint=row.endpoint,
        auth_source=row.auth_source,
        key_id=row.key_id,
        policy_decision_id=row.policy_decision_id,
        request_id=row.request_id,
        ok=row.ok,
        error=row.error,
        metadata=metadata,
        payload_hash=row.payload_hash,
        previous_hash=row.previous_hash,
        chain_hash=row.chain_hash,
        signature=row.signature,
        signature_key_id=row.signature_key_id,
    )


async def record_audit_event(
    *,
    event: str,
    event_id: str | None = None,
    created_at: datetime | None = None,
    wallet_id: str | None = None,
    tool: str | None = None,
    endpoint: str | None = None,
    auth_source: str | None = None,
    key_id: str | None = None,
    policy_decision_id: str | None = None,
    request_id: str | None = None,
    ok: bool = True,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    if event_id is not None and (not event_id or len(event_id) > 50):
        raise ValueError("audit_event_id_invalid")
    model_kwargs: dict[str, Any] = {
        "event_id": event_id or f"audit-{uuid.uuid4().hex[:16]}",
        "event": event,
        "wallet_id": wallet_id,
        "tool": tool,
        "endpoint": endpoint,
        "auth_source": auth_source,
        "key_id": key_id,
        "policy_decision_id": policy_decision_id,
        "request_id": request_id,
        "ok": ok,
        "error": error,
        "metadata_json": json.dumps(metadata or {}, default=str),
    }
    if created_at is not None:
        model_kwargs["created_at"] = to_naive_utc(created_at)
    model = ControlPlaneAuditEventModel(**model_kwargs)
    from app.services.audit_chain import append_chained_audit_event

    # Sign + persist against the per-wallet chain head, which is advanced by an
    # optimistic conditional UPDATE (with retry) rather than a lock, so
    # concurrent writers cannot fork the hash chain.
    persisted = await append_chained_audit_event(model)
    return _to_event(persisted)


async def list_audit_events(
    *,
    event: str | None = None,
    wallet_id: str | None = None,
    key_id: str | None = None,
    tool: str | None = None,
    endpoint: str | None = None,
    policy_decision_id: str | None = None,
    request_id: str | None = None,
    ok: bool | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditEvent]:
    stmt = (
        select(ControlPlaneAuditEventModel)
        .order_by(
            desc(cast(ColumnElement[Any], ControlPlaneAuditEventModel.created_at))
        )
        .limit(limit)
        .offset(offset)
    )
    stmt = _apply_audit_filters(
        stmt,
        event=event,
        wallet_id=wallet_id,
        key_id=key_id,
        tool=tool,
        endpoint=endpoint,
        policy_decision_id=policy_decision_id,
        request_id=request_id,
        ok=ok,
        created_after=created_after,
        created_before=created_before,
    )

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(stmt)
        return [_to_event(row) for row in result.scalars().all()]


async def count_audit_events(
    *,
    event: str | None = None,
    wallet_id: str | None = None,
    key_id: str | None = None,
    tool: str | None = None,
    endpoint: str | None = None,
    policy_decision_id: str | None = None,
    request_id: str | None = None,
    ok: bool | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> int:
    stmt = select(func.count()).select_from(ControlPlaneAuditEventModel)
    stmt = _apply_audit_filters(
        stmt,
        event=event,
        wallet_id=wallet_id,
        key_id=key_id,
        tool=tool,
        endpoint=endpoint,
        policy_decision_id=policy_decision_id,
        request_id=request_id,
        ok=ok,
        created_after=created_after,
        created_before=created_before,
    )

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(stmt)
        return int(result.scalar_one())


async def count_audit_events_grouped(
    *,
    event: str | None = None,
    wallet_id: str | None = None,
    key_id: str | None = None,
    tool: str | None = None,
    endpoint: str | None = None,
    policy_decision_id: str | None = None,
    request_id: str | None = None,
    ok: bool | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
) -> list[AuditEventCount]:
    """Count matching events per (event, wallet_id, ok), in SQL.

    Summaries are built from these counts rather than from listed rows, so
    they cover every matching event instead of only the newest page of them.
    """
    columns = (
        cast(ColumnElement[str], ControlPlaneAuditEventModel.event),
        cast(ColumnElement[str | None], ControlPlaneAuditEventModel.wallet_id),
        cast(ColumnElement[bool], ControlPlaneAuditEventModel.ok),
    )
    stmt = select(*columns, func.count()).group_by(*columns)
    stmt = _apply_audit_filters(
        stmt,
        event=event,
        wallet_id=wallet_id,
        key_id=key_id,
        tool=tool,
        endpoint=endpoint,
        policy_decision_id=policy_decision_id,
        request_id=request_id,
        ok=ok,
        created_after=created_after,
        created_before=created_before,
    )

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(stmt)
        return [
            AuditEventCount(
                event=row[0], wallet_id=row[1], ok=bool(row[2]), count=int(row[3])
            )
            for row in result.all()
        ]


async def summarize_audit_events(
    *,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    wallet_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate audit events, optionally for a single wallet.

    ``wallet_id`` scopes the aggregate to one tenant, which is what lets a
    wallet key read a summary of its own activity without an operator key.
    Unscoped (admin) summaries still span every wallet.

    ``total``, ``by_event``, ``by_outcome`` and ``by_wallet`` are exact.
    ``by_policy_reason`` is tallied from at most ``AUDIT_SUMMARY_SCAN_LIMIT``
    of the newest events; ``by_policy_reason_truncated`` says when that cap
    left events out, so a partial tally is never mistaken for a full one.
    """
    summary: dict[str, Any] = {
        "total": 0,
        "by_event": {},
        "by_outcome": {"ok": 0, "error": 0},
        "by_wallet": {},
        "by_policy_reason": {},
        "by_policy_reason_truncated": False,
    }
    for bucket in await count_audit_events_grouped(
        created_after=created_after,
        created_before=created_before,
        wallet_id=wallet_id,
    ):
        summary["total"] += bucket.count
        summary["by_event"][bucket.event] = (
            summary["by_event"].get(bucket.event, 0) + bucket.count
        )
        outcome = "ok" if bucket.ok else "error"
        summary["by_outcome"][outcome] += bucket.count
        wallet_key = bucket.wallet_id or "unknown"
        summary["by_wallet"][wallet_key] = (
            summary["by_wallet"].get(wallet_key, 0) + bucket.count
        )

    events = await list_audit_events(
        created_after=created_after,
        created_before=created_before,
        wallet_id=wallet_id,
        limit=AUDIT_SUMMARY_SCAN_LIMIT,
    )
    for event in events:
        reason = str(event.metadata.get("policy_reason") or event.error or "unknown")
        summary["by_policy_reason"][reason] = (
            summary["by_policy_reason"].get(reason, 0) + 1
        )
    summary["by_policy_reason_truncated"] = len(events) < summary["total"]
    return summary


def _apply_audit_filters(stmt, **filters):
    event = filters.get("event")
    wallet_id = filters.get("wallet_id")
    key_id = filters.get("key_id")
    tool = filters.get("tool")
    endpoint = filters.get("endpoint")
    policy_decision_id = filters.get("policy_decision_id")
    request_id = filters.get("request_id")
    ok = filters.get("ok")
    created_after = filters.get("created_after")
    created_before = filters.get("created_before")

    if event:
        stmt = stmt.where(ControlPlaneAuditEventModel.event == event)
    if wallet_id:
        stmt = stmt.where(ControlPlaneAuditEventModel.wallet_id == wallet_id)
    if key_id:
        stmt = stmt.where(ControlPlaneAuditEventModel.key_id == key_id)
    if tool:
        stmt = stmt.where(ControlPlaneAuditEventModel.tool == tool)
    if endpoint:
        stmt = stmt.where(ControlPlaneAuditEventModel.endpoint == endpoint)
    if policy_decision_id:
        stmt = stmt.where(
            ControlPlaneAuditEventModel.policy_decision_id == policy_decision_id
        )
    if request_id:
        stmt = stmt.where(ControlPlaneAuditEventModel.request_id == request_id)
    if ok is not None:
        stmt = stmt.where(ControlPlaneAuditEventModel.ok == ok)
    if created_after:
        created_after = to_naive_utc(created_after)
        stmt = stmt.where(ControlPlaneAuditEventModel.created_at >= created_after)
    if created_before:
        created_before = to_naive_utc(created_before)
        stmt = stmt.where(ControlPlaneAuditEventModel.created_at <= created_before)
    return stmt
