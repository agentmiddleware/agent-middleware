"""Trust-plane facade: signed price quotes.

Re-exports the canonical implementation from :mod:`app.services.quotes`, plus
the shared pricing helpers the quote endpoint and the governed invoke path both
compute prices with.
"""

from __future__ import annotations

from app.services.pricing import charge_units_for, tool_price
from app.services.quotes import (
    QUOTE_REASON_CONSUMED,
    QUOTE_REASON_EXPIRED,
    QUOTE_REASON_NOT_FOUND,
    QUOTE_REASON_TOOL_MISMATCH,
    QUOTE_REASON_WALLET_MISMATCH,
    QUOTE_STATUS_ACTIVE,
    QUOTE_STATUS_CONSUMED,
    QUOTE_STATUS_EXPIRED,
    QuoteError,
    QuoteService,
    QuoteValidation,
    get_quote_service,
    quote_model_to_response,
    quote_ttl_seconds,
)

__all__ = [
    "QUOTE_REASON_CONSUMED",
    "QUOTE_REASON_EXPIRED",
    "QUOTE_REASON_NOT_FOUND",
    "QUOTE_REASON_TOOL_MISMATCH",
    "QUOTE_REASON_WALLET_MISMATCH",
    "QUOTE_STATUS_ACTIVE",
    "QUOTE_STATUS_CONSUMED",
    "QUOTE_STATUS_EXPIRED",
    "QuoteError",
    "QuoteService",
    "QuoteValidation",
    "charge_units_for",
    "get_quote_service",
    "quote_model_to_response",
    "quote_ttl_seconds",
    "tool_price",
]
