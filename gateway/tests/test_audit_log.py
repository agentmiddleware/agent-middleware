from datetime import timedelta, timezone

import pytest

from app.core.time import to_naive_utc
from app.db.database import get_session_factory
from app.db.models import ControlPlaneAuditEventModel
from app.services.audit_log import list_audit_events, record_audit_event


@pytest.mark.anyio
async def test_record_and_list_audit_events(clean_database):
    event = await record_audit_event(
        event="mcp.invoke",
        wallet_id="wallet-1",
        tool="echo",
        endpoint="/mcp/messages",
        auth_source="db",
        key_id="key-1",
        policy_decision_id="pol-1",
        request_id="req-1",
        ok=True,
        metadata={"cost": 2.0},
    )

    events = await list_audit_events(wallet_id="wallet-1")

    assert event.event_id.startswith("audit-")
    assert len(events) == 1
    assert events[0].event == "mcp.invoke"
    assert events[0].wallet_id == "wallet-1"
    assert events[0].tool == "echo"
    assert events[0].policy_decision_id == "pol-1"
    assert events[0].metadata["cost"] == 2.0


@pytest.mark.anyio
async def test_list_audit_events_filters_by_tool(clean_database):
    await record_audit_event(event="mcp.invoke", wallet_id="wallet-1", tool="echo")
    await record_audit_event(event="mcp.invoke", wallet_id="wallet-1", tool="search")

    events = await list_audit_events(tool="echo")

    assert len(events) == 1
    assert events[0].tool == "echo"


@pytest.mark.anyio
async def test_list_audit_events_normalizes_offset_time_filters(
    clean_database,
    enforce_naive_utc_datetime_columns,
):
    event = await record_audit_event(event="mcp.invoke", wallet_id="wallet-1")
    event_utc = event.created_at.replace(tzinfo=timezone.utc)

    events = await list_audit_events(
        created_after=(event_utc - timedelta(minutes=1)).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        ),
        created_before=(event_utc + timedelta(minutes=1)).astimezone(
            timezone(timedelta(hours=-7))
        ),
    )

    assert [item.event_id for item in events] == [event.event_id]
    assert to_naive_utc(event_utc) == event.created_at


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("event_id", "metadata_json"),
    [
        ("audit-invalid-json", "{bad-json"),
        ("audit-non-object-json", '["not", "an", "object"]'),
    ],
)
async def test_list_audit_events_ignores_invalid_metadata_json(
    clean_database,
    event_id,
    metadata_json,
):
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            ControlPlaneAuditEventModel(
                event_id=event_id,
                event="mcp.invoke",
                request_id="req-invalid-metadata",
                metadata_json=metadata_json,
            )
        )
        await session.commit()

    events = await list_audit_events(request_id="req-invalid-metadata")

    assert len(events) == 1
    assert events[0].metadata == {}
