"""Trust-plane facade: idempotent, replay-safe invocation.

Re-exports the canonical idempotency implementation from
:mod:`app.services.idempotency`.
"""

from __future__ import annotations

from app.services.idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH,
    IdempotencyBegin,
    IdempotencyConflictError,
    IdempotencyCreationDisabledError,
    IdempotencyInProgressError,
    IdempotencyReplay,
    IdempotencyService,
    InvalidIdempotencyKeyError,
    decode_idempotency_key_header,
    get_idempotency_service,
    resolve_client_idempotency_key,
    validate_client_idempotency_key,
)

__all__ = [
    "GOVERNED_MCP_IDEMPOTENCY_ENDPOINT",
    "MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH",
    "IdempotencyBegin",
    "IdempotencyConflictError",
    "IdempotencyCreationDisabledError",
    "IdempotencyInProgressError",
    "IdempotencyReplay",
    "IdempotencyService",
    "InvalidIdempotencyKeyError",
    "decode_idempotency_key_header",
    "get_idempotency_service",
    "resolve_client_idempotency_key",
    "validate_client_idempotency_key",
]
