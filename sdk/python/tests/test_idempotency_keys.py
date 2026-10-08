"""Idempotency keys on the legacy (deprecated) SDK paths.

``B2AEdgeClient.call_mcp_tool`` bypasses the governed loop, but it must
still send an ``Idempotency-Key`` so a retry that reuses the key is not a
silent second invocation.
"""

import httpx
import pytest

from b2a_sdk.edge_client import B2AEdgeClient


def _edge_client(handler):
    """Edge client whose HTTP layer is a recording mock transport."""
    edge = B2AEdgeClient(api_url="http://test", api_key="key-1", wallet_id="wallet-1")
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    edge._client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return edge, seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})


@pytest.mark.asyncio
async def test_call_mcp_tool_sends_minted_key_when_omitted():
    """No caller key: the client mints one and sends it as header and rpc id."""
    edge, seen = _edge_client(_ok)
    try:
        await edge.call_mcp_tool("echo", {"message": "hi"})
    finally:
        await edge.close()

    assert len(seen) == 1
    key = seen[0].headers["idempotency-key"]
    assert key.strip() != ""
    assert len(key) <= 128
    import json as jsonlib

    assert jsonlib.loads(seen[0].content)["id"] == key


@pytest.mark.asyncio
async def test_call_mcp_tool_forwards_caller_key():
    """A caller key is forwarded verbatim for header and rpc id."""
    edge, seen = _edge_client(_ok)
    try:
        await edge.call_mcp_tool("echo", {"message": "hi"}, idempotency_key="legacy-key-1")
    finally:
        await edge.close()

    assert seen[0].headers["idempotency-key"] == "legacy-key-1"


@pytest.mark.asyncio
async def test_call_mcp_tool_retry_with_same_key_reuses_it():
    """Two calls with the same caller key send the same key twice."""
    edge, seen = _edge_client(_ok)
    try:
        await edge.call_mcp_tool("echo", {"message": "hi"}, idempotency_key="legacy-key-2")
        await edge.call_mcp_tool("echo", {"message": "hi"}, idempotency_key="legacy-key-2")
    finally:
        await edge.close()

    assert len(seen) == 2
    assert seen[0].headers["idempotency-key"] == "legacy-key-2"
    assert seen[1].headers["idempotency-key"] == "legacy-key-2"


@pytest.mark.asyncio
async def test_call_mcp_tool_distinct_calls_get_distinct_keys():
    """Two auto-keyed calls mint different keys: no accidental replay."""
    edge, seen = _edge_client(_ok)
    try:
        await edge.call_mcp_tool("echo", {"message": "hi"})
        await edge.call_mcp_tool("echo", {"message": "hi"})
    finally:
        await edge.close()

    assert len(seen) == 2
    assert seen[0].headers["idempotency-key"] != seen[1].headers["idempotency-key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", ["", "   ", "k" * 129])
async def test_call_mcp_tool_rejects_bad_key_before_sending(bad_key):
    """A blank or overlong key fails closed locally: nothing is sent."""
    edge, seen = _edge_client(_ok)
    try:
        with pytest.raises(ValueError, match="idempotency_key"):
            await edge.call_mcp_tool("echo", {"message": "hi"}, idempotency_key=bad_key)
    finally:
        await edge.close()

    assert seen == []


class _FakeInvokeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"ok": True}


class _FakeInvokeClient:
    """Stand-in for httpx.AsyncClient recording legacy invoke posts."""

    instances: list["_FakeInvokeClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.posts: list[dict] = []
        _FakeInvokeClient.instances.append(self)

    async def __aenter__(self) -> "_FakeInvokeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url, *, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": dict(headers or {}), "json": json})
        return _FakeInvokeResponse()


@pytest.fixture
def fake_invoke_client(monkeypatch):
    """Route the legacy invoke path through the recording fake client."""
    import b2a_sdk.mcp as mcp_module

    _FakeInvokeClient.instances.clear()
    monkeypatch.setattr(mcp_module.httpx, "AsyncClient", _FakeInvokeClient)
    return _FakeInvokeClient


@pytest.mark.asyncio
async def test_invoke_service_sends_minted_key_when_omitted(fake_invoke_client):
    """No caller key: the legacy path still sends a minted key."""
    from b2a_sdk.mcp import invoke_service

    result = await invoke_service("svc-1", {"a": 1}, "wallet-1", "key-1", api_url="http://test")

    assert result == {"ok": True}
    assert len(fake_invoke_client.instances) == 1
    headers = fake_invoke_client.instances[0].posts[0]["headers"]
    assert headers["Idempotency-Key"].strip() != ""


@pytest.mark.asyncio
async def test_invoke_service_reuses_caller_key(fake_invoke_client):
    """A caller key is forwarded verbatim, so retries can share it."""
    from b2a_sdk.mcp import invoke_service

    await invoke_service(
        "svc-1",
        {"a": 1},
        "wallet-1",
        "key-1",
        api_url="http://test",
        idempotency_key="legacy-invoke-1",
    )
    await invoke_service(
        "svc-1",
        {"a": 1},
        "wallet-1",
        "key-1",
        api_url="http://test",
        idempotency_key="legacy-invoke-1",
    )

    posts = [post for inst in fake_invoke_client.instances for post in inst.posts]
    assert len(posts) == 2
    assert posts[0]["headers"]["Idempotency-Key"] == "legacy-invoke-1"
    assert posts[1]["headers"]["Idempotency-Key"] == "legacy-invoke-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", ["", "   ", "k" * 129])
async def test_invoke_service_rejects_bad_key_before_sending(fake_invoke_client, bad_key):
    """A blank or overlong key fails closed locally: nothing is sent."""
    from b2a_sdk.mcp import invoke_service

    with pytest.raises(ValueError, match="idempotency_key"):
        await invoke_service(
            "svc-1",
            {"a": 1},
            "wallet-1",
            "key-1",
            api_url="http://test",
            idempotency_key=bad_key,
        )

    assert fake_invoke_client.instances == []
