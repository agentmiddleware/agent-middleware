"""RAG deletion and query boundary regressions; no external providers."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.auth import AuthContext, get_auth_context
from app.routers.awi_enhanced import router
from app.schemas.awi_enhanced import RAGQueryRequest
from app.services import awi_http_governance
from app.services.awi_rag_engine import AWIRAGEngine


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_count", [1, 2, 3, 8])
async def test_cascade_removes_all_session_memories_and_preserves_other_owners(
    memory_count,
):
    engine = AWIRAGEngine(embedding_model="mock", embedding_dimension=8)
    memory_ids = [
        await engine.index_session(
            "owned-session", "shopping", [], [], owner_wallet_id="owner"
        )
        for _ in range(memory_count)
    ]
    other = await engine.index_session(
        "other-session", "shopping", [], [], owner_wallet_id="other"
    )

    assert await engine.delete_session_memories("owned-session") == memory_count
    assert [await engine.get_memory(memory_id) for memory_id in memory_ids] == [
        None
    ] * memory_count
    assert await engine.get_session_memories("owned-session") == []
    assert await engine.delete_session_memories("owned-session") == 0
    assert await engine.get_memory(other) is not None
    results = await engine.search("shopping", similarity_threshold=0)
    assert [result.memory_id for result in results] == [other]
    stats = await engine.get_stats()
    assert stats["total_memories"] == stats["total_sessions"] == 1
    assert stats["type_counts"] == {"shopping": 1}


@pytest.mark.parametrize("query", ["", " ", "\t\n", "\u2003"])
def test_blank_query_rejected_by_schema(query):
    with pytest.raises(ValidationError):
        RAGQueryRequest(query=query)


@pytest.mark.parametrize("query", ["shopping", "  shopping\n", "\u8cb7\u3044\u7269"])
def test_nonblank_query_preserved_by_schema(query):
    assert RAGQueryRequest(query=query).query == query


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", " ", "\t\n", "\u2003"])
async def test_blank_http_query_rejected_before_governance(monkeypatch, query):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        source="api_key", raw_key="synthetic", wallet_id="owner", key_id="synthetic"
    )
    begin = AsyncMock(side_effect=AssertionError("invalid query reached governance"))
    monkeypatch.setattr(awi_http_governance, "begin_awi_http_governed", begin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post("/v1/awi/rag/query", json={"query": query})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "query"]
    begin.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", " ", "\t\n", "\u2003"])
async def test_blank_engine_query_has_no_embedding_or_access_effect(monkeypatch, query):
    engine = AWIRAGEngine(embedding_model="mock", embedding_dimension=8)
    memory_id = await engine.index_session(
        "owned-session", "shopping", [], [], owner_wallet_id="owner"
    )
    embedding = AsyncMock(side_effect=AssertionError("blank query requested embedding"))
    monkeypatch.setattr(engine, "_generate_embedding", embedding)

    assert await engine.search(query, similarity_threshold=0) == []
    embedding.assert_not_awaited()
    memory = await engine.get_memory(memory_id)
    assert memory is not None and memory.access_count == 0


def test_empty_mock_embedding_is_neutral():
    engine = AWIRAGEngine(embedding_model="mock", embedding_dimension=8)
    embedding = engine._generate_mock_embedding("")
    assert embedding == [0.0] * 8
    assert engine._cosine_similarity(embedding, [1.0] * 8) == 0
