"""Tests for the shared `governed_tool` decorator (framework_integrations)."""

import asyncio
import inspect
from decimal import Decimal
from types import SimpleNamespace

import pytest
from framework_integrations._governed import GovernedToolWrapper, governed_tool

_USE_DEFAULT_STRUCTURED = object()


class FakeSession:
    """Duck-typed GovernedEdgeSession: records calls, returns canned results."""

    def __init__(self, receipt_id="rcpt-1", structured=_USE_DEFAULT_STRUCTURED):
        self.calls = []
        self.receipt_id = receipt_id
        if structured is _USE_DEFAULT_STRUCTURED:
            structured = {"ok": True}
        self.structured = structured

    async def invoke(self, name, arguments, *, idempotency_key, estimated_credits=None):
        await asyncio.sleep(0)
        self.calls.append(
            {
                "name": name,
                "arguments": arguments,
                "idempotency_key": idempotency_key,
                "estimated_credits": estimated_credits,
            }
        )
        tag = arguments.get("query", "")
        return SimpleNamespace(
            content=[{"type": "text", "text": "done"}],
            structured_content=self.structured,
            receipt=SimpleNamespace(receipt_id=f"{self.receipt_id}-{tag}"),
        )


def _tool(session, **kwargs):
    @governed_tool(session, **kwargs)
    async def search(query: str, limit: int = 10) -> dict:
        """Search for things."""
        raise AssertionError("stub body must never run")

    return search


def test_stub_defaults_are_bound_into_arguments():
    """Defaults travel to the server, and the receipt is visible in the
    calling context (contextvars do not leak across asyncio.run calls, so it
    is read inside the task)."""
    session = FakeSession()
    tool = _tool(session)

    async def main():
        result = await tool("q")
        return result, tool.last_receipt

    result, receipt = asyncio.run(main())
    assert result == {"ok": True}
    assert session.calls[0]["arguments"] == {"query": "q", "limit": 10}
    assert receipt.receipt_id == "rcpt-1-q"


def test_content_list_returned_when_no_structured_content():
    session = FakeSession(structured=None)
    tool = _tool(session)
    assert asyncio.run(tool("q")) == [{"type": "text", "text": "done"}]


def test_supplied_key_reused_blank_refused_absent_derived():
    session = FakeSession()
    tool = _tool(session)
    asyncio.run(tool("a", idempotency_key="k1"))
    asyncio.run(tool("b", idempotency_key="k1"))
    assert [c["idempotency_key"] for c in session.calls] == ["k1", "k1"]
    with pytest.raises(ValueError, match="must not be blank"):
        asyncio.run(tool("c", idempotency_key="   "))
    asyncio.run(tool("d", idempotency_key=None))
    derived = session.calls[2]["idempotency_key"]
    assert derived and derived != "k1"


def test_reserved_key_is_not_sent_as_a_tool_argument():
    """A stub that declares its own `idempotency_key` parameter must not leak
    the reserved key into the server arguments."""
    session = FakeSession()

    @governed_tool(session)
    async def write_note(text: str, idempotency_key: str = "tool-default") -> dict:
        """Write a note."""
        raise AssertionError("stub body must never run")

    asyncio.run(write_note("hi", idempotency_key="caller-key"))
    assert session.calls[0]["idempotency_key"] == "caller-key"
    assert session.calls[0]["arguments"] == {"text": "hi"}


def test_blank_tool_name_refused_at_decoration_time():
    session = FakeSession()
    with pytest.raises(ValueError, match="tool_name"):

        @governed_tool(session, tool_name="  ")
        async def something() -> dict:
            """Do something."""
            raise AssertionError("stub body must never run")


def test_tool_name_override_and_credits_hint_forwarded():
    session = FakeSession()
    tool = _tool(session, tool_name="custom.search", credits_hint=Decimal(5))
    asyncio.run(tool("q"))
    assert session.calls[0]["name"] == "custom.search"
    assert session.calls[0]["estimated_credits"] == Decimal(5)


def test_sync_function_raises_at_call_time():
    session = FakeSession()

    @governed_tool(session)
    def sync_tool(query: str) -> dict:
        """Sync stub."""
        return {}

    with pytest.raises(RuntimeError, match="requires an async function"):
        sync_tool("q")
    assert session.calls == []


def test_wrapper_exposes_stub_metadata_and_coroutine():
    session = FakeSession()
    tool = _tool(session)
    assert isinstance(tool, GovernedToolWrapper)
    assert tool.__name__ == "search"
    assert tool.__doc__ == "Search for things."
    assert list(inspect.signature(tool).parameters) == ["query", "limit"]
    assert asyncio.iscoroutinefunction(tool.governed_call)


def test_last_receipt_is_per_task_not_shared():
    """Two concurrent tasks through one wrapper each read their own receipt."""
    session = FakeSession()
    tool = _tool(session)
    seen = {}

    async def one(tag):
        await tool(tag)
        seen[tag] = tool.last_receipt.receipt_id

    async def main():
        await asyncio.gather(one("a"), one("b"))

    asyncio.run(main())
    assert seen == {"a": "rcpt-1-a", "b": "rcpt-1-b"}
