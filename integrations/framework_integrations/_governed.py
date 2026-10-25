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
import hashlib
import inspect
import json
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

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


def derive_governed_idempotency_key(
    *,
    permit_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    action_id: str | None = None,
) -> str:
    """Derive the idempotency key for one logical governed tool call.

    Key rule: the key is a deterministic ``gov-`` prefixed SHA-256 hash of
    the stable identity of the logical call: the session's permit id, the
    tool name, the canonical (key-sorted JSON) bound arguments, and the
    caller-supplied action id when one is given. Retrying the same logical
    call therefore reuses the same key, and the server replays the original
    receipt instead of charging again. No randomness is used, so a retry
    can never mint a fresh key by accident.

    A different action needs a different key. Arguments alone do not always
    distinguish two actions: pass ``action_id`` (for example the
    framework's tool call id) whenever two calls can carry identical
    arguments but must bill separately. Without ``action_id``, two calls
    with identical permit, tool, and arguments share a key and the second
    replays the first.

    Raises:
        ValueError: if ``permit_id`` or ``tool_name`` is blank, or a
            supplied ``action_id`` is blank.
    """
    if not isinstance(permit_id, str) or not permit_id.strip():
        raise ValueError("permit_id must not be blank")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tool_name must not be blank")
    if action_id is not None and (
        not isinstance(action_id, str) or not action_id.strip()
    ):
        raise ValueError("action_id must not be blank; omit it to derive without one")
    canonical = json.dumps(
        {
            "permit_id": permit_id,
            "tool": tool_name,
            "args": arguments,
            "action_id": action_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"gov-{digest}"


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

    1. consumes an ``idempotency_key`` keyword and an ``action_id``
       keyword (both are reserved: a stub parameter with either name is
       consumed as key material, never sent to the server). A supplied
       non-blank ``idempotency_key`` is used as-is: retries that must
       replay (same receipt, no double charge) pass the same caller-owned
       key. A supplied blank key raises ``ValueError`` rather than being
       silently replaced. When the keyword is absent or ``None`` the key
       is derived deterministically via
       :func:`derive_governed_idempotency_key` from the session's permit
       id, the tool name, the canonical bound arguments, and ``action_id``
       when given, so retrying the same logical tool call reuses the key
       instead of minting a fresh random one. Pass ``action_id`` (for
       example the framework's tool call id) when two distinct actions
       can carry identical arguments but must bill separately;
       a stub that declares an ``idempotency_key`` parameter (keyword or
       positional) has it consumed the same way and never sent to the
       server;
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
        if tool_name is not None and not tool_name.strip():
            raise ValueError("tool_name must not be blank")
        name = tool_name or func.__name__
        signature = inspect.signature(func)
        receipt_var: contextvars.ContextVar[Receipt | None] = contextvars.ContextVar(
            f"governed_tool_last_receipt_{name}", default=None
        )

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            action_id: str | None = None
            if "action_id" in kwargs:
                supplied_action = kwargs.pop("action_id")
                if supplied_action is not None:
                    action_id = str(supplied_action)
                    if not action_id.strip():
                        raise ValueError(
                            "action_id must not be blank; omit it to derive without one"
                        )
            missing = object()
            declared = signature.parameters.get("idempotency_key")
            accepts_keyword = declared is not None and declared.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            supplied = (
                kwargs.get("idempotency_key", missing)
                if accepts_keyword
                else kwargs.pop("idempotency_key", missing)
            )
            bound = signature.bind(*args, **kwargs)
            positional = bound.arguments.get("idempotency_key", missing)
            if (
                supplied is not missing
                and positional is not missing
                and not accepts_keyword
            ):
                raise TypeError("multiple values for idempotency_key")
            if supplied is missing:
                supplied = positional
            idempotency_key: str | None = None
            if supplied is not missing and supplied is not None:
                idempotency_key = str(supplied)
                if not idempotency_key.strip():
                    raise ValueError(
                        "idempotency_key must not be blank; omit it to "
                        "derive a stable key"
                    )
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            # idempotency_key is reserved for the governed loop (consumed
            # above); a stub that declares a parameter of the same name must
            # not leak it into the tool arguments sent to the server.
            arguments.pop("idempotency_key", None)
            if idempotency_key is None:
                idempotency_key = derive_governed_idempotency_key(
                    permit_id=session.permit_id,
                    tool_name=name,
                    arguments=arguments,
                    action_id=action_id,
                )
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


__all__ = ["GovernedToolWrapper", "derive_governed_idempotency_key", "governed_tool"]
