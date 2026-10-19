"""Static contract for coroutine consumers of scoped async handlers."""

import asyncio

from app.core.auth import AuthContext
from app.core.scopes import require_scope


@require_scope("billing:charge")
async def _handler(auth: AuthContext, count: int) -> int:
    return count + 1


async def create_scoped_task(auth: AuthContext) -> asyncio.Task[int]:
    return asyncio.create_task(_handler(auth=auth, count=1))


def run_scoped(auth: AuthContext) -> int:
    return asyncio.run(_handler(auth=auth, count=1))
