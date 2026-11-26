"""Tests for the framework tool factories (framework_integrations.tools)."""

import asyncio

import pytest
from framework_integrations.client import B2AClient, B2AConfig
from framework_integrations.tools import (
    get_autogen_tools,
    get_crewai_tools,
    get_langgraph_tools,
    get_llamaindex_tools,
)

try:
    import llama_index  # noqa: F401
except ImportError:
    _HAS_LLAMAINDEX = False
else:
    _HAS_LLAMAINDEX = True

try:
    import pydantic_ai  # noqa: F401
except ImportError:
    _HAS_PYDANTIC_AI = False
else:
    _HAS_PYDANTIC_AI = True


def _client():
    return B2AClient(B2AConfig(api_key="k", wallet_id="w1"))


def test_get_crewai_tools_refuses_with_a_pointer():
    """The async client cannot survive CrewAI's per-call event loops, so the
    factory refuses instead of handing out tools that silently never send."""
    with pytest.raises(NotImplementedError, match="CrewAIB2ATool"):
        get_crewai_tools(_client())


def test_get_autogen_tools_maps_six_operations_to_the_client():
    tools = get_autogen_tools(_client())
    assert set(tools) == {
        "emit_telemetry",
        "get_balance",
        "send_message",
        "ai_decide",
        "self_heal",
        "create_awi_session",
    }
    assert all(asyncio.iscoroutinefunction(fn) for fn in tools.values())


def test_get_langgraph_tools_returns_six_named_async_tools():
    pytest.importorskip("langchain_core")
    tools = get_langgraph_tools(_client())
    assert [t.name for t in tools] == [
        "emit_telemetry",
        "get_balance",
        "send_message",
        "ai_decide",
        "self_heal",
        "awi_session",
    ]


def test_get_llamaindex_tools_needs_the_framework():
    if _HAS_LLAMAINDEX:
        tools = get_llamaindex_tools(_client())
        assert len(tools) == 6
    else:
        with pytest.raises(ImportError, match="LlamaIndex"):
            get_llamaindex_tools(_client())


def test_middleware_wrap_needs_no_framework():
    """The governed wrap path works without langgraph/pydantic-ai installed;
    only the as_tool adapters import their framework."""
    from framework_integrations._governed import governed_tool as generic
    from framework_integrations.langgraph_middleware import LangGraphGovernedTools
    from framework_integrations.pydantic_ai_middleware import PydanticAIGovernedTools

    assert LangGraphGovernedTools.wrap is not None
    assert PydanticAIGovernedTools.wrap is not None
    assert generic is not None

    from framework_integrations.pydantic_ai_middleware import as_pydantic_ai_tool

    if _HAS_PYDANTIC_AI:
        assert callable(as_pydantic_ai_tool)
    else:
        with pytest.raises(ImportError, match="Pydantic AI"):
            as_pydantic_ai_tool(None, None)
