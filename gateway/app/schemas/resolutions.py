"""Public wire shape for signed receipt resolutions."""

from __future__ import annotations

import json
from typing import Literal, cast

from pydantic import BaseModel

from app.core.config import public_api_origin
from app.db.models import ReceiptResolutionModel
from app.services.signing_keys import RECEIPT_CANONICALIZATION


class ResolutionResponse(BaseModel):
    version: Literal["resolution-v0.1"]
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
