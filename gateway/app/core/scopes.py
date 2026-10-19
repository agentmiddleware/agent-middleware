"""Scope enforcement decorator for FastAPI routes.

Usage:
    @router.post("/v1/permits")
    @require_scope("billing:charge")
    async def create_permit(...):
        ...
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Awaitable, Callable, Coroutine, ParamSpec, TypeVar

from fastapi import HTTPException, status

from app.core.auth import AuthContext

P = ParamSpec("P")
T = TypeVar("T")


def require_scope(
    *required_scopes: str,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Coroutine[Any, Any, T]]]:
    """Decorator that checks JWT scopes on the auth context.

    API key callers (source="db" or "env") bypass scope checks
    (they have implicit full access). JWT callers must have at least
    one of the required scopes.
    """

    def decorator(
        func: Callable[P, Awaitable[T]],
    ) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Find AuthContext in kwargs
            auth: AuthContext | None = None
            candidate = kwargs.get("auth")
            if isinstance(candidate, AuthContext):
                auth = candidate
            else:
                for arg in args:
                    if isinstance(arg, AuthContext):
                        auth = arg
                        break

            if auth is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "error": "unauthenticated",
                        "message": "Authentication required.",
                    },
                )

            # API key callers have implicit full access. "static-dev" is the
            # local-only static development/training key path; it carries
            # bootstrap-admin power in local-compatible environments only.
            if auth.source in ("db", "env", "static-dev"):
                return await func(*args, **kwargs)

            # JWT callers must have at least one required scope. None means
            # the context never received a scope list (see AuthContext).
            provided_scopes = auth.scopes if auth.scopes is not None else []
            if not any(scope in provided_scopes for scope in required_scopes):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "insufficient_scope",
                        "message": f"Requires one of: {', '.join(required_scopes)}",
                        "required": list(required_scopes),
                        "provided": provided_scopes,
                    },
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
