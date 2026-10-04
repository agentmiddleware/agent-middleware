"""Shared ``governed_tool`` implementation for the framework middleware.

One implementation, imported and re-exported by both
``framework_integrations.langgraph_middleware`` and
``framework_integrations.pydantic_ai_middleware`` so the decorator cannot
drift between frameworks. It wraps an async function as a client-side stub
for a middleware-registered MCP tool: each call is screened by the
session's :class:`~b2a_sdk.edge_client.LocalPermitValidator` — a
locally-denied call raises ``b2a_sdk.errors.PermitDeniedError`` without
touching the server — and then dispatched through the governed loop
(``AgentMiddlewareClient.invoke_tool``: permit, caller-owned idempotency
key, signed receipt). The wrapped function's body never runs locally; its
name, signature, and docstring describe the tool to the framework.

This module has no framework dependency (neither langgraph nor pydantic_ai
is imported), and it is only ever imported through the middleware modules,
which ``framework_integrations/__init__`` loads lazily via PEP 562 — so
importing the package still needs neither b2a_sdk nor a framework.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

try:
    from b2a_sdk.edge_client import GovernedEdgeSession
except ImportError as exc:  # pragma: no cover - depends on checkout layout
    raise ImportError(
        "framework_integrations middleware requires the b2a_sdk package. "
        "From a repository checkout run: pip install -e ./sdk/python "
        "(or add b2a_sdk/src to PYTHONPATH)."
    ) from exc

if TYPE_CHECKING:  # imported for annotations only; no runtime coupling
    from b2a_sdk.models import Receipt


class GovernedToolWrapper:
    """Callable façade over one governed async tool call.

    Carries the wrapped stub's metadata (``functools.update_wrapper``, so
    ``__name__``/``__doc__``/``inspect.signature`` all resolve to the stub)
    and exposes two things a plain function cannot:

    * :attr:`last_receipt` — the typed ``Receipt`` of the most recent
      governed call **made in the current task/context**, read from a
      ``contextvars.ContextVar``. Concurrent tasks each observe their own
      receipt; a shared mutable attribute would let one task read another
      task's receipt.
    * :attr:`governed_call` — the underlying coroutine function, for
      framework adapters that must hand ``inspect.iscoroutinefunction``-
      detectable callables to their tool constructors.
    """

    def __init__(
        self,
        call: Callable[..., Any],
        receipt_var: contextvars.ContextVar[Receipt | None],
    ) -> None:
        self._call = call
        self._receipt_var = receipt_var
        functools.update_wrapper(self, call)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._call(*args, **kwargs)

    @property
    def governed_call(self) -> Callable[..., Any]:
        """The underlying coroutine function driving the governed loop."""
        return self._call

    @property
    def last_receipt(self) -> Receipt | None:
        """Receipt of this task's most recent call through this wrapper.

        Backed by a ``contextvars.ContextVar``, so a task (or plain awaited
        call chain) reads the receipt of its own last invocation — never a
        receipt written concurrently by another task. ``None`` until the
        current context has completed a call.
        """
        return self._receipt_var.get()


def governed_tool(
    session: GovernedEdgeSession,
    *,
    tool_name: str | None = None,
    credits_hint: Decimal | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory: turn an async stub into a governed tool call.

    The wrapped function must be async; a sync function gets a wrapper that
    raises ``RuntimeError`` at call time (matching ``b2a_sdk.decorators
    .billable``'s established behavior). On each call the wrapper:

    1. consumes an ``idempotency_key`` keyword: a supplied non-blank key is
       used as-is (retries that must replay — same receipt, no double
       charge — need a caller-owned key); a supplied blank/whitespace key
       raises ``ValueError`` rather than being silently replaced; when the
       keyword is absent or ``None`` a fresh ``uuid4().hex`` is derived,
       making that call a new invocation;
    2. binds the remaining arguments to the stub's signature **with the
       stub's declared defaults applied**, so an omitted parameter travels
       to the server as the stub's contractual default rather than letting
       the server substitute its own; then runs the session's local permit
       check (``credits_hint`` feeds the budget check), raising
       ``PermitDeniedError`` locally on denial;
    3. invokes the tool through the governed loop and returns the tool
       result — ``structuredContent`` when present, else the MCP content
       list. The typed ``Receipt`` of the call is stored in a per-wrapper
       ``contextvars.ContextVar`` and read back via
       ``wrapper.last_receipt``, which is therefore per-task: concurrent
       tasks each see their own most recent receipt.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        name = tool_name or func.__name__
        signature = inspect.signature(func)
        receipt_var: contextvars.ContextVar[Receipt | None] = contextvars.ContextVar(
            f"governed_tool_last_receipt_{name}", default=None
        )

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if "idempotency_key" in kwargs:
                supplied = kwargs.pop("idempotency_key")
                if supplied is None:
                    idempotency_key = uuid.uuid4().hex
                else:
                    idempotency_key = str(supplied)
                    if not idempotency_key.strip():
                        raise ValueError(
                            "idempotency_key must not be blank; omit it to "
                            "derive a fresh key"
                        )
            else:
                idempotency_key = uuid.uuid4().hex
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            result = await session.invoke(
                name,
                arguments,
                idempotency_key=idempotency_key,
                estimated_credits=credits_hint,
            )
            receipt_var.set(result.receipt)
            if result.structured_content is not None:
                return result.structured_content
            return result.content

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(
                f"@governed_tool requires an async function. "
                f"Got sync function: {func.__name__}"
            )

        if asyncio.iscoroutinefunction(func):
            return GovernedToolWrapper(async_wrapper, receipt_var)
        return sync_wrapper

    return decorator


__all__ = ["GovernedToolWrapper", "governed_tool"]
