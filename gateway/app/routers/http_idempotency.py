"""Opt-in ``Idempotency-Key`` replay protection for state-changing HTTP routes.

Shared by the billing routes and API-key issuance: begin a record on the way
in (replaying a stored response, or 409 on a conflicting reuse), and complete
it with the terminal outcome so a retry with the same key gets the original
result instead of repeating the side effect.

A missing header is still opt-in: the call runs, and a later call is a new
operation. A header that is present but unusable is refused before any side
effect. Dropping it would make the retry look like a call that never asked
for replay protection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, overload

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from ..services.idempotency import (
    IDEMPOTENCY_KEY_HEADER_SOURCE,
    IdempotencyConflictError,
    IdempotencyCreationDisabledError,
    IdempotencyInProgressError,
    IdempotencyService,
    InvalidIdempotencyKeyError,
    decode_idempotency_key_header,
    get_idempotency_service,
    resolve_client_idempotency_key,
)


@dataclass
class IdempotencyGuard:
    """Opt-in replay protection for a state-changing money endpoint.

    Mirrors the flow already used inline by ``/v1/billing/charge``: begin a
    record on the way in (replaying a stored response, or 409-ing on a
    conflicting reuse), and complete it with the terminal outcome so a retry
    with the same ``Idempotency-Key`` gets the original result instead of
    charging/crediting/transferring twice.
    """

    service: IdempotencyService | None
    wallet_id: str
    endpoint: str
    key: str | None

    async def complete(
        self,
        response_json: dict,
        status_code: int,
        *,
        response_reference: str | None = None,
    ) -> None:
        if not self.service or not self.key:
            return
        await self.service.complete(
            wallet_id=self.wallet_id,
            endpoint=self.endpoint,
            idempotency_key=self.key,
            response_reference=response_reference,
            response_json=response_json,
            status_code=status_code,
        )


def resolve_optional_http_idempotency_key(lines: Sequence[str] | None) -> str | None:
    """Return the one key to honor, or None when the caller sent no header.

    Every line the client sent has to be a usable key, and when more than one
    line is present they have to agree. The first line must not win by itself:
    a retry that leads with the other line would be a second operation.
    """
    if not lines:
        return None
    return resolve_client_idempotency_key(
        [
            (IDEMPOTENCY_KEY_HEADER_SOURCE, decode_idempotency_key_header(line))
            for line in lines
        ]
    )


async def begin_http_idempotency(
    *,
    idempotency_key_lines: list[str] | None,
    wallet_id: str,
    endpoint: str,
    request_payload: dict[str, Any],
    compatible_request_payload: dict[str, Any] | None = None,
) -> tuple[IdempotencyGuard, JSONResponse | None]:
    """Start (or replay) an idempotent operation. Returns the guard plus an
    optional replay response to return immediately.

    ``compatible_request_payload`` is an older hash shape that may only be
    read. It is never used to open a new record.
    """
    try:
        idempotency_key = resolve_optional_http_idempotency_key(idempotency_key_lines)
    except InvalidIdempotencyKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(exc), **exc.as_error_data()},
        ) from exc
    if idempotency_key is None:
        return IdempotencyGuard(None, wallet_id, endpoint, None), None
    idem = get_idempotency_service()
    try:
        allow_new_record = True
        # API-key routes historically stored FastAPI's latin-1 header text.
        # Prefer an existing historical identity before opening a UTF-8 slot.
        # Never create a historical slot, even if it disappears after lookup.
        if endpoint in {"/v1/api-keys", "/v1/api-keys/rotate"}:
            for line in idempotency_key_lines or ():
                if (
                    line != idempotency_key
                    and await idem.get_record(
                        wallet_id=wallet_id, endpoint=endpoint, idempotency_key=line
                    )
                    is not None
                ):
                    idempotency_key = line
                    allow_new_record = False
                    break
        replay = await idem.begin(
            wallet_id=wallet_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            allow_new_record=allow_new_record,
            compatible_request_payload=compatible_request_payload,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "idempotency_key_reused", "message": str(exc)},
        ) from exc
    except (IdempotencyInProgressError, IdempotencyCreationDisabledError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "idempotency_in_progress", "message": str(exc)},
        ) from exc
    guard = IdempotencyGuard(idem, wallet_id, endpoint, idempotency_key)
    if replay is not None:
        return guard, JSONResponse(
            status_code=replay.status_code, content=replay.response_json
        )
    return guard, None


@overload
def resolve_idempotency_key_lines(
    lines: Sequence[str] | None, *, required: Literal[True]
) -> str: ...
@overload
def resolve_idempotency_key_lines(
    lines: Sequence[str] | None, *, required: Literal[False] = ...
) -> str | None: ...
def resolve_idempotency_key_lines(
    lines: Sequence[str] | None,
    *,
    required: bool = False,
) -> str | None:
    """Return the one Idempotency-Key named by every header line.

    Callers pass ``list[str]`` from Header so a second line is visible. A
    single-value header parameter keeps only the first line, and a later
    call that sends only the dropped line is a second charge, transfer, or
    permit. Identical lines collapse. Differing lines, a blank line, a
    control character, or a key longer than the store column are refused
    here, before a replay slot is opened. No lines means the caller did not
    send the header. ``required`` turns that absence into the same 400.
    FastAPI still answers 422 when a required Header parameter is omitted.
    """
    try:
        key = resolve_optional_http_idempotency_key(lines)
    except InvalidIdempotencyKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": str(exc), **exc.as_error_data()},
        ) from exc
    if required and key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": (
                    "invalid_idempotency_key: Idempotency-Key header must "
                    "contain a non-whitespace character"
                ),
                "error": "invalid_idempotency_key",
                "reason_code": "idempotency_key_blank",
                "source": IDEMPOTENCY_KEY_HEADER_SOURCE,
            },
        )
    return key
