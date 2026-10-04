"""Fixed disposable database profile for opt-in action proofs; no I/O here."""

import re

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def require_action_database_url(value: str) -> str:
    """Reject every driver target override before a connection can be created."""
    try:
        url = make_url(value)
        accepted = (
            url.drivername == "postgresql+asyncpg"
            and url.username == "sellers"
            and url.password is None
            and url.host == "127.0.0.1"
            and url.port == 55439
            and re.fullmatch(r"amw_action_[a-z0-9_]+", url.database or "") is not None
            and len(url.database or "") <= 63
            and not url.query
            and "?" not in value
        )
    except (ArgumentError, TypeError, ValueError):
        accepted = False
    if not accepted:
        # Never echo a rejected URL: it may contain credentials.
        raise ValueError("unsafe_action_test_database_url")
    return value


def require_action_test_environment(environment: str | None) -> None:
    if environment != "test":
        raise ValueError("unsafe_action_test_environment")
