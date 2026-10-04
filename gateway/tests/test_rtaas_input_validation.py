import httpx
import pytest
from fastapi import FastAPI
from unittest.mock import AsyncMock

from app.core.auth import AuthContext, get_auth_context
from app.core.dependencies import get_rtaas_engine
from app.routers.rtaas import CreateJobRequest, router
from app.schemas.red_team import AttackCategory


@pytest.mark.parametrize("category", list(AttackCategory))
def test_declared_attack_categories_remain_valid(category):
    request = CreateJobRequest(
        tenant_id="owner",
        targets=[{"url": "https://example.test"}],
        attack_categories=[category.value],
    )
    assert request.attack_categories == [category]


def test_default_categories_remain_unspecified():
    request = CreateJobRequest(
        tenant_id="owner",
        targets=[{"url": "https://example.test"}],
    )
    assert request.attack_categories is None


@pytest.mark.asyncio
async def test_unknown_category_rejected_before_engine():
    app = FastAPI()
    app.include_router(router)
    engine = AsyncMock()
    app.dependency_overrides[get_rtaas_engine] = lambda: engine
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        source="api_key", raw_key="synthetic", wallet_id="owner", key_id="synthetic"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/rtaas/jobs",
            json={
                "tenant_id": "owner",
                "targets": [{"url": "https://example.test"}],
                "attack_categories": ["not-a-category"],
            },
        )
    assert result.status_code == 422
    engine.create_job.assert_not_awaited()
