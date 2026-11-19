"""Public wire shape for signed receipt resolutions."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import public_api_origin
from app.db.models import ReceiptResolutionModel
from app.services.signing_keys import RECEIPT_CANONICALIZATION


class ResolutionResponse(BaseModel):
    version: Literal["resolution-v0.1", "resolution-v0.2"]
    evidence: dict[str, Any] | None = None
    evidence_hash: str | None = None
    issuer: str
    resolution_id: str
    receipt_id: str
    wallet_id: str
    sequence: int
    prev_hash: str | None
    receipt_hash: str
    outcome: Literal["confirmed_success", "confirmed_not_delivered"]
    created_at: str
    payload_hash: str
    resolution_hash: str
    signing_input: str
    signature: str
    alg: Literal["Ed25519"] = "Ed25519"
    kid: str
    canonicalization: str = RECEIPT_CANONICALIZATION
    keys_url: str = "/.well-known/trust-keys.json"


class ResolutionListResponse(BaseModel):
    records: list[ResolutionResponse]
    total: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


def resolution_to_response(model: ReceiptResolutionModel) -> ResolutionResponse:
    signed = json.loads(model.signing_input)
    return ResolutionResponse(
        version=signed["version"],
        issuer=public_api_origin(),
        evidence=json.loads(model.evidence) if model.evidence else None,
        evidence_hash=model.evidence_hash,
        resolution_id=model.resolution_id,
        receipt_id=model.receipt_id,
        wallet_id=model.wallet_id,
        sequence=model.sequence,
        prev_hash=model.prev_hash,
        receipt_hash=model.receipt_hash,
        outcome=cast(
            Literal["confirmed_success", "confirmed_not_delivered"], model.outcome
        ),
        created_at=signed["created_at"],
        payload_hash=model.payload_hash,
        resolution_hash=model.resolution_hash,
        signing_input=model.signing_input,
        signature=model.signature,
        kid=model.signature_key_id,
    )


class ResolutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=256)
    upstream_reference_ids: list[str] = Field(default_factory=list, max_length=64)
    observed_at: datetime
    note: str = Field(min_length=1, max_length=4096)
    raw_lookup_answer: dict[str, Any] | None = None

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at requires a timezone")
        return value


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["confirmed_success", "confirmed_not_delivered"]
    evidence: ResolutionEvidence
    use_lookup: bool = False
    refund_credits: bool = False
