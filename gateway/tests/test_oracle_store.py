"""
Direct tests for the PG-backed OracleStore introduced in #29.

test_oracle.py already exercises the HTTP surface (22 tests). These
verify store-level behavior: target lifecycle, indexed-api upsert,
registration fidelity, discovery-hit aggregation, and get_stats.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.database import get_session_factory
from app.db.models import (
    OracleCrawlTargetModel,
    OracleDiscoveryHitModel,
    OracleIndexedAPIModel,
    OracleRegistrationModel,
)
from app.schemas.oracle import (
    CompatibilityTier,
    DirectoryType,
    IndexedAPI,
    IndexedCapability,
    OracleStatus,
    RegistrationResult,
)
from app.services.oracle import OracleStore


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Reset oracle tables between tests — other tests share the DB."""
    factory = get_session_factory()
    async with factory() as session:
        # Hits, registrations, then apis, then targets (no FK cycle here but order is clean).
        await session.execute(delete(OracleDiscoveryHitModel))
        await session.execute(delete(OracleRegistrationModel))
        await session.execute(delete(OracleIndexedAPIModel))
        await session.execute(delete(OracleCrawlTargetModel))
        await session.commit()
    yield


def _indexed(
    api_id: str = "api-1",
    tier: CompatibilityTier = CompatibilityTier.NATIVE,
    directory_type: DirectoryType = DirectoryType.OPENAPI,
    score: float = 0.9,
    name: str = "Test API",
) -> IndexedAPI:
    return IndexedAPI(
        api_id=api_id,
        url="https://api.test.com",
        name=name,
        description="Test description",
        directory_type=directory_type,
        capabilities=[
            IndexedCapability(
                name="search",
                description="full-text search",
                endpoint="/search",
                method="GET",
            )
        ],
        compatibility_tier=tier,
        compatibility_score=score,
        tags=["test", "ai"],
        last_crawled=datetime.now(timezone.utc),
        status=OracleStatus.INDEXED,
    )


