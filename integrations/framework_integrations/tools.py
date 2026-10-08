"""
Framework-Specific Tool Adapters
===============================
Convert B2A tools to framework-specific formats.

Status: these factories are LEGACY and UNGOVERNED. Every tool they return
calls the API directly with no permit check, no idempotency key, and no
signed receipt. New code should use the governed wrappers first:
``CrewAIB2ATool`` from ``integrations/crewai-agent-middleware``,
the AutoGen wrapper from ``integrations/autogen-agent-middleware``,
the LangChain wrapper from ``integrations/langchain-agent-middleware``,
the OpenAI runner from ``integrations/openai-agent-middleware``, or
``LangGraphGovernedTools`` / ``PydanticAIGovernedTools`` in this package.
Each factory below emits a ``DeprecationWarning`` to steer buyers there.
"""

import warnings
from typing import Any, TypeVar

from .client import B2AClient

T = TypeVar("T")

_UNGOVERNED_WARNING = (
    "{name} is a legacy ungoverned factory: its tools call the API with no "
    "permit, no idempotency key, and no signed receipt. Prefer the governed "
    "wrappers (CrewAIB2ATool, the autogen/langchain/openai integrations, or "
    "LangGraphGovernedTools/PydanticAIGovernedTools)."
)


def get_langgraph_tools(client: B2AClient) -> list[Any]:
    """
    LEGACY and UNGOVERNED: prefer the governed wrappers (see module docstring).

    Get LangGraph-compatible tools from B2A client.

    Usage:
    ```python
    from langgraph.prebuilt import create_react_agent
    from framework_integrations import B2AClient, get_langgraph_tools

    client = B2AClient(api_key="...", wallet_id="...")
    tools = get_langgraph_tools(client)

    agent = create_react_agent(model, tools)
    result = await agent.ainvoke({"messages": ["..."]})
    ```

    Returns a list of LangChain BaseTool-compatible objects. The tools are
    async (``B2AClient`` is an async client), so drive them with
    ``ainvoke``; LangChain refuses a sync ``invoke`` of an async-only tool
    rather than returning an un-awaited coroutine.
    """
    warnings.warn(
        _UNGOVERNED_WARNING.format(name="get_langgraph_tools"),
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        from langchain_core.tools import tool
    except ImportError:
        raise ImportError("LangGraph not installed. Run: pip install langgraph")

    @tool
    async def emit_telemetry(event: str, properties: str = "{}") -> str:
        """Emit a telemetry event to track agent activity.

        Args:
            event: Name of the event (e.g., 'task_completed', 'error')
            properties: JSON string of event properties
        """
        import json

        props = json.loads(properties) if properties else {}
        return str(await client.emit_telemetry(event, props))

    @tool
    async def get_balance() -> str:
        """Get the current wallet balance in credits."""
        balance = await client.get_balance()
        return f"Current balance: {balance} credits"

    @tool
    async def send_message(
        to_agent: str, content: str, priority: str = "normal"
    ) -> str:
        """Send a message to another agent.

        Args:
            to_agent: Target agent ID
            content: Message content (JSON string)
            priority: Message priority (normal, high, critical)
        """
        import json

        content_dict = json.loads(content)
        return str(await client.send_message(to_agent, content_dict, priority))

    @tool
    async def ai_decide(context: str, options: str) -> str:
        """Make an AI-powered decision based on context.

        Args:
            context: JSON string with decision context
            options: JSON array of option strings
        """
        import json

        ctx = json.loads(context)
        opts = json.loads(options)
        decision = await client.decide(ctx, opts)
        return f"Decision: {decision}"

    @tool
    async def self_heal(issue: str, error_log: str = "{}") -> str:
        """AI-powered self-healing diagnostics.

        Args:
            issue: Description of the problem
            error_log: JSON string with error context
        """
        import json

        ctx = json.loads(error_log)
        result = await client.heal(issue, ctx)
        return str(result)

    @tool
    async def awi_session(target_url: str, max_steps: int = 100) -> str:
        """Create an AWI session for web automation.

        Args:
            target_url: URL of the website to interact with
            max_steps: Maximum steps for the session
        """
        result = await client.create_awi_session(target_url, max_steps)
        return f"Session created: {result.get('session_id', 'unknown')}"

    return [
        emit_telemetry,
        get_balance,
        send_message,
        ai_decide,
        self_heal,
        awi_session,
    ]


def get_crewai_tools(client: B2AClient) -> list[Any]:
    """
    Not supported: raises ``NotImplementedError``.

    CrewAI executes every tool through a synchronous ``invoke`` that runs an
    async tool body with ``asyncio.run`` -- a fresh event loop per call.
    ``B2AClient`` is an async client whose ``httpx.AsyncClient`` pools
    connections on the loop that opened them, so the call after the first
    fails with "Event loop is closed". The earlier sync tools never awaited
    the client at all (no request was sent; the balance and AWI tools
    returned a coroutine repr or crashed). Rather than hand CrewAI tools that
    cannot work, this factory refuses.

    Use the governed CrewAI wrapper instead (permit -> invoke -> signed
    receipt): ``CrewAIB2ATool`` from ``integrations/crewai-agent-middleware``,
    or ``framework_integrations.LangGraphGovernedTools`` for LangGraph.
    """
    raise NotImplementedError(
        "get_crewai_tools is not supported: CrewAI runs each async tool on a "
        "fresh event loop, which the async B2AClient cannot survive between "
        "calls. Use the governed CrewAIB2ATool from "
        "integrations/crewai-agent-middleware, or "
        "framework_integrations.LangGraphGovernedTools."
    )


def get_autogen_tools(client: B2AClient) -> list[Any]:
    """
    LEGACY and UNGOVERNED: prefer the governed wrappers (see module docstring).

    Get AutoGen-compatible tools from B2A client.

    Usage:
    ```python
    import autogen
    from framework_integrations import B2AClient, get_autogen_tools

    client = B2AClient(api_key="...", wallet_id="...")
    tools = get_autogen_tools(client)

    agent = autogen.AssistantAgent(
        name="agent",
        llm_config=llm_config,
        function_map=tools
    )
    ```

    Returns a dict mapping function names to functions for AutoGen.
    """
    warnings.warn(
        _UNGOVERNED_WARNING.format(name="get_autogen_tools"),
        DeprecationWarning,
        stacklevel=2,
    )
    return {
        "emit_telemetry": client.emit_telemetry,
        "get_balance": client.get_balance,
        "send_message": client.send_message,
        "ai_decide": client.decide,
        "self_heal": client.heal,
        "create_awi_session": client.create_awi_session,
    }


def get_llamaindex_tools(client: B2AClient) -> list[Any]:
    """
    LEGACY and UNGOVERNED: prefer the governed wrappers (see module docstring).

    Get LlamaIndex-compatible tools from B2A client.

    Usage:
    ```python
    from llama_index.core.agent import ReActAgent
    from llama_index.core.tools import FunctionTool
    from framework_integrations import B2AClient, get_llamaindex_tools

    client = B2AClient(api_key="...", wallet_id="...")
    tools = get_llamaindex_tools(client)

    agent = ReActAgent.from_tools(tools, llm=llm)
    response = await agent.achat("...")
    ```

    Returns a list of LlamaIndex FunctionTool objects built from async
    functions (``B2AClient`` is an async client), so ``acall`` awaits the
    request in the caller's event loop.
    """
    warnings.warn(
        _UNGOVERNED_WARNING.format(name="get_llamaindex_tools"),
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        from llama_index.core.tools import FunctionTool
    except ImportError:
        raise ImportError("LlamaIndex not installed. Run: pip install llama-index")

    async def emit_telemetry(event: str, properties: str = "{}") -> str:
        """Emit a telemetry event to track agent activity."""
        import json

        props = json.loads(properties) if properties else {}
        return str(await client.emit_telemetry(event, props))

    async def get_balance() -> str:
        """Get the current wallet balance in credits."""
        balance = await client.get_balance()
        return f"Current balance: {balance} credits"

    async def send_message(
        to_agent: str, content: str, priority: str = "normal"
    ) -> str:
        """Send a message to another agent."""
        import json

        content_dict = json.loads(content)
        return str(await client.send_message(to_agent, content_dict, priority))

    async def ai_decide(context: str, options: str) -> str:
        """Make an AI-powered decision based on context."""
        import json

        ctx = json.loads(context)
        opts = json.loads(options)
        decision = await client.decide(ctx, opts)
        return f"Decision: {decision}"

    async def self_heal(issue: str, error_log: str = "{}") -> str:
        """AI-powered self-healing diagnostics."""
        import json

        ctx = json.loads(error_log)
        result = await client.heal(issue, ctx)
        return str(result)

    async def awi_session(target_url: str, max_steps: int = 100) -> str:
        """Create an AWI session for web automation."""
        result = await client.create_awi_session(target_url, max_steps)
        return f"Session created: {result.get('session_id', 'unknown')}"

    # async_fn, not fn: FunctionTool.acall awaits these in the caller's loop
    # instead of calling a sync wrapper that hands back a coroutine.
    return [
        FunctionTool.from_defaults(async_fn=emit_telemetry, name="emit_telemetry"),
        FunctionTool.from_defaults(async_fn=get_balance, name="get_balance"),
        FunctionTool.from_defaults(async_fn=send_message, name="send_message"),
        FunctionTool.from_defaults(async_fn=ai_decide, name="ai_decide"),
        FunctionTool.from_defaults(async_fn=self_heal, name="self_heal"),
        FunctionTool.from_defaults(async_fn=awi_session, name="awi_session"),
    ]
