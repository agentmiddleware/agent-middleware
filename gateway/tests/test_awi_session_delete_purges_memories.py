"""Issue 355: RAG memories are removed with the AWI session they belong to.

Memory access is authorized through the owning session. After the session
was deleted, its owner got 404 for the memory and had no route left to
delete it, while the bootstrap admin key still read it in full.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.awi_rag_engine import get_awi_rag_engine
from app.services.awi_session import get_awi_session_manager
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

pytestmark = [pytest.mark.proof, pytest.mark.anyio]


@pytest.fixture
async def client():
    http_app = FastAPI()
    http_app.include_router(app.router)
    async with AsyncClient(
        transport=ASGITransport(app=http_app), base_url="http://test"
    ) as client:
        yield client


async def _session_with_memory(client):
    actor = await provision_agent_wallet(client)
    wallet = actor["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet,
        key_id=actor["key_id"],
        tool_name="awi_memory_index",
        max_credits=50,
        idem_key="permit-memory-purge",
    )
    created = await client.post(
        "/v1/awi/sessions",
        json={"target_url": "https://synthetic.invalid", "wallet_id": wallet},
        headers=actor["agent_headers"],
    )
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    indexed = await client.post(
        "/v1/awi/rag/index",
        json={
            "session_id": sid,
            "session_type": "shopping",
            "action_history": [],
            "state_snapshots": [],
        },
        headers={
            **actor["agent_headers"],
            "X-Wallet-Id": wallet,
            "X-Permit-Id": permit["permit_id"],
            "Idempotency-Key": "memory-purge-index",
        },
    )
    assert indexed.status_code == 201, indexed.text
    memory_id = indexed.json()["memory_id"]
    # Precondition: both the owner and the admin can read it while it lives.
    for headers in (actor["agent_headers"], BOOTSTRAP_HEADERS):
        resp = await client.get(f"/v1/awi/rag/memory/{memory_id}", headers=headers)
        assert resp.status_code == 200, resp.text
    return actor, sid, memory_id


async def test_session_delete_purges_its_memories(client, clean_database):
    actor, sid, memory_id = await _session_with_memory(client)

    deleted = await client.delete(
        f"/v1/awi/sessions/{sid}", headers=actor["agent_headers"]
    )
    assert deleted.status_code == 204, deleted.text

    for headers in (actor["agent_headers"], BOOTSTRAP_HEADERS):
        resp = await client.get(f"/v1/awi/rag/memory/{memory_id}", headers=headers)
        assert resp.status_code == 404, resp.text
    rag = get_awi_rag_engine()
    assert await rag.get_memory(memory_id) is None
    assert await rag.get_session_memories(sid) == []


async def test_failed_purge_keeps_the_session(client, clean_database, monkeypatch):
    actor, sid, memory_id = await _session_with_memory(client)
    rag = get_awi_rag_engine()

    async def purge_fails(_session_id):
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(rag, "delete_session_memories", purge_fails)
    with pytest.raises(RuntimeError):
        await get_awi_session_manager().destroy_session(sid)
    monkeypatch.undo()

    # The owner still reaches the memory and can retry the delete.
    resp = await client.get(
        f"/v1/awi/rag/memory/{memory_id}", headers=actor["agent_headers"]
    )
    assert resp.status_code == 200, resp.text
    deleted = await client.delete(
        f"/v1/awi/sessions/{sid}", headers=actor["agent_headers"]
    )
    assert deleted.status_code == 204, deleted.text
    assert await rag.get_memory(memory_id) is None


async def test_expiry_sweep_purges_memories(client, clean_database):
    _, sid, memory_id = await _session_with_memory(client)
    manager = get_awi_session_manager()
    session = await manager.get_session(sid)
    session.updated_at = datetime.now(timezone.utc) - timedelta(
        seconds=session.timeout_seconds + 1
    )

    result = await manager.cleanup_expired_async()
    assert result["sessions_removed"] >= 1
    assert await manager.get_session(sid) is None
    assert await get_awi_rag_engine().get_memory(memory_id) is None
    resp = await client.get(
        f"/v1/awi/rag/memory/{memory_id}", headers=BOOTSTRAP_HEADERS
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("top_k", ["0", "-1", "21", "1000"])
async def test_session_context_rejects_out_of_range_top_k(
    client, clean_database, top_k
):
    actor, sid, _ = await _session_with_memory(client)
    resp = await client.get(
        f"/v1/awi/rag/context/{sid}",
        params={"top_k": top_k},
        headers=actor["agent_headers"],
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("top_k", ["1", "20"])
async def test_session_context_accepts_top_k_bounds(client, clean_database, top_k):
    actor, sid, _ = await _session_with_memory(client)
    resp = await client.get(
        f"/v1/awi/rag/context/{sid}",
        params={"top_k": top_k},
        headers=actor["agent_headers"],
    )
    assert resp.status_code == 200, resp.text