@pytest.mark.anyio
async def test_target_lifecycle_store_update_get():
    """store_target inserts, update_target mutates, get_target reflects both."""
    store = OracleStore()
    await store.store_target(
        "t-1",
        {
            "target_id": "t-1",
            "url": "https://api.example.com",
            "directory_type": "openapi",
            "status": OracleStatus.PENDING.value,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    t = await store.get_target("t-1")
    assert t is not None
    assert t["status"] == OracleStatus.PENDING.value
    assert t["api_id"] is None

    await store.update_target(
        "t-1",
        status=OracleStatus.INDEXED.value,
        api_id="api-42",
        crawled_at=datetime.now(timezone.utc),
        raw_payload_hash="abc123",
    )

    t2 = await store.get_target("t-1")
    assert t2["status"] == OracleStatus.INDEXED.value
    assert t2["api_id"] == "api-42"
    assert t2["crawled_at"] is not None
    assert t2["raw_payload_hash"] == "abc123"


@pytest.mark.anyio
async def test_store_indexed_upserts_on_same_api_id():
    """Re-indexing the same API replaces its row, not duplicates it."""
    store = OracleStore()
    await store.store_indexed(_indexed(api_id="api-x", name="First", score=0.5))
    await store.store_indexed(_indexed(api_id="api-x", name="Second", score=0.9))

    row = await store.get_indexed("api-x")
    assert row is not None
    assert row.name == "Second"
    assert row.compatibility_score == 0.9

    all_rows, _ = await store.list_indexed()
    assert len(all_rows) == 1


@pytest.mark.anyio
async def test_list_indexed_filters_and_sorts():
    store = OracleStore()
    await store.store_indexed(
        _indexed(api_id="a", tier=CompatibilityTier.NATIVE, score=0.9)
    )
    await store.store_indexed(
        _indexed(api_id="b", tier=CompatibilityTier.COMPATIBLE, score=0.7)
    )
    await store.store_indexed(
        _indexed(
            api_id="c",
            tier=CompatibilityTier.NATIVE,
            score=0.5,
            directory_type=DirectoryType.MCP_SERVER,
        )
    )

    # Sorted descending by score by default.
    all_rows, _ = await store.list_indexed()
    assert [r.api_id for r in all_rows] == ["a", "b", "c"]

    # Tier filter.
    natives, _ = await store.list_indexed(tier=CompatibilityTier.NATIVE)
    assert {r.api_id for r in natives} == {"a", "c"}

    # Directory-type filter.
    mcps, _ = await store.list_indexed(directory_type=DirectoryType.MCP_SERVER)
    assert [r.api_id for r in mcps] == ["c"]


@pytest.mark.anyio
async def test_list_indexed_respects_pagination():
    store = OracleStore()
    for i in range(5):
        await store.store_indexed(
            _indexed(api_id=f"p{i}", score=0.1 * (i + 1), name=f"API {i}")
        )
    page, total = await store.list_indexed(limit=2, offset=1)
    assert total == 5
    assert len(page) == 2
    assert [a.api_id for a in page] == ["p3", "p2"]


@pytest.mark.anyio
async def test_list_crawl_targets_filters_by_domain():
    store = OracleStore()
    await store.store_target(
        "ct-1",
        {
            "url": "https://api.foo.example.com/v1",
            "directory_type": "openapi",
            "status": "indexed",
        },
    )
    await store.store_target(
        "ct-2",
        {
            "url": "https://other.net/",
            "directory_type": "openapi",
            "status": "failed",
        },
    )
    rows, total = await store.list_crawl_targets(
        domain="foo.example", limit=10, offset=0
    )
    assert total == 1
    assert rows[0]["target_id"] == "ct-1"
    assert rows[0]["domain"] == "api.foo.example.com"


@pytest.mark.anyio
async def test_registration_storage_preserves_fields():
    store = OracleStore()
    await store.store_registration(
        RegistrationResult(
            directory_url="https://agents.dev/register",
            directory_type=DirectoryType.AGENT_REGISTRY,
            status=OracleStatus.REGISTERED,
            registration_id="reg-abc",
            message="ok",
        )
    )
    # Failed registrations (no id) should still persist with a synthesized one.
    await store.store_registration(
        RegistrationResult(
            directory_url="https://broken.example.com/register",
            directory_type=DirectoryType.PLUGIN_STORE,
            status=OracleStatus.FAILED,
            message="timeout",
        )
    )

    regs = await store.get_registrations()
    assert len(regs) == 2
    ids = {r.registration_id for r in regs}
    assert "reg-abc" in ids
    # The failed one synthesized an id instead of leaving it None.
    assert all(rid is not None for rid in ids)


@pytest.mark.anyio
async def test_record_discovery_hit_and_stats():
    store = OracleStore()
    await store.record_discovery_hit("https://hackernews.com")
    await store.record_discovery_hit("https://hackernews.com")
    await store.record_discovery_hit("https://agents.dev")
    await store.record_discovery_hit()  # default "direct"

    stats = await store.get_stats()
    assert stats["discovery_hits"] == 4
    # Top referrer is HN with 2 hits, ordered descending.
    referrers = stats["top_referrers"]
    assert referrers["https://hackernews.com"] == 2
    assert referrers["https://agents.dev"] == 1
    assert referrers["direct"] == 1


@pytest.mark.anyio
async def test_get_stats_counts_across_tables():
    store = OracleStore()
    await store.store_target(
        "t-1",
        {"url": "u", "directory_type": "openapi", "status": "pending"},
    )
    await store.store_indexed(_indexed(api_id="i-1"))
    await store.store_registration(
        RegistrationResult(
            directory_url="u",
            directory_type=DirectoryType.MCP_SERVER,
            status=OracleStatus.REGISTERED,
            registration_id="r-1",
        )
    )
    await store.record_discovery_hit("x")

    stats = await store.get_stats()
    assert stats == {
        "targets_crawled": 1,
        "apis_indexed": 1,
        "registrations": 1,
        "discovery_hits": 1,
        "top_referrers": {"x": 1},
    }


@pytest.mark.anyio
async def test_capabilities_and_tags_round_trip_through_json():
    """capabilities_json + tags_json must deserialize back to the same shape."""
    store = OracleStore()
    api = _indexed(api_id="round-trip")
    await store.store_indexed(api)

    got = await store.get_indexed("round-trip")
    assert got is not None
    assert got.tags == ["test", "ai"]
    assert len(got.capabilities) == 1
    assert got.capabilities[0].name == "search"
    assert got.capabilities[0].endpoint == "/search"
