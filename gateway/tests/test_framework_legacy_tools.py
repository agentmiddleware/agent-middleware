"""Legacy framework tool factories must await the async B2AClient.

Regression coverage for ``framework_integrations.tools``: the LangGraph and
LlamaIndex factories wrapped plain ``def`` tools around ``async def``
B2AClient methods without ``await``. No HTTP request was ever sent, the tool
handed back a coroutine (or its repr inside a string), and ``awi_session``
crashed with AttributeError on ``coroutine.get``. CrewAI drives every tool
through a synchronous ``invoke`` that runs a coroutine on a fresh event loop
per call, which the shared ``httpx.AsyncClient`` cannot survive between
calls, so ``get_crewai_tools`` now refuses loudly and points at the governed
wrappers instead of returning tools that cannot work.

The framework-free tests stub the one decorator/class each factory imports,
so they run in CI where no agent framework is installed; the importorskip
tests exercise the real frameworks when they are present. Every test routes
the client through ``httpx.MockTransport`` to prove a request actually left.
"""

from __future__ import annotations

import gc
import inspect
import sys
import types
import warnings
from typing import Any

import httpx
import pytest

from framework_integrations import (
    B2AClient,
    get_autogen_tools,
    get_crewai_tools,
    get_langgraph_tools,
    get_llamaindex_tools,
)


WALLET_ID = "wallet-legacy-tools"
API_KEY = "legacy-tools-key"

# tool name -> (tool kwargs, HTTP method, path, text the tool output must hold)
TOOL_CASES: dict[str, tuple[dict[str, Any], str, str, str]] = {
    "emit_telemetry": (
        {"event": "task_completed", "properties": '{"step": 1}'},
        "POST",
        "/v1/telemetry/events",
        "evt-1",
    ),
    "get_balance": (
        {},
        "GET",
        f"/v1/billing/wallets/{WALLET_ID}",
        "Current balance: 42.5 credits",
    ),
    "send_message": (
        {"to_agent": "agent-2", "content": '{"hello": "world"}'},
        "POST",
        "/v1/comms/messages",
        "msg-1",
    ),
    "ai_decide": (
        {"context": '{"goal": "ship"}', "options": '["a", "b"]'},
        "POST",
        "/v1/ai/decide",
        "Decision: b",
    ),
    "self_heal": (
        {"issue": "timeout", "error_log": '{"code": 504}'},
        "POST",
        "/v1/ai/heal",
        "restart",
    ),
    "awi_session": (
        {"target_url": "https://example.com", "max_steps": 5},
        "POST",
        "/v1/awi/sessions",
        "Session created: awi-123",
    ),
}

_RESPONSES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/v1/telemetry/events"): {"event_id": "evt-1"},
    ("GET", f"/v1/billing/wallets/{WALLET_ID}"): {"balance": 42.5},
    ("POST", "/v1/comms/messages"): {"message_id": "msg-1"},
    ("POST", "/v1/ai/decide"): {"decision": "b"},
    ("POST", "/v1/ai/heal"): {"action": "restart"},
    ("POST", "/v1/awi/sessions"): {"session_id": "awi-123"},
}


@pytest.fixture
async def recording():
    """A B2AClient whose every request is recorded and answered in-process."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = _RESPONSES.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={"detail": "unexpected request"})
        return httpx.Response(200, json=body)

    client = B2AClient(api_url="http://b2a.test", api_key=API_KEY, wallet_id=WALLET_ID)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    yield client, sent
    await client.close()


def _stub_module(monkeypatch: pytest.MonkeyPatch, dotted: str, **attrs: Any) -> None:
    """Install an empty package chain for ``dotted`` carrying ``attrs``."""
    parts = dotted.split(".")
    for depth in range(1, len(parts) + 1):
        name = ".".join(parts[:depth])
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for attr, value in attrs.items():
        setattr(sys.modules[dotted], attr, value)


class _StubFunctionTool:
    """Mirror of ``llama_index.core.tools.FunctionTool``'s async dispatch.

    ``acall`` awaits ``async_fn`` when one was given and otherwise calls the
    sync ``fn`` directly, as the real class does (via ``sync_to_async``).
    """

    def __init__(self, fn: Any, async_fn: Any, name: str | None) -> None:
        self.fn = fn
        self.async_fn = async_fn
        self.name = name

    @classmethod
    def from_defaults(
        cls,
        fn: Any = None,
        name: str | None = None,
        async_fn: Any = None,
        **_: Any,
    ) -> "_StubFunctionTool":
        return cls(fn, async_fn, name)

    async def acall(self, **kwargs: Any) -> Any:
        if self.async_fn is not None:
            return await self.async_fn(**kwargs)
        return self.fn(**kwargs)


async def _ainvoke_langchain_style(fn: Any, kwargs: dict[str, Any]) -> Any:
    """Dispatch the way LangChain's ``ainvoke`` does for a bare function.

    A coroutine function is awaited; a plain function is simply called.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return fn(**kwargs)


def _assert_one_request(sent: list[httpx.Request], method: str, path: str) -> None:
    assert [(r.method, r.url.path) for r in sent] == [(method, path)], (
        "the tool must send exactly one request to the API; "
        f"saw {[(r.method, r.url.path) for r in sent]}"
    )
    assert sent[0].headers["X-API-Key"] == API_KEY


