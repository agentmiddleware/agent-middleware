"""Focused unit tests for the AWI RAG engine (in-memory backend only).

No network and no external services: the engine is built with a mock
embedding model so ``_generate_embedding`` never touches an API.
"""

import pytest

from app.services.awi_rag_engine import AWIRAGEngine


def make_engine(dim: int = 8) -> AWIRAGEngine:
    return AWIRAGEngine(embedding_model="mock", embedding_dimension=dim)


ACTIONS = [{"action": "add_to_cart", "parameters": {"product": "Widget"}}]
SNAPSHOTS = [{"summary": "bought a widget"}]


async def test_index_and_get_memory_roundtrip() -> None:
    engine = make_engine()
    memory_id = await engine.index_session(
        "sess-1", "shopping", ACTIONS, SNAPSHOTS, owner_wallet_id="owner"
    )

    memory = await engine.get_memory(memory_id)
    assert memory is not None
    assert memory.session_id == "sess-1"
    assert memory.session_type == "shopping"
    assert memory.action_sequence == ["add_to_cart"]
    assert memory.key_entities == ["Widget"]
    assert memory.owner_wallet_id == "owner"
    assert len(memory.embedding) == 8

    session_memories = await engine.get_session_memories("sess-1")
    assert [m.memory_id for m in session_memories] == [memory_id]


async def test_search_returns_indexed_sessions_and_bumps_access() -> None:
    engine = make_engine()
    memory_id = await engine.index_session("sess-1", "shopping", ACTIONS, SNAPSHOTS)

    results = await engine.search("anything at all", similarity_threshold=0.0)
    assert [r.memory_id for r in results] == [memory_id]
    assert results[0].session_id == "sess-1"
    assert 0.0 <= results[0].similarity_score <= 1.0

    memory = await engine.get_memory(memory_id)
    assert memory is not None and memory.access_count == 1


async def test_search_session_type_filter() -> None:
    engine = make_engine()
    await engine.index_session("shop-sess", "shopping", ACTIONS, SNAPSHOTS)
    await engine.index_session(
        "login-sess",
        "authentication",
        [{"action": "login", "parameters": {}}],
        [{"summary": "signed in"}],
    )

    results = await engine.search(
        "anything", session_type="shopping", similarity_threshold=0.0
    )
    assert [r.session_id for r in results] == ["shop-sess"]

    assert (
        await engine.search(
            "anything", session_type="no-such-type", similarity_threshold=0.0
        )
        == []
    )


async def test_search_owner_scoping() -> None:
    engine = make_engine()
    await engine.index_session(
        "sess-a", "shopping", ACTIONS, SNAPSHOTS, owner_wallet_id="wallet-a"
    )
    await engine.index_session(
        "sess-b", "shopping", ACTIONS, SNAPSHOTS, owner_wallet_id="wallet-b"
    )

    scoped = await engine.search(
        "anything", similarity_threshold=0.0, owner_wallet_ids={"wallet-a"}
    )
    assert [r.session_id for r in scoped] == ["sess-a"]

    unscoped = await engine.search("anything", similarity_threshold=0.0)
    assert {r.session_id for r in unscoped} == {"sess-a", "sess-b"}


async def test_search_by_entities_matches_case_insensitively() -> None:
    engine = make_engine()
    await engine.index_session("sess-1", "shopping", ACTIONS, SNAPSHOTS)

    results = await engine.search_by_entities(["widget"])
    assert [r.session_id for r in results] == ["sess-1"]
    assert results[0].similarity_score == 1.0

    assert await engine.search_by_entities(["something-else"]) == []


async def test_get_similar_sessions_finds_matching_session() -> None:
    engine = make_engine()
    await engine.index_session("sess-1", "shopping", ACTIONS, SNAPSHOTS)
    await engine.index_session("sess-2", "shopping", ACTIONS, SNAPSHOTS)

    similar = await engine.get_similar_sessions("sess-1")
    assert [r.session_id for r in similar] == ["sess-2"]
    assert similar[0].similarity_score == pytest.approx(1.0)


async def test_get_session_context_shape() -> None:
    engine = make_engine()
    await engine.index_session("sess-1", "shopping", ACTIONS, SNAPSHOTS)

    context = await engine.get_session_context(
        "current", {"goal": "shop for widgets", "url": "http://example.com/shop"}
    )
    assert context["current_session_id"] == "current"
    assert context["current_session_type"] == "shopping"
    assert context["query_used"] == "shop for widgets"
    assert set(context) == {
        "current_session_id",
        "current_session_type",
        "relevant_past_sessions",
        "suggested_next_actions",
        "common_patterns",
        "query_used",
    }


async def test_delete_memory_and_stats() -> None:
    engine = make_engine()
    memory_id = await engine.index_session("sess-1", "shopping", ACTIONS, SNAPSHOTS)

    stats = await engine.get_stats()
    assert stats["total_memories"] == 1
    assert stats["total_sessions"] == 1
    assert stats["type_counts"] == {"shopping": 1}
    assert stats["vector_store"] == "in_memory"

    assert await engine.delete_memory(memory_id) is True
    assert await engine.get_memory(memory_id) is None
    assert (await engine.get_stats())["total_memories"] == 0


async def test_blank_query_returns_empty_without_side_effects() -> None:
    engine = make_engine()
    memory_id = await engine.index_session("sess-1", "shopping", ACTIONS, SNAPSHOTS)

    assert await engine.search("   ") == []

    memory = await engine.get_memory(memory_id)
    assert memory is not None and memory.access_count == 0


async def test_missing_memory_lookups() -> None:
    engine = make_engine()
    assert await engine.get_memory("does-not-exist") is None
    assert await engine.get_session_memories("no-such-session") == []
    assert await engine.get_similar_sessions("no-such-session") == []
    assert await engine.delete_memory("does-not-exist") is False
