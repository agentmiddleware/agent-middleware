"""Carry advisory evidence through existing signed audit/receipt storage."""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session_factory
from app.db.models import ControlPlaneAuditEventModel
from app.services.signing_keys import sha256_hex


def jev_audit_id(wallet_id: str, endpoint: str, idempotency_key: str) -> str:
    identity = {"wallet_id": wallet_id, "endpoint": endpoint, "idempotency_key": idempotency_key}
    return f"audit-jev-{sha256_hex(identity)[:40]}"


async def load_jev_guard_metadata(
    event_id: str | None,
    wallet_id: str,
    *,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    if event_id is None:
        return None
    if session is None:
        async with get_session_factory()() as owned_session:
            return await load_jev_guard_metadata(
                event_id, wallet_id, session=owned_session
            )
    row = await session.get(ControlPlaneAuditEventModel, event_id)
    if (
        row is None
        or row.wallet_id != wallet_id
        or row.event not in {"mcp.invoke", "jev.risk_guard"}
    ):
        return None
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    guard = metadata.get("jev_risk_guard")
    return guard if isinstance(guard, dict) else None
