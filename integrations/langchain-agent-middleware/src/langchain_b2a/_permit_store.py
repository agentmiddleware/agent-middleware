"""Durable permit request snapshots, so a restarted process can replay a permit.

The trust plane hashes the whole ``POST /v1/permits`` body under the caller's
permit idempotency key, including ``expires_at``. A process that rebuilds the
body after a restart sends a new expiry under the same key, and the server
answers 409 ``idempotency_key_reused`` instead of replaying the permit.

A :class:`PermitStore` keeps the exact body the first attempt sent. It is
written before the first network call, so a crash between "request sent" and
"response read" still leaves the original body on disk for the next process.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from b2a_sdk.models import PermitRequest

PermitRecord = dict[str, Any]


class PermitStore(Protocol):
    """Persistence for permit request snapshots, keyed by permit idempotency key."""

    def get(self, permit_idempotency_key: str) -> PermitRecord | None: ...

    def put(self, permit_idempotency_key: str, record: PermitRecord) -> None: ...


class JsonFilePermitStore:
    """One JSON file, rewritten atomically on every put.

    Enough for a single agent process that may crash and restart. Several
    processes sharing one permit key need their own database behind
    :class:`PermitStore`.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def _load(self) -> dict[str, PermitRecord]:
        if not self._path.exists():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise TypeError(f"{self._path}: permit store must be a JSON object")
        return data

    def get(self, permit_idempotency_key: str) -> PermitRecord | None:
        record = self._load().get(permit_idempotency_key)
        if record is not None and not isinstance(record, dict):
            raise TypeError(f"{self._path}: permit record must be a JSON object")
        return record

    def put(self, permit_idempotency_key: str, record: PermitRecord) -> None:
        data = self._load()
        data[permit_idempotency_key] = record
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def permit_record(request: PermitRequest, permit_ttl_minutes: object) -> PermitRecord:
    """The JSON snapshot of a permit request, as the SDK puts it on the wire."""
    return {"request": request.to_payload(), "permit_ttl_minutes": permit_ttl_minutes}


def request_from_record(record: PermitRecord) -> PermitRequest:
    """Rebuild the request whose wire body equals the stored snapshot."""
    payload = record.get("request")
    if not isinstance(payload, dict):
        raise TypeError("stored permit record has no request body")
    payload = dict(payload)
    payload["max_credits"] = Decimal(payload["max_credits"])
    payload["expires_at"] = datetime.fromisoformat(payload["expires_at"])
    request = PermitRequest(**payload)
    if request.to_payload() != record["request"]:
        raise ValueError("stored permit record does not round-trip to the same body")
    return request


def same_terms(stored: PermitRequest, fresh: PermitRequest) -> bool:
    """True when two permit bodies differ at most in their expiry."""
    left = stored.to_payload()
    right = fresh.to_payload()
    left.pop("expires_at")
    right.pop("expires_at")
    # Compare amounts by value: a budget of 100 and 100.0 is one permit.
    if Decimal(left.pop("max_credits")) != Decimal(right.pop("max_credits")):
        return False
    return left == right
