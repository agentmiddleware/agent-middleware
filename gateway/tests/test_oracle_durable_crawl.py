"""Contract: Oracle durable crawl row + domain index (Phase 1)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import OracleCrawlTargetModel
from app.main import app

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _restore_oracle_simulation():
    settings = get_settings()
    saved = settings.SIMULATION_MODE_ORACLE
    yield
    settings.SIMULATION_MODE_ORACLE = saved


@pytest.mark.anyio
async def test_durable_crawl_writes_hash_and_index_lists_targets():
    settings = get_settings()
    settings.SIMULATION_MODE_ORACLE = False
    url = "https://phase1-durable.example/api"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/oracle/crawl",
            json={"url": url, "directory_type": "openapi"},
            headers=HEADERS,
        )
        assert r.status_code == 202

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(OracleCrawlTargetModel).where(OracleCrawlTargetModel.url == url)
        )
        row = result.scalar_one_or_none()
    assert row is not None
    assert row.status == "indexed"
    assert row.raw_payload_hash is not None
    assert len(row.raw_payload_hash) == 64

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        idx = await client.get(
            "/v1/oracle/index",
            params={"domain": "phase1-durable.example"},
            headers=HEADERS,
        )
    assert idx.status_code == 200
    body = idx.json()
    assert body["total"] >= 1
    assert len(body["crawl_targets"]) >= 1
    match = next(t for t in body["crawl_targets"] if t["url"] == url)
    assert match["raw_payload_hash"] == row.raw_payload_hash
    assert match["domain"] == "phase1-durable.example"
    assert match["status"] == "indexed"


@pytest.mark.anyio
async def test_simulation_mode_oracle_skips_payload_hash():
    settings = get_settings()
    settings.SIMULATION_MODE_ORACLE = True
    url = "https://phase1-sim.example/api"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/v1/oracle/crawl",
            json={"url": url, "directory_type": "openapi"},
            headers=HEADERS,
        )
        assert r.status_code == 202
        assert r.json()["status"] == "indexed"

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(OracleCrawlTargetModel).where(OracleCrawlTargetModel.url == url)
        )
        row = result.scalar_one_or_none()
    assert row is not None
    assert row.raw_payload_hash is None
