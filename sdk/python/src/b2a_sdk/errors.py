"""Typed SDK errors for authentication and governed tool calls."""

from __future__ import annotations

from typing import Any


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
        shortfall: float | str | None = None,
        top_up_url: str | None = None,
        *,
        receipt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.wallet_id = wallet_id
        self.shortfall = None if shortfall is None or shortfall == "unknown" else float(shortfall)
        self.top_up_url = top_up_url
        self.receipt_id = receipt_id
        message = f"Insufficient funds in wallet {wallet_id}. Shortfall: {self.shortfall} credits."
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
