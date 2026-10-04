"""Campaign completion must survive a fresh durable read (IP-008)."""

import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.services.content_factory import ContentFactory, FORMAT_ADAPTERS
from app.schemas.content_factory import ContentFormat


@pytest.fixture
async def client(clean_database):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as local_client:
        yield local_client


@pytest.fixture
def api_headers():
    return {"X-API-Key": "test-key"}


@pytest.mark.proof
@pytest.mark.anyio
@pytest.mark.parametrize("render_failure", [False, True])
async def test_completed_campaign_persists_status_and_pipeline_ids(
    client, api_headers, monkeypatch, render_failure
):
    if render_failure:

        async def fail_render(*args):
            raise ValueError("synthetic renderer failure")

        monkeypatch.setitem(FORMAT_ADAPTERS, ContentFormat.TEXT_POST, fail_render)
    created = await client.post(
        "/v1/factory/campaigns",
        headers=api_headers,
        json={
            "campaign_title": "Synthetic persistence probe",
            "source_url": "https://source.example.test/video",
            "hooks": [
                {
                    "title": "Synthetic hook",
                    "hook_type": "educational",
                    "start_seconds": 0,
                    "end_seconds": 10,
                    "target_formats": ["text_post"],
                }
            ],
            "auto_schedule": False,
        },
    )
    assert created.status_code == 202
    result = created.json()
    assert result["status"] == ("failed" if render_failure else "completed")
    assert len(result["pipeline_ids"]) == 1
    fetched = await client.get(
        "/v1/factory/campaigns/" + result["campaign_id"], headers=api_headers
    )
    assert fetched.status_code == 200
    actual = {key: fetched.json()[key] for key in ("status", "pipeline_ids")}
    expected = {key: result[key] for key in ("status", "pipeline_ids")}
    assert actual == expected

    stored = await ContentFactory().store.get_campaign(result["campaign_id"])
    assert stored is not None
    assert stored.status == result["status"]
    assert stored.pipeline_ids == result["pipeline_ids"]
    listing = await client.get("/v1/factory/campaigns", headers=api_headers)
    item = next(
        c
        for c in listing.json()["campaigns"]
        if c["campaign_id"] == result["campaign_id"]
    )
    assert item["status"] == result["status"]
