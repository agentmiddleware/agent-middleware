"""
Legacy B2A SDK decorators
==================

Developer Experience decorators for agent tools.

- @monitored: Wires function telemetry to the Autonomous PM
- @billable: Gates function execution behind the billing engine
- @mcp_tool: Registers a function as an MCP tool with auto-discovery

Telemetry is scheduled as a background task for non-blocking execution.
"""

import asyncio
import contextvars
import functools
import inspect
import time
import traceback
from collections.abc import Callable, Coroutine
from typing import Any, ParamSpec, TypeVar, get_type_hints

from .client import B2AClient, DryRunSimulation

P = ParamSpec("P")
T = TypeVar("T")

_registration_callbacks: list[Callable] = []

_dry_run_context: contextvars.ContextVar[DryRunSimulation | None] = contextvars.ContextVar(
    "dry_run_context", default=None
)

# Strong references to in-flight telemetry tasks: the event loop keeps only a
# weak one, so an unreferenced task can be garbage-collected before it runs.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Schedule a telemetry coroutine without blocking or failing the caller.

    A sync function under @monitored can run with no event loop, where
    ``asyncio.create_task`` raises RuntimeError *after* the function already
    ran -- discarding its result, or masking its own exception. Telemetry is
    best-effort, so with no running loop the event is dropped instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _error_details(func_name: str, exc: Exception, capture_traceback: bool) -> dict[str, Any]:
    """Error telemetry fields; exception text and traceback only on opt-in.

    Exception messages and source lines in a traceback can carry secrets
    (tokens, PII, request bodies), so by default only the exception type is
    reported.
    """
    if not capture_traceback:
        return {"message": f"Error in {func_name}"}
    return {
        "message": f"Error in {func_name}: {exc}",
        "stack_trace": traceback.format_exc(),
    }


def register_mcp_tool_callback(callback: Callable) -> None:
    """
    Register a callback to be called when @mcp_tool is used.

    This allows the SDK to auto-register tools with the backend.
    """
    _registration_callbacks.append(callback)


def _notify_registration(
    service_id: str, func: Callable, input_schema: dict | None, output_schema: dict | None
) -> None:
    """Notify all registered callbacks of a new MCP tool."""
    for callback in _registration_callbacks:
        try:
            callback(service_id, func, input_schema, output_schema)
        except Exception:
            pass


def _extract_schema_from_func(func: Callable) -> tuple[dict | None, dict | None]:
    """Extract input/output schemas from a function signature."""
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func) if func else {}

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            hint = hints.get(param_name)
            if hint is None:
                properties[param_name] = {"type": "string"}
            elif hasattr(hint, "model_json_schema"):
                properties[param_name] = hint.model_json_schema()
            elif hasattr(hint, "schema"):
                properties[param_name] = hint.schema()
            else:
                type_str = getattr(hint, "__name__", str(hint))
                properties[param_name] = {"type": type_str}

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        input_schema = (
            {
                "type": "object",
                "properties": properties,
                "required": required,
            }
            if properties
            else None
        )

        return_type = hints.get("return")
        output_schema = None
        if return_type and return_type is not type(None):
            if hasattr(return_type, "model_json_schema"):
                output_schema = return_type.model_json_schema()
            elif hasattr(return_type, "schema"):
                output_schema = return_type.schema()

        return input_schema, output_schema

    except Exception:
        return None, None


