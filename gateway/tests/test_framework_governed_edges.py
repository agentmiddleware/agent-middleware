"""Edge behavior of the shared governed_tool wrapper and its LangGraph adapter.

These tests stub the session and langchain so they do not need a database
or an installed framework.
"""

from __future__ import annotations

import sys
import types
from decimal import Decimal
from typing import Any

import pytest

from framework_integrations._governed import governed_tool


class _Receipt:
    def __init__(self, receipt_id: str) -> None:
        self.receipt_id = receipt_id


class _Result:
    def __init__(self, receipt_id: str, structured: Any, content: Any) -> None:
        self.receipt = _Receipt(receipt_id)
        self.structured_content = structured
        self.content = content


class _Session:
    def __init__(self, result: _Result | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or _Result("r-1", {"ok": True}, [{"text": "nope"}])

    async def invoke(self, name, arguments, *, idempotency_key, estimated_credits=None):
        self.calls.append(
            {
                "name": name,
                "arguments": arguments,
                "idempotency_key": idempotency_key,
                "estimated_credits": estimated_credits,
            }
        )
        return self._result


def _stub_tool(monkeypatch: pytest.MonkeyPatch, recorded: dict[str, Any]) -> None:
    def tool(name_or_fn=None, *args, **kwargs):
        def decorate(fn):
            recorded["name"] = name_or_fn if isinstance(name_or_fn, str) else fn.__name__
            recorded["fn"] = fn
            return fn

        if callable(name_or_fn) and not isinstance(name_or_fn, str):
            return decorate(name_or_fn)
        return decorate

    parts = "langchain_core.tools".split(".")
    for depth in range(1, len(parts) + 1):
        name = ".".join(parts[:depth])
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["langchain_core.tools"].tool = tool


@pytest.mark.anyio
async def test_governed_tool_refuses_unsafe_idempotency_keys_without_calling():
    session = _Session()

    @governed_tool(session, tool_name="echo")
    async def echo(message: str = "ok") -> dict[str, Any]:
        """Stub."""
        raise AssertionError("stub body must never run")

    with pytest.raises(ValueError, match="128"):
        await echo(idempotency_key="k" * 129)
    with pytest.raises(ValueError, match="header"):
        await echo(idempotency_key="key\r\ninjected")
    with pytest.raises(ValueError, match="string"):
        await echo(idempotency_key=12345)  # type: ignore[arg-type]
    assert session.calls == []


@pytest.mark.anyio
async def test_float_credits_hint_reaches_the_session_as_a_decimal():
    session = _Session()

    @governed_tool(session, tool_name="echo", credits_hint=2.5)  # type: ignore[arg-type]
    async def echo(message: str = "ok") -> dict[str, Any]:
        """Stub."""
        raise AssertionError("stub body must never run")

    result = await echo(message="hi", idempotency_key="edge-key")
    assert result == {"ok": True}
    assert session.calls[0]["estimated_credits"] == Decimal("2.5")
    assert isinstance(session.calls[0]["estimated_credits"], Decimal)


def test_blank_tool_name_is_rejected_at_decoration():
    session = _Session()
    with pytest.raises(ValueError, match="tool_name"):

        @governed_tool(session, tool_name="  ")
        async def echo() -> dict[str, Any]:
            """Stub."""
            return {}


@pytest.mark.anyio
async def test_langgraph_adapter_publishes_the_server_tool_name(monkeypatch):
    recorded: dict[str, Any] = {}
    _stub_tool(monkeypatch, recorded)
    from framework_integrations.langgraph_middleware import as_langgraph_tool

    session = _Session()

    async def lookup(query: str) -> dict[str, Any]:
        """Look something up."""
        raise AssertionError("stub body must never run")

    as_langgraph_tool(session, lookup, tool_name="server_lookup")
    assert recorded["name"] == "server_lookup"

    await recorded["fn"](query="caps", idempotency_key="lg-key")
    assert session.calls[0]["name"] == "server_lookup"
    assert session.calls[0]["arguments"] == {"query": "caps"}
