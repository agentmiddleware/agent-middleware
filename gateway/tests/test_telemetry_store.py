"""
Direct tests for the PG-backed EventStore introduced in #28.

test_telemetry.py already covers the HTTP surface. These tests exercise
the store in isolation to verify: round-trip fidelity, filter correctness,
retention eviction, stats aggregation, and tenant (owner) partitioning.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.db.models import TelemetryEventModel
from app.db.database import get_session_factory
from app.core.time import utc_now
from app.schemas.telemetry import (
    Severity,
    TelemetryEvent,
    TelemetryEventType,
)
from app.services.telemetry_pm import (
    ANY_OWNER,
    AnomalyDetector,
    EventStore,
    owner_for_wallet,
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_telemetry_table():
    """Per-test isolation — other tests may have written events."""
    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import delete

        await session.execute(delete(TelemetryEventModel))
        await session.commit()
    yield


def _event(
    event_type: TelemetryEventType = TelemetryEventType.ERROR,
    severity: Severity = Severity.HIGH,
    source: str = "iot-bridge",
    message: str = "boom",
    metadata: dict | None = None,
    timestamp: datetime | None = None,
) -> TelemetryEvent:
    return TelemetryEvent(
        event_type=event_type,
        severity=severity,
        source=source,
        message=message,
        metadata=metadata or {},
        timestamp=timestamp,
    )


@pytest.mark.anyio
async def test_ingest_then_query_round_trip(enforce_naive_utc_datetime_columns):
    """What goes in must come out with fields intact."""
    store = EventStore(retention_hours=168)

    events = [
        _event(message="first", metadata={"k": "v1"}),
        _event(severity=Severity.LOW, message="second", metadata={"k": "v2"}),
    ]
    count, errors = await store.ingest(events, batch_id="b-roundtrip")

    assert count == 2
    assert errors == []

    fetched = await store.query()
    assert len(fetched) == 2
    # Ordered newest-first → second was inserted second, appears first.
    messages = [se.event.message for se in fetched]
    assert set(messages) == {"first", "second"}

    for se in fetched:
        assert se.batch_id == "b-roundtrip"
        assert se.event.metadata.get("k") in {"v1", "v2"}


@pytest.mark.anyio
async def test_query_filters():
    store = EventStore()

    await store.ingest(
        [
            _event(
                event_type=TelemetryEventType.ERROR, severity=Severity.HIGH, source="a"
            ),
            _event(
                event_type=TelemetryEventType.WARNING,
                severity=Severity.MEDIUM,
                source="a",
            ),
            _event(
                event_type=TelemetryEventType.ERROR, severity=Severity.HIGH, source="b"
            ),
        ],
        batch_id="b-filters",
    )

    # by event_type
    errors_only = await store.query(event_type=TelemetryEventType.ERROR)
    assert len(errors_only) == 2
    assert all(se.event.event_type == TelemetryEventType.ERROR for se in errors_only)

    # by severity
    high_only = await store.query(severity=Severity.HIGH)
    assert len(high_only) == 2

    # by source
    a_only = await store.query(source="a")
    assert len(a_only) == 2
    assert all(se.event.source == "a" for se in a_only)

    # combined filter
    errors_from_a = await store.query(event_type=TelemetryEventType.ERROR, source="a")
    assert len(errors_from_a) == 1


@pytest.mark.anyio
async def test_stats_aggregation():
    store = EventStore()

    await store.ingest(
        [
            _event(event_type=TelemetryEventType.ERROR, source="svc-a"),
            _event(event_type=TelemetryEventType.ERROR, source="svc-b"),
            _event(
                event_type=TelemetryEventType.WARNING,
                severity=Severity.MEDIUM,
                source="svc-a",
            ),
        ],
        batch_id="b-stats",
    )

    stats = await store.stats()
    assert stats["total"] == 3
    assert stats["by_type"]["error"] == 2
    assert stats["by_type"]["warning"] == 1
    assert stats["by_source"]["svc-a"] == 2
    assert stats["by_source"]["svc-b"] == 1


@pytest.mark.anyio
async def test_evict_expired_drops_old_rows(enforce_naive_utc_datetime_columns):
    """Anything older than the retention window must be purged."""
    # Retention = 1 hour. We'll manually insert a row with a past ingested_at.
    store = EventStore(retention_hours=1)

    # Ingest a fresh event first so there's a known baseline.
    await store.ingest([_event(message="fresh")], batch_id="b-fresh")

    # Manually insert an expired row (2 hours old).
    factory = get_session_factory()
    two_hours_ago = utc_now() - timedelta(hours=2)
    async with factory() as session:
        session.add(
            TelemetryEventModel(
                event_id="expired-evt",
                batch_id="b-old",
                event_type="error",
                severity="high",
                source="ghost",
                message="old",
                ingested_at=two_hours_ago,
            )
        )
        await session.commit()

    # Sanity: both rows are queryable before eviction.
    pre = await store.query(limit=100)
    assert len(pre) == 2

    removed = await store._evict_expired()
    assert removed == 1

    post = await store.query(limit=100)
    assert len(post) == 1
    assert post[0].event.message == "fresh"


@pytest.mark.anyio
async def test_query_time_window_uses_event_timestamp_when_present(
    enforce_naive_utc_datetime_columns,
):
    """A client-supplied timestamp wins over ingested_at for range queries."""
    store = EventStore()

    now = datetime.now(timezone.utc)
    event_timezone = timezone(timedelta(hours=-7))
    query_timezone = timezone(timedelta(hours=5, minutes=30))
    far_past = (now - timedelta(days=3)).astimezone(event_timezone)
    recent = (now - timedelta(minutes=1)).astimezone(event_timezone)

    await store.ingest(
        [
            _event(message="old-event", timestamp=far_past),
            _event(message="recent-event", timestamp=recent),
        ],
        batch_id="b-window",
    )

    in_last_hour = await store.query(
        since=(now - timedelta(hours=1)).astimezone(query_timezone)
    )
    messages = [se.event.message for se in in_last_hour]
    assert "recent-event" in messages
    assert "old-event" not in messages
    recent_event = next(
        se.event for se in in_last_hour if se.event.message == "recent-event"
    )
    assert recent_event.timestamp is not None
    assert recent_event.timestamp.tzinfo is None
    assert recent_event.timestamp == recent.astimezone(timezone.utc).replace(
        tzinfo=None
    )


# --- Tenant (owner) partitioning ---

OWNER_A = owner_for_wallet("agt-tenant-a")
OWNER_B = owner_for_wallet("agt-tenant-b")


@pytest.mark.anyio
async def test_owner_partitions_are_isolated():
    store = EventStore()
    await store.ingest([_event(source="a1"), _event(source="a2")], "b-a", OWNER_A)
    await store.ingest([_event(source="b1")], "b-b", OWNER_B)
    await store.ingest([_event(source="ops")], "b-admin")

    # A pre-scoping row (bare id, no owner) lands in the unowned partition.
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            TelemetryEventModel(
                event_id="legacy-evt",
                batch_id="b-legacy",
                event_type="error",
                severity="high",
                source="legacy",
                message="old",
            )
        )
        await session.commit()

    a_events = await store.query(owner=OWNER_A)
    assert {se.event.source for se in a_events} == {"a1", "a2"}
    for se in a_events:
        # The owner key fits the 50-char id column and never embeds the
        # wallet id itself.
        assert len(se.event_id) <= 50
        assert "agt-tenant-a" not in se.event_id

    assert {se.event.source for se in await store.query(owner=OWNER_B)} == {"b1"}
    assert {se.event.source for se in await store.query(owner=None)} == {
        "ops",
        "legacy",
    }
    assert len(await store.query()) == 5
    assert len(await store.query(owner=ANY_OWNER)) == 5
    assert await store.query(owner=owner_for_wallet("agt-stranger")) == []

    a_stats = await store.stats(owner=OWNER_A)
    assert a_stats["total"] == 2
    assert a_stats["by_source"] == {"a1": 1, "a2": 1}
    assert (await store.stats(owner=None))["by_source"] == {"ops": 1, "legacy": 1}
    assert (await store.stats())["total"] == 5

    assert await store.owners(event_type=TelemetryEventType.ERROR) == sorted(
        [None, OWNER_A, OWNER_B], key=lambda o: (o is not None, o or "")
    )


class _FakeDurableState:
    """In-process stand-in for the durable state store."""

    enabled = True

    def __init__(self, payload=None):
        self.payload = payload

    async def load_json(self, key):
        return self.payload

    async def save_json(self, key, value):
        self.payload = json.loads(json.dumps(value))
        return True


def _detector(store: EventStore, state: _FakeDurableState) -> AnomalyDetector:
    detector = AnomalyDetector(store)
    detector._state = state  # type: ignore[assignment]
    return detector


@pytest.mark.anyio
async def test_detector_builds_anomalies_per_owner_and_persists_owner():
    store = EventStore()
    # A alone has a concentrated burst; B's errors are below the sample floor
    # and must not dilute (or join) A's anomaly.
    await store.ingest([_event(source="a-svc") for _ in range(12)], "b-a", OWNER_A)
    await store.ingest([_event(source="b-svc") for _ in range(3)], "b-b", OWNER_B)

    state = _FakeDurableState()
    detector = _detector(store, state)
    [report] = await detector.analyze()
    assert report.summary.startswith("12/12 errors")
    assert report.affected_endpoints == ["a-svc"]

    # Owner survives a restart via the durable payload.
    reloaded = _detector(store, state)
    assert await reloaded.get_anomaly(report.anomaly_id, owner=OWNER_A) is not None
    assert await reloaded.get_anomaly(report.anomaly_id, owner=OWNER_B) is None
    assert await reloaded.get_anomaly(report.anomaly_id) is not None
    assert (await reloaded.get_anomalies(owner=OWNER_B))[1] == 0
    assert (await reloaded.get_anomalies(owner=OWNER_A))[1] == 1


@pytest.mark.anyio
async def test_persisted_anomaly_without_owner_is_admin_only():
    """An anomaly persisted before tenant scoping (or with a corrupt owner)
    fails closed: only an unscoped (bootstrap-admin) read sees it."""
    now = utc_now().isoformat()
    record = {
        "anomaly_id": "anom-legacy",
        "severity": "medium",
        "category": "source_concentration",
        "summary": "legacy",
        "affected_endpoints": ["svc"],
        "event_count": 10,
        "first_seen": now,
        "last_seen": now,
    }
    state = _FakeDurableState(
        {"anom-legacy": record, "anom-bad-owner": {**record, "owner": 42}}
    )
    detector = _detector(EventStore(), state)

    for anomaly_id in ("anom-legacy", "anom-bad-owner"):
        assert await detector.get_anomaly(anomaly_id, owner=OWNER_A) is None
        assert await detector.get_anomaly(anomaly_id) is not None
    assert (await detector.get_anomalies(owner=OWNER_A))[1] == 0
    assert (await detector.get_anomalies())[1] == 2