def monitored(
    client: B2AClient,
    service_name: str,
    capture_args: bool = False,
    *,
    capture_traceback: bool = False,
):
    """
    Instantly wires a function to the Autonomous Product Manager.

    Tracks execution latency and success/failure status, and reports the
    exception type on error. Telemetry is fired in the background to add zero
    latency to execution; a sync function called with no running event loop
    still runs normally, but its telemetry event is dropped.

    Usage:
        b2a = B2AClient(api_key="agt-xyz123")

        @monitored(b2a, service_name="web_scraper")
        async def scrape_website(url: str):
            # Your agent logic here...
            pass

    Args:
        client: B2AClient instance for telemetry submission
        service_name: Name of the service/module (appears in telemetry)
        capture_args: If True, includes function args in metadata
        capture_traceback: If True, error events also carry the exception
            message and the formatted traceback, which can contain secrets.
            Off by default.

    Returns:
        Decorator function
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            start_time = time.time()
            metadata = {}

            if capture_args:
                metadata["args"] = str(args)[:200]
                metadata["kwargs"] = {k: str(v)[:200] for k, v in kwargs.items()}

            try:
                result = await func(*args, **kwargs)

                latency_ms = int((time.time() - start_time) * 1000)
                metadata["latency_ms"] = latency_ms
                metadata["status"] = "success"

                _fire_and_forget(
                    client.telemetry(
                        event_type="api_call",
                        source=service_name,
                        message=f"Successfully executed {func.__name__}",
                        severity="info",
                        function=func.__name__,
                        **metadata,
                    )
                )

                return result

            except Exception as e:
                latency_ms = int((time.time() - start_time) * 1000)

                _fire_and_forget(
                    client.telemetry(
                        event_type="error",
                        source=service_name,
                        severity="high",
                        function=func.__name__,
                        error_type=type(e).__name__,
                        latency_ms=latency_ms,
                        status="failed",
                        **_error_details(func.__name__, e, capture_traceback),
                    )
                )

                raise

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            start_time = time.time()
            metadata = {}

            if capture_args:
                metadata["args"] = str(args)[:200]
                metadata["kwargs"] = {k: str(v)[:200] for k, v in kwargs.items()}

            try:
                result = func(*args, **kwargs)

                latency_ms = int((time.time() - start_time) * 1000)
                metadata["latency_ms"] = latency_ms
                metadata["status"] = "success"

                _fire_and_forget(
                    client.telemetry(
                        event_type="api_call",
                        source=service_name,
                        message=f"Successfully executed {func.__name__}",
                        severity="info",
                        function=func.__name__,
                        **metadata,
                    )
                )

                return result

            except Exception as e:
                latency_ms = int((time.time() - start_time) * 1000)

                _fire_and_forget(
                    client.telemetry(
                        event_type="error",
                        source=service_name,
                        severity="high",
                        function=func.__name__,
                        error_type=type(e).__name__,
                        latency_ms=latency_ms,
                        status="failed",
                        **_error_details(func.__name__, e, capture_traceback),
                    )
                )

                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def billable(
    client: B2AClient,
    wallet_id: str,
    service_category: str,
    units: float = 1.0,
    request_path: str | None = None,
    *,
    idempotency_key_factory: Callable[..., str] | None = None,
):
    """
    Gates function execution behind the Agent Financial Gateway.

    Before executing the function, the SDK attempts to charge the wallet.
    If the wallet has insufficient funds, InsufficientFundsError is raised
    and the function never executes.

    Without ``idempotency_key_factory`` each call is charged independently,
    so the charge is not replay-safe: retrying a call whose charge response
    was lost bills the wallet again. The factory receives the decorated
    function's arguments and must return the same key for every retry of the
    same logical call (derive it from a caller-supplied request or job id; a
    fresh UUID per call protects nothing). The server then replays the
    original charge instead of debiting twice. The decorated function itself
    still runs on every call. Even without a factory the underlying
    ``charge()`` always sends a key (minted fresh for that call) and reuses
    it across its own internal retries; the factory is what makes retries of
    the decorated function itself replay-safe.

    When called within a `simulate_session()` context, the charge is
    simulated without affecting real balance or triggering velocity monitoring.

    Usage:
        b2a = B2AClient(api_key="agt-xyz123")

        @billable(b2a, wallet_id="agt-123", service_category="content_factory", units=5.0)
        async def generate_video(url: str):
            # This only runs if wallet has 5+ credits
            pass

        # Or with simulation:
        async with b2a.simulate_session(wallet_id="agt-123") as sim:
            await generate_video(url)  # Simulated charge
            print(f"Simulated cost: {sim.total_cost}")

    Args:
        client: B2AClient instance for billing
        wallet_id: Wallet to charge
        service_category: Service category for pricing
        units: Number of units to charge (default: 1.0)
        request_path: Optional API path for tracking
        idempotency_key_factory: Optional callable, invoked with the decorated
            function's arguments, returning the charge's ``Idempotency-Key``

    Returns:
        Decorator function

    Raises:
        InsufficientFundsError: If wallet balance is insufficient
        IdempotencyConflictError: If the key was used for a different charge
        ValueError: If the factory returns a blank or overlong key (nothing is
            charged and the function does not run)
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            sim = _dry_run_context.get()

            if sim is not None and sim._active:
                result = await client.simulate_charge(
                    wallet_id=wallet_id,
                    service_category=service_category,
                    units=units,
                    session_id=sim.session_id,
                    description=request_path or f"{func.__module__}.{func.__name__}",
                )
                sim.add_charge_result(result)
                return await func(*args, **kwargs)

            charge_kwargs: dict[str, Any] = {}
            if idempotency_key_factory is not None:
                # Passed only when configured, so a client whose charge()
                # predates the keyword keeps working.
                charge_kwargs["idempotency_key"] = idempotency_key_factory(*args, **kwargs)
            await client.charge(
                wallet_id=wallet_id,
                service_category=service_category,
                units=units,
                request_path=request_path or f"{func.__module__}.{func.__name__}",
                **charge_kwargs,
            )
            return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            raise RuntimeError(
                f"@billable requires an async function. Got sync function: {func.__name__}"
            )

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def combined(
    client: B2AClient,
    wallet_id: str,
    service_category: str,
    service_name: str,
    units: float = 1.0,
    request_path: str | None = None,
    *,
    idempotency_key_factory: Callable[..., str] | None = None,
):
    """
    Combines @monitored and @billable into a single decorator.

    This is the recommended decorator for billable agent tools
    that you want telemetry on.

    Usage:
        @combined(b2a, wallet_id="agt-123", service_category="content_factory",
                  service_name="video_generator", units=5.0)
        async def generate_video(url: str):
            pass

    Args:
        client: B2AClient instance
        wallet_id: Wallet to charge
        service_category: Service category for pricing
        service_name: Name for telemetry
        units: Units to charge
        request_path: Optional API path
        idempotency_key_factory: Optional charge key factory; see @billable

    Returns:
        Decorator function
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        monitored_decorator = monitored(client, service_name)
        billable_decorator = billable(
            client,
            wallet_id,
            service_category,
            units,
            request_path,
            idempotency_key_factory=idempotency_key_factory,
        )

        decorated = billable_decorator(monitored_decorator(func))

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await decorated(*args, **kwargs)

        return wrapper

    return decorator


def mcp_tool(
    service_id: str,
    name: str | None = None,
    description: str | None = None,
    category: str = "custom",
    credits_per_unit: float = 1.0,
    unit_name: str = "call",
):
    """
    Decorator to register a function as an MCP tool.

    When applied, the function is automatically registered with the
    B2A service registry and exposed via MCP.

    Usage:
        @mcp_tool(
            service_id="video-generator",
            name="Video Generator",
            description="Generate a video from a URL",
            category="content_factory",
            credits_per_unit=10.0,
        )
        async def generate_video(url: str, style: str = "cinematic") -> dict:
            # Your implementation here
            return {"video_url": f"https://example.com/{url}.mp4"}

    Args:
        service_id: Unique identifier for the service (used in MCP calls)
        name: Human-readable name (defaults to function name)
        description: Service description (defaults to docstring or "No description")
        category: Service category for pricing
        credits_per_unit: Credits to charge per call
        unit_name: Unit name for pricing display

    Returns:
        Decorator function
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        actual_name = name or func.__name__
        actual_description = description or func.__doc__ or "No description"

        input_schema, output_schema = _extract_schema_from_func(func)

        _notify_registration(
            service_id=service_id,
            func=func,
            input_schema=input_schema,
            output_schema=output_schema,
        )

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await func(*args, **kwargs)

        wrapper._b2a_mcp_metadata = {
            "service_id": service_id,
            "name": actual_name,
            "description": actual_description,
            "category": category,
            "credits_per_unit": credits_per_unit,
            "unit_name": unit_name,
            "input_schema": input_schema,
            "output_schema": output_schema,
        }

        return wrapper

    return decorator
