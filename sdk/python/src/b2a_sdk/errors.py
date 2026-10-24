"""Typed SDK errors for authentication and governed tool calls."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any


def _finite_amount(value: Any) -> Decimal | None:
    """Parse a credit amount without accepting booleans or non-finite values."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text == "unknown":
            return None
    elif isinstance(value, (int, float, Decimal)):
        try:
            text = str(value)
        except ValueError:
            return None
    else:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


class AgentMiddlewareError(Exception):
    """Base class for all SDK-specific failures."""


class APIError(AgentMiddlewareError):
    """The API returned an error or an invalid response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail
        self.status_code = status_code
        self.payload = payload or {}
        super().__init__(detail)


class TransportError(AgentMiddlewareError):
    """The request could not be completed at the HTTP transport boundary."""


class AuthenticationError(APIError):
    """The supplied API key was missing or invalid."""


class AuthorizationError(APIError):
    """The authenticated key cannot access the requested resource."""


class PermitDeniedError(AuthorizationError):
    """A governed invocation was denied by its permit."""

    def __init__(
        self,
        reason: str,
        *,
        receipt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.receipt_id = receipt_id
        super().__init__(reason, status_code=403, payload=payload)


class IdempotencyConflictError(APIError):
    """An idempotency key is in progress or was reused for another request."""


class DeliveryUncertainError(APIError):
    """The upstream dispatch occurred, but its outcome cannot be confirmed."""

    def __init__(
        self,
        *,
        receipt_id: str,
        detail: str = "delivery_uncertain",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.receipt_id = receipt_id
        super().__init__(detail, status_code=504, payload=payload)


class InsufficientFundsError(APIError):
    """The wallet does not have enough credits for an operation.

    The positional constructor remains compatible with the provisional 0.3 SDK.
    """

    def __init__(
        self,
        wallet_id: str,
        shortfall: float | str | Decimal | None = None,
        top_up_url: str | None = None,
        *,
        receipt_id: str | None = None,
        payload: dict[str, Any] | None = None,
        shortfall_exact: str | None = None,
    ) -> None:
        self.wallet_id = wallet_id
        self.top_up_url = top_up_url
        self.receipt_id = receipt_id
        exact = _finite_amount(shortfall_exact) if isinstance(shortfall_exact, str) else None
        amount = exact if exact is not None else _finite_amount(shortfall)
        self.shortfall: float | None = None
        self.shortfall_exact: str | None = None
        if amount is not None:
            try:
                approximate = float(amount)
            except (OverflowError, ValueError):
                pass
            else:
                if math.isfinite(approximate):
                    self.shortfall = approximate
            if exact is not None and isinstance(shortfall_exact, str):
                self.shortfall_exact = shortfall_exact.strip()
            elif isinstance(shortfall, str):
                self.shortfall_exact = shortfall.strip()
        shown = self.shortfall_exact if self.shortfall_exact is not None else self.shortfall
        message = f"Insufficient funds in wallet {wallet_id}. Shortfall: {shown} credits."
        if top_up_url:
            message = f"{message} Top up at: {top_up_url}"
        super().__init__(message, status_code=402, payload=payload)


__all__ = [
    "APIError",
    "AgentMiddlewareError",
    "AuthenticationError",
    "AuthorizationError",
    "DeliveryUncertainError",
    "IdempotencyConflictError",
    "InsufficientFundsError",
    "PermitDeniedError",
    "TransportError",
]
