"""Governed Pydantic AI middleware over the in-process permit validator.

``governed_tool`` wraps an async function as a client-side stub for a
middleware-registered MCP tool. Each call is screened by the session's
:class:`~b2a_sdk.edge_client.LocalPermitValidator` — a locally-denied call
raises ``b2a_sdk.errors.PermitDeniedError`` without touching the server —
and then dispatched through the governed loop
(``AgentMiddlewareClient.invoke_tool``: permit, caller-owned idempotency
key, signed receipt). The wrapped function's body never runs locally; its
name, signature, and docstring describe the tool to the framework.

Honesty note: the local checks are an optimistic latency mirror. The
governed loop is the path; the server's ``authorize_and_reserve`` remains
authoritative and can still deny a call the local mirror allowed.

This module works with NEITHER framework installed: the generic
``governed_tool`` decorator has no framework dependency, and only
:func:`as_pydantic_ai_tool` imports ``pydantic_ai`` — inside the function,
following the pattern in ``framework_integrations/tools.py``.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from typing import Any, Callable

try:
    from b2a_sdk.edge_client import GovernedEdgeSession
except ImportError as exc:  # pragma: no cover - depends on checkout layout
    raise ImportError(
        "framework_integrations middleware requires the b2a_sdk package. "
        "From a repository checkout run: pip install -e ./sdk/python "
        "(or add b2a_sdk/src to PYTHONPATH)."
    ) from exc

# One shared implementation (see framework_integrations/_governed.py),
# re-exported here so `from framework_integrations.pydantic_ai_middleware
# import governed_tool` keeps working and cannot drift from the LangGraph
# module's copy. Wrappers expose `last_receipt` per task via a contextvar.
from ._governed import GovernedToolWrapper, governed_tool, resolve_tool_name


def as_pydantic_ai_tool(
    session: GovernedEdgeSession,
    func: Callable[..., Any],
    *,
    tool_name: str | None = None,
    credits_hint: Decimal | None = None,
) -> Any:
    """Return a ``pydantic_ai.Tool`` that dispatches through the governed loop.

    ``pydantic_ai`` is imported inside this function so the module (and the
    generic ``governed_tool`` path) loads without the framework installed.
    """
    try:
        from pydantic_ai import Tool
    except ImportError as exc:
        raise ImportError(
            "Pydantic AI not installed. Run: pip install pydantic-ai"
        ) from exc

    name = resolve_tool_name(func, tool_name)
    wrapped = governed_tool(session, tool_name=name, credits_hint=credits_hint)(func)
    # Hand the framework the underlying coroutine function: pydantic_ai
    # detects async tools via inspect.iscoroutinefunction, which a callable
    # wrapper object would defeat.
    target = (
        wrapped.governed_call if isinstance(wrapped, GovernedToolWrapper) else wrapped
    )
    return Tool(
        target,
        name=name,
        description=inspect.getdoc(func) or "",
    )


class PydanticAIGovernedTools:
    """Bind one :class:`GovernedEdgeSession` and mint governed tools from it.

    ``wrap`` is the framework-free decorator (usable directly in tests and
    plain agents); ``as_tool`` additionally packages the wrapper as a
    ``pydantic_ai.Tool`` and therefore needs pydantic_ai installed.
    """

    def __init__(self, session: GovernedEdgeSession) -> None:
        self.session = session

    def wrap(
        self,
        func: Callable[..., Any] | None = None,
        *,
        tool_name: str | None = None,
        credits_hint: Decimal | None = None,
    ) -> Callable[..., Any]:
        decorator = governed_tool(
            self.session, tool_name=tool_name, credits_hint=credits_hint
        )
        return decorator(func) if func is not None else decorator

    def as_tool(
        self,
        func: Callable[..., Any],
        *,
        tool_name: str | None = None,
        credits_hint: Decimal | None = None,
    ) -> Any:
        return as_pydantic_ai_tool(
            self.session, func, tool_name=tool_name, credits_hint=credits_hint
        )


__all__ = ["PydanticAIGovernedTools", "as_pydantic_ai_tool", "governed_tool"]