def _assert_text_result(result: Any, expected: str) -> None:
    assert not inspect.isawaitable(result), (
        f"tool returned an un-awaited coroutine: {result!r}"
    )
    assert isinstance(result, str), f"tool must return text, got {type(result)!r}"
    assert "coroutine" not in result, f"coroutine repr leaked into output: {result!r}"
    assert expected in result


def _assert_no_unawaited_coroutines(caught: list[warnings.WarningMessage]) -> None:
    gc.collect()
    leaked = [
        str(w.message)
        for w in caught
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert not leaked, f"coroutines were created and dropped: {leaked}"


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(TOOL_CASES))
async def test_langgraph_tools_await_the_client_and_send_a_request(
    monkeypatch, recording, name
):
    _stub_module(monkeypatch, "langchain_core.tools", tool=lambda fn: fn)
    client, sent = recording
    tools = {fn.__name__: fn for fn in get_langgraph_tools(client)}
    assert set(tools) == set(TOOL_CASES)
    kwargs, method, path, expected = TOOL_CASES[name]

    gc.collect()  # attribute stray coroutines from earlier tests to them
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await _ainvoke_langchain_style(tools[name], kwargs)
        _assert_no_unawaited_coroutines(caught)

    _assert_text_result(result, expected)
    _assert_one_request(sent, method, path)


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(TOOL_CASES))
async def test_llamaindex_tools_await_the_client_and_send_a_request(
    monkeypatch, recording, name
):
    _stub_module(monkeypatch, "llama_index.core.tools", FunctionTool=_StubFunctionTool)
    client, sent = recording
    tools = {t.name: t for t in get_llamaindex_tools(client)}
    assert set(tools) == set(TOOL_CASES)
    kwargs, method, path, expected = TOOL_CASES[name]

    gc.collect()  # attribute stray coroutines from earlier tests to them
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await tools[name].acall(**kwargs)
        _assert_no_unawaited_coroutines(caught)

    _assert_text_result(result, expected)
    _assert_one_request(sent, method, path)


@pytest.mark.anyio
async def test_langgraph_tool_rejects_malformed_json_without_sending(
    monkeypatch, recording
):
    """Invalid model-supplied arguments fail before any request is made."""
    _stub_module(monkeypatch, "langchain_core.tools", tool=lambda fn: fn)
    client, sent = recording
    tools = {fn.__name__: fn for fn in get_langgraph_tools(client)}

    with pytest.raises(ValueError):
        await _ainvoke_langchain_style(
            tools["send_message"], {"to_agent": "agent-2", "content": "{not json"}
        )
    assert sent == []


def test_crewai_factory_refuses_and_points_at_governed_wrappers(recording):
    client, sent = recording

    with pytest.raises(NotImplementedError) as excinfo:
        get_crewai_tools(client)

    message = str(excinfo.value)
    assert "integrations/crewai-agent-middleware" in message
    assert "LangGraphGovernedTools" in message
    assert sent == []


async def _unused_async(*args: Any, **kwargs: Any) -> dict[str, str]:
    raise AssertionError("legacy factory must warn before touching the client")


@pytest.mark.parametrize(
    "factory_name",
    ["get_langgraph_tools", "get_llamaindex_tools", "get_autogen_tools"],
)
def test_legacy_factories_warn_they_are_ungoverned(monkeypatch, factory_name):
    """Every ungoverned factory steers buyers to the governed wrappers first."""
    _stub_module(monkeypatch, "langchain_core.tools", tool=lambda fn: fn)
    _stub_module(monkeypatch, "llama_index.core.tools", FunctionTool=_StubFunctionTool)
    client = types.SimpleNamespace(
        emit_telemetry=_unused_async,
        get_balance=_unused_async,
        send_message=_unused_async,
        decide=_unused_async,
        heal=_unused_async,
        create_awi_session=_unused_async,
    )
    factory = {
        "get_langgraph_tools": get_langgraph_tools,
        "get_llamaindex_tools": get_llamaindex_tools,
        "get_autogen_tools": get_autogen_tools,
    }[factory_name]

    with pytest.warns(DeprecationWarning, match="legacy ungoverned"):
        tools = factory(client)

    assert tools, "legacy factory must still return its tools alongside the warning"


# --- Real frameworks, when installed ----------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(TOOL_CASES))
async def test_real_langchain_tools_ainvoke_sends_a_request(recording, name):
    pytest.importorskip("langchain_core.tools")
    client, sent = recording
    tools = {t.name: t for t in get_langgraph_tools(client)}
    kwargs, method, path, expected = TOOL_CASES[name]

    result = await tools[name].ainvoke(kwargs)

    _assert_text_result(result, expected)
    _assert_one_request(sent, method, path)


def test_real_langchain_tools_refuse_sync_invoke(recording):
    """A sync caller gets an error, never a silently dropped coroutine."""
    pytest.importorskip("langchain_core.tools")
    client, sent = recording
    tools = {t.name: t for t in get_langgraph_tools(client)}

    with pytest.raises(NotImplementedError):
        tools["get_balance"].invoke({})
    assert sent == []


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(TOOL_CASES))
async def test_real_llamaindex_tools_acall_sends_a_request(recording, name):
    pytest.importorskip("llama_index.core.tools")
    client, sent = recording
    tools = {t.metadata.get_name(): t for t in get_llamaindex_tools(client)}
    kwargs, method, path, expected = TOOL_CASES[name]

    output = await tools[name].acall(**kwargs)

    _assert_text_result(output.raw_output, expected)
    _assert_one_request(sent, method, path)
