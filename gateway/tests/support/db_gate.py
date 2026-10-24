"""One shared gate for opt-in real-database tests.

Several test modules only prove their claim against a live database
(PostgreSQL row locks, the asyncpg driver, Alembic round trips). They stay
skipped on a normal run and must never skip quietly in CI: a green suite that
never executed the money-safety proofs certifies nothing.

Convention: every database-gated skip in this suite goes through this module.
The gate is open when the caller names an opt-in that is set
(``flag="NAME"`` needs ``NAME=1``, ``url_env="NAME"`` needs that variable to
hold a URL). When the gate is closed the test skips, unless ``CI_STRICT=1``,
in which case it fails loudly instead. CI sets ``CI_STRICT=1`` in every job
that provisions the databases, plus a prove step asserting zero skips.
"""

from __future__ import annotations

import os

import pytest

#: Set to "1" by CI jobs that provision real databases. Closed gates fail
#: instead of skipping so a misconfigured job goes red, not green.
CI_STRICT_ENV_VAR = "CI_STRICT"


def ci_strict() -> bool:
    """True when skips must fail loudly instead of passing silently."""
    return os.environ.get(CI_STRICT_ENV_VAR) == "1"


def gate_open(*, flag: str | None = None, url_env: str | None = None) -> bool:
    """True when the named opt-in is set (no opt-in named means open)."""
    if flag is not None and os.environ.get(flag) != "1":
        return False
    if url_env is not None and not os.environ.get(url_env, "").strip():
        return False
    return True


def _refuse_quiet_skip(reason: str) -> None:
    if ci_strict():
        pytest.fail(
            f"CI_STRICT=1 but a database-gated test would skip: {reason}",
            pytrace=False,
        )
    pytest.skip(reason)


def require_opt_in(
    *, flag: str | None = None, url_env: str | None = None, reason: str
) -> None:
    """Call inside a test or fixture: skip unless opted in, fail under CI_STRICT."""
    if gate_open(flag=flag, url_env=url_env):
        return
    _refuse_quiet_skip(reason)


def require_live_postgres(*, flag: str | None = None, reason: str) -> None:
    """Skip unless opted in AND the live engine speaks PostgreSQL.

    Both halves fail loudly under CI_STRICT: a job that sets the flag but
    points at SQLite (or vice versa) goes red instead of silently skipping.
    """
    require_opt_in(flag=flag, reason=reason)
    from app.db.database import get_engine

    engine = get_engine()
    if engine is not None and engine.dialect.name == "postgresql":
        return
    _refuse_quiet_skip(reason)


class _ClosedGateCondition:
    """Evaluate the gate when pytest sets up a selected test, not on import."""

    def __init__(self, *, flag: str | None, url_env: str | None, reason: str):
        self.flag = flag
        self.url_env = url_env
        self.reason = reason

    def __bool__(self) -> bool:
        if gate_open(flag=self.flag, url_env=self.url_env):
            return False
        if ci_strict():
            pytest.fail(
                f"CI_STRICT=1 but a database-gated test would skip: {self.reason}",
                pytrace=False,
            )
        return True


def collection_mark(
    *, flag: str | None = None, url_env: str | None = None, reason: str
):
    """Skip or fail a selected gated test during setup.

    Call sites may append the mark unconditionally::

        pytestmark = [
            pytest.mark.asyncio(loop_scope="session"),
            db_gate.collection_mark(flag="RUN_FOO_TESTS", reason="..."),
        ]

    An imported module cannot fail a separate proof's collection. Under
    CI_STRICT, only a selected gated test fails when its gate is closed.
    """
    return pytest.mark.skipif(
        _ClosedGateCondition(flag=flag, url_env=url_env, reason=reason),
        reason=reason,
    )
