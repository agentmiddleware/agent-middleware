"""Typed value objects for the governed trust-plane SDK surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

JsonObject = dict[str, Any]


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"response field {key!r} must be a non-empty string")
    return value


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"response field {key!r} must be a string or null")
    return value


def _datetime(value: Any, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"response field {key!r} must be an ISO-8601 string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: Any, key: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"response field {key!r} must be a decimal") from exc


def _string_list(data: Mapping[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"response field {key!r} must be a string list")
    return list(value)


@dataclass(slots=True)
class ToolDefinition:
    """One executable tool advertised by MCP discovery."""

    name: str
    description: str
    input_schema: JsonObject
    annotations: JsonObject
    raw: JsonObject = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolDefinition:
        input_schema = data.get("inputSchema")
        annotations = data.get("annotations")
        if input_schema is None:
            input_schema = {}
        if annotations is None:
            annotations = {}
        if not isinstance(input_schema, Mapping):
            raise ValueError("response field 'inputSchema' must be an object")
        if not isinstance(annotations, Mapping):
            raise ValueError("response field 'annotations' must be an object")
        return cls(
            name=_required_string(data, "name"),
            description=str(data.get("description") or ""),
            input_schema=dict(input_schema),
            annotations=dict(annotations),
            raw=dict(data),
        )


@dataclass(slots=True)
class PermitRequest:
    """Input for creating a signed, scoped permit."""

    issuer_wallet_id: str
    subject_wallet_id: str
    max_credits: Decimal
    expires_at: datetime
    subject_key_id: str | None = None
    scopes: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    nonce: str | None = None

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "issuer_wallet_id": self.issuer_wallet_id,
            "subject_wallet_id": self.subject_wallet_id,
            "subject_key_id": self.subject_key_id,
            "scopes": list(self.scopes),
            "allowed_tools": list(self.allowed_tools),
            "max_credits": str(self.max_credits),
            "expires_at": self.expires_at.isoformat(),
        }
        if self.nonce is not None:
            payload["nonce"] = self.nonce
        return payload


@dataclass(slots=True)
class Permit:
    """A signed authorization permit returned by the trust plane."""

    permit_id: str
    issuer_wallet_id: str
    subject_wallet_id: str
    subject_key_id: str | None
    scopes: list[str]
    allowed_tools: list[str]
    max_credits: Decimal
    spent_credits: Decimal
    expires_at: datetime
    nonce: str
    status: str
    signature: str
    key_id: str
    issued_at: datetime
    revoked_at: datetime | None
    raw: JsonObject = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Permit:
        revoked_at = data.get("revoked_at")
        return cls(
            permit_id=_required_string(data, "permit_id"),
            issuer_wallet_id=_required_string(data, "issuer_wallet_id"),
            subject_wallet_id=_required_string(data, "subject_wallet_id"),
            subject_key_id=_optional_string(data, "subject_key_id"),
            scopes=_string_list(data, "scopes"),
            allowed_tools=_string_list(data, "allowed_tools"),
            max_credits=_decimal(data.get("max_credits"), "max_credits"),
            spent_credits=_decimal(data.get("spent_credits"), "spent_credits"),
            expires_at=_datetime(data.get("expires_at"), "expires_at"),
            nonce=_required_string(data, "nonce"),
            status=_required_string(data, "status"),
            signature=_required_string(data, "signature"),
            key_id=_required_string(data, "key_id"),
            issued_at=_datetime(data.get("issued_at"), "issued_at"),
            revoked_at=(_datetime(revoked_at, "revoked_at") if revoked_at is not None else None),
            raw=dict(data),
        )


@dataclass(slots=True)
class Receipt:
    """A signed receipt for a governed invocation outcome."""

    receipt_id: str
    idempotency_record_id: str | None
    permit_id: str
    wallet_id: str
    key_id: str | None
    tool: str
    request_hash: str
    response_hash: str | None
    ledger_entry_id: str | None
    dispatch_attempt_id: str | None
    credits_authorized: Decimal
    credits_charged: Decimal
    outcome: str
    audit_event_id: str | None
    created_at: datetime
    signature: str
    signature_key_id: str
    raw: JsonObject = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Receipt:
        return cls(
            receipt_id=_required_string(data, "receipt_id"),
            idempotency_record_id=_optional_string(data, "idempotency_record_id"),
            permit_id=_required_string(data, "permit_id"),
            wallet_id=_required_string(data, "wallet_id"),
            key_id=_optional_string(data, "key_id"),
            tool=_required_string(data, "tool"),
            request_hash=_required_string(data, "request_hash"),
            response_hash=_optional_string(data, "response_hash"),
            ledger_entry_id=_optional_string(data, "ledger_entry_id"),
            dispatch_attempt_id=_optional_string(data, "dispatch_attempt_id"),
            credits_authorized=_decimal(data.get("credits_authorized"), "credits_authorized"),
            credits_charged=_decimal(data.get("credits_charged"), "credits_charged"),
            outcome=_required_string(data, "outcome"),
            audit_event_id=_optional_string(data, "audit_event_id"),
            created_at=_datetime(data.get("created_at"), "created_at"),
            signature=_required_string(data, "signature"),
            signature_key_id=_required_string(data, "signature_key_id"),
            raw=dict(data),
        )


@dataclass(slots=True)
class InvocationResult:
    """Successful MCP result plus its signed receipt."""

    content: list[JsonObject]
    structured_content: JsonObject | None
    receipt: Receipt
    raw: JsonObject = field(repr=False)


@dataclass(slots=True)
class ReceiptVerification:
    """Server-side signature verification result."""

    valid: bool
    reason: str | None
    receipt: Receipt | None
    raw: JsonObject = field(repr=False)


@dataclass(slots=True)
class EvidenceBundle:
    """Buyer-facing receipt, permit, ledger, audit, and verification evidence."""

    receipt_id: str
    valid: bool
    receipt: Receipt
    permit: Permit | None
    ledger_entry: JsonObject | None
    audit_event: JsonObject | None
    verification: dict[str, str]
    dispatch: JsonObject | None
    raw: JsonObject = field(repr=False)


@dataclass(slots=True)
class ACPLineItem:
    """One purchasable line of an ACP checkout, denominated in minor units."""

    name: str
    quantity: int
    unit_amount: int
    currency: str
    # Optional in the ACP payload, so optional in the constructor: a line item
    # without a SKU is constructed by omitting it, not by passing None.
    sku: str | None = None

    def __post_init__(self) -> None:
        """Reject line items the server would refuse, before the SPT is sent.

        Bounds mirror ``app/schemas/acp.py::ACPLineItem`` exactly. Catching
        these here keeps a delegated payment credential off the wire on a
        request that could only 422.
        """
        if not 0 < self.quantity <= 10_000:
            raise ValueError(f"quantity must be in 1..10000, got {self.quantity!r}")
        if not 0 <= self.unit_amount <= 10_000_000:
            raise ValueError(
                f"unit_amount must be in 0..10000000 minor units, got {self.unit_amount!r}"
            )

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "name": self.name,
            "quantity": self.quantity,
            "unit_amount": self.unit_amount,
            "currency": self.currency,
        }
        if self.sku is not None:
            payload["sku"] = self.sku
        return payload


@dataclass(slots=True)
class ACPCheckoutRequest:
    """An ACP checkout to translate into PermitV2 bounds and settle via SPT."""

    intent_id: str
    line_items: list[ACPLineItem]
    # Delegated payment credential: the server masks it and keeps it out of the
    # audit trail. repr=False keeps it out of this dataclass's repr, so it does
    # not surface in logs or tracebacks that render the request object. It is
    # still returned by to_payload() and sent on the wire, so callers must not
    # log that payload.
    spt_token: str = field(repr=False)
    merchant_domain: str
    client_total: int

    def to_payload(self) -> JsonObject:
        return {
            "intent_id": self.intent_id,
            "line_items": [item.to_payload() for item in self.line_items],
            "spt_token": self.spt_token,
            "merchant_domain": self.merchant_domain,
            "client_total": self.client_total,
        }


@dataclass(slots=True)
class ACPCheckoutResponse:
    """The settled checkout, with its trust-plane evidence identifiers."""

    order_id: str
    intent_id: str
    permit_id: str
    receipt_id: str
    audit_event_id: str
    derived_total: str
    status: str
    raw: JsonObject = field(repr=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ACPCheckoutResponse:
        return cls(
            order_id=_required_string(data, "order_id"),
            intent_id=_required_string(data, "intent_id"),
            permit_id=_required_string(data, "permit_id"),
            receipt_id=_required_string(data, "receipt_id"),
            audit_event_id=_required_string(data, "audit_event_id"),
            derived_total=_required_string(data, "derived_total"),
            status=_required_string(data, "status"),
            raw=dict(data),
        )


__all__ = [
    "ACPCheckoutRequest",
    "ACPCheckoutResponse",
    "ACPLineItem",
    "EvidenceBundle",
    "InvocationResult",
    "JsonObject",
    "Permit",
    "PermitRequest",
    "Receipt",
    "ReceiptVerification",
    "ToolDefinition",
]
