"""Routes that do not exist while their settings flag is off.

A handler-level check (or a FastAPI dependency) runs only after the route
matched. By then FastAPI has already read the body, so a disabled route still
answers malformed JSON or a schema-invalid body with a 422. That 422 names the
field constraints, and a wrong method gets a 405. Both reveal that the route
exists.

``flag_gated_route`` returns an ``APIRoute`` subclass that reports no match
while the flag is off. The router then falls through exactly as it does for
a path it never had: the same 404 for every method and body. The flag is read
on each request, so tests and local operators can flip it without
re-mounting.
"""

from __future__ import annotations

from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from .config import get_settings


def flag_gated_route(flag: str) -> type[APIRoute]:
    """An ``APIRoute`` class whose routes match only while ``flag`` is true."""

    class FlagGatedRoute(APIRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            if not getattr(get_settings(), flag):
                return Match.NONE, {}
            return super().matches(scope)

    FlagGatedRoute.__name__ = FlagGatedRoute.__qualname__ = f"FlagGatedRoute_{flag}"
    return FlagGatedRoute
