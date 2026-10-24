"""Scoped async handlers remain usable by standard coroutine consumers."""

import pytest
from fastapi import HTTPException

from app.core.auth import AuthContext
from typecheck.scopes_coroutine import create_scoped_task, run_scoped


def _auth(scopes: list[str]) -> AuthContext:
    return AuthContext(source="jwt", raw_key="test", scopes=scopes)


def test_asyncio_run_accepts_scoped_handler():
    assert run_scoped(_auth(["billing:charge"])) == 2


async def test_asyncio_create_task_accepts_scoped_handler():
    task = await create_scoped_task(_auth(["billing:charge"]))
    assert await task == 2


def test_scoped_handler_still_denies_missing_scope():
    with pytest.raises(HTTPException) as exc:
        run_scoped(_auth(["billing:read"]))
    assert exc.value.status_code == 403
