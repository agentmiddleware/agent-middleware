"""
PostgreSQL / SQLite-backed durable store for agent-to-agent messages.

Used only when ``SIMULATION_MODE_AGENT_COMMS`` is false and ``DATABASE_URL`` is set.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlmodel import col

from ..db.database import get_session_factory, is_database_configured
from ..db.models import AgentCommsMessageModel

logger = logging.getLogger(__name__)

QUEUED = "queued"
DELIVERED = "delivered"


def compute_payload_hash(
    *,
    body: dict[str, Any],
    correlation_id: str | None,
    from_agent: str,
    message_type: str,
    priority: str,
    reply_to: str | None,
    subject: str,
    to_agent: str,
) -> str:
    blob: dict[str, Any] = {
        "body": body,
        "correlation_id": correlation_id,
        "from_agent": from_agent,
        "message_type": message_type,
        "priority": priority,
        "reply_to": reply_to,
        "subject": subject,
        "to_agent": to_agent,
    }
    return hashlib.sha256(
        json.dumps(blob, sort_keys=True, default=str).encode()
    ).hexdigest()


class CommsMessageStore:
    """Persist and list agent comms messages in the primary app database."""

    @staticmethod
    def _require_db() -> None:
        if not is_database_configured():
            raise RuntimeError(
                "CommsMessageStore requires DATABASE_URL. "
                "Disable simulation or configure the database."
            )

    async def insert_message(
        self,
        *,
        message_id: str,
        from_agent: str,
        to_agent: str,
        message_type: str,
        priority: str,
        subject: str,
        body: dict[str, Any],
        correlation_id: str | None,
        reply_to: str | None,
        status: str,
        payload_hash: str,
        created_at: datetime,
        delivered_at: datetime | None,
    ) -> None:
        """Insert final routed message state (one row per send)."""
        self._require_db()
        factory = get_session_factory()
        row = AgentCommsMessageModel(
            message_id=message_id,
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            priority=priority,
            subject=subject,
            body_json=json.dumps(body, sort_keys=True, default=str),
            correlation_id=correlation_id,
            reply_to=reply_to,
            status=status,
            payload_hash=payload_hash,
            created_at=created_at,
            delivered_at=delivered_at,
        )
        async with factory() as session:
            session.add(row)
            await session.commit()

    async def list_inbox_for_agent(
        self,
        to_agent: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Return paginated inbox rows for ``to_agent``, ordered by ``created_at`` ASC.
        Messages still ``queued`` are marked ``delivered`` and ``delivered_at`` set.
        """
        self._require_db()
        factory = get_session_factory()
        filt = col(AgentCommsMessageModel.to_agent) == to_agent
        count_stmt = (
            select(func.count()).select_from(AgentCommsMessageModel).where(filt)
        )
        stmt = (
            select(AgentCommsMessageModel)
            .where(filt)
            .order_by(col(AgentCommsMessageModel.created_at).asc())
            .limit(limit)
            .offset(offset)
        )
        now = datetime.now(timezone.utc)
        async with factory() as session:
            total = int((await session.execute(count_stmt)).scalar_one())
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            for row in rows:
                if row.status == QUEUED:
                    row.status = DELIVERED
                    row.delivered_at = now
            await session.commit()

        out: list[dict[str, Any]] = []
        for row in rows:
            body: dict[str, Any] = {}
            if row.body_json:
                try:
                    body = json.loads(row.body_json)
                except json.JSONDecodeError:
                    logger.warning("Corrupt body_json for message %s", row.message_id)
            out.append(
                {
                    "message_id": row.message_id,
                    "from_agent": row.from_agent,
                    "to_agent": row.to_agent,
                    "message_type": row.message_type,
                    "priority": row.priority,
                    "subject": row.subject,
                    "body": body,
                    "correlation_id": row.correlation_id,
                    "reply_to": row.reply_to,
                    "status": row.status,
                    "payload_hash": row.payload_hash,
                    "created_at": row.created_at,
                    "delivered_at": row.delivered_at,
                }
            )
        return out, total
