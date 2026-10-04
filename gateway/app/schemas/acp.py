"""Schemas for the ACP (Agentic Commerce Protocol) ingest surface.

Strictly validated request/response models for OpenAI/Stripe Agentic Commerce
Protocol checkouts. Amounts are integers in minor units (cents); the currency
is validated against a fail-closed allowlist (settlement-rails checklist
item 5), and the client-asserted total is advisory only — the adapter derives
the real total from the line items (checklist items 3 and 4).

The ``spt_token`` field is a delegated payment credential. It must never be
logged or persisted: it may not appear in receipts, audit-event metadata,
idempotency payloads, or governance records.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, SecretStr, field_validator

# Fail closed on denomination: only currencies on this allowlist may reach the
# settlement path. Anything else — including uppercase variants — is rejected
# at the schema boundary rather than normalized.
SUPPORTED_ACP_CURRENCIES = frozenset({"usd"})

_INTENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-\.]{1,128}$")
# Lowercase hostname: dot-separated LDH labels, at least two labels, no
# scheme/port/path. Uppercase is rejected rather than folded so the value that
# lands in the permit's recipient_domain is byte-identical to the input.
_HOSTNAME_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_MERCHANT_DOMAIN_PATTERN = re.compile(rf"^{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})+$")

_MAX_SPT_TOKEN_LENGTH = 512


class ACPLineItem(BaseModel):
    """One purchasable line of an ACP checkout, denominated in minor units."""

    name: str = Field(..., min_length=1, max_length=256)
    sku: str | None = Field(default=None, max_length=128)
    quantity: int = Field(..., gt=0, le=10_000)
    # Minor units (cents), never fractional. The upper bound is a sanity cap
    # on a single line; real exposure is capped by the permit budget, which
    # cannot exceed the subject wallet's balance.
    unit_amount: int = Field(..., ge=0, le=10_000_000)
    currency: str

    @field_validator("currency")
    @classmethod
    def _currency_allowlisted(cls, value: str) -> str:
        if value not in SUPPORTED_ACP_CURRENCIES:
            raise ValueError("acp_unsupported_currency")
        return value


class ACPCheckoutRequest(BaseModel):
    """An ACP checkout to translate into PermitV2 bounds and settle via SPT."""

    intent_id: str
    line_items: list[ACPLineItem] = Field(..., min_length=1, max_length=100)
    # Shared Payment Token — see the module docstring: never logged, never
    # persisted. ``SecretStr`` masks it in reprs AND in model_dump /
    # model_dump_json, so an accidental serialization of the request cannot
    # leak the raw credential; only an explicit ``.get_secret_value()`` call
    # (the outbound Stripe charge) ever sees it.
    spt_token: SecretStr
    merchant_domain: str = Field(..., min_length=1, max_length=253)
    # Client-asserted total in minor units. The adapter derives the total
    # server-side and requires exact equality; this value never drives the
    # charged amount.
    client_total: int = Field(..., ge=0)

    @field_validator("intent_id")
    @classmethod
    def _intent_id_shape(cls, value: str) -> str:
        if not _INTENT_ID_PATTERN.fullmatch(value):
            raise ValueError("acp_intent_id_invalid")
        return value

    @field_validator("spt_token")
    @classmethod
    def _spt_token_bounded(cls, value: SecretStr) -> SecretStr:
        # Length bounds live here because pydantic Field length constraints
        # do not apply to the inner value of a SecretStr.
        if not 1 <= len(value.get_secret_value()) <= _MAX_SPT_TOKEN_LENGTH:
            raise ValueError("acp_spt_token_invalid")
        return value

    @field_validator("merchant_domain")
    @classmethod
    def _merchant_domain_shape(cls, value: str) -> str:
        if not _MERCHANT_DOMAIN_PATTERN.fullmatch(value):
            raise ValueError("acp_merchant_domain_invalid")
        return value


class ACPCheckoutResponse(BaseModel):
    """The settled checkout, with its trust-plane evidence identifiers."""

    order_id: str
    intent_id: str
    permit_id: str
    receipt_id: str
    audit_event_id: str
    # Exact decimal string in whole currency units (USD), derived server-side
    # from the line items — never echoed from the client-asserted total.
    derived_total: str
    status: str
