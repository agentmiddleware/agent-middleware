from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import AuthContext, get_auth_context
from app.core.time import utc_now
from app.services.x402_engine import (
    X402Error,
    X402SettlementUncertainError,
    get_x402_handler,
)
from app.trust import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    PermitError,
    get_idempotency_service,
    get_receipt_service,
)

router = APIRouter(prefix="/v1/x402", tags=["X402 Settlement"])

_SETTLE_ENDPOINT = "/v1/x402/settle"

# Age changes the conflict reported to the caller; it never proves that a
# worker stopped or that a committed permit reservation was compensated.
_SETTLE_STALE_SECONDS = 300


# Response/request models live in the router module: the x402 surface is small
# and dormant, so it keeps its schemas local rather than growing app/schemas.


class X402ParseRequest(BaseModel):
    """An upstream HTTP response's status and headers, as observed."""

    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)


class X402RequirementResponse(BaseModel):
    amount_usd: str
    pay_to: str
    network: str
    asset: str


class X402SettleRequest(BaseModel):
    permit_id: str
    wallet_id: str
    amount: Decimal
    pay_to: str
    network: str
    asset: str = "USDC"
    # Payer wallet's on-chain address. Required for EVM networks: the
    # facilitator attestation signs the exact EIP-712 message, so `from`
    # must be the real payer, not a blank the wallet fills in afterwards.
    payer: str | None = None


class X402AttestationResponse(BaseModel):
    """Ed25519 facilitator attestation — trust-plane evidence, not on-chain."""

    alg: str
    signature: str
    key_id: str
    payload_hash: str


class X402SettleResponse(BaseModel):
    permit_id: str
    wallet_id: str
    network: str
    pay_to: str
    asset: str
    amount_usd: str
    # Exact decimal string; the permit budget was reserved for this amount.
    credits: str
    # What the payer wallet must sign (EIP-712 typed data or Solana message).
    authorization: dict[str, Any]
    attestation: X402AttestationResponse
    receipt_id: str
    shadow_session_id: str
    shadow_charge_id: str | None = None
    audit_event_id: str | None = None


@router.post("/parse", response_model=X402RequirementResponse)
async def parse_payment_required(
    request: X402ParseRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> X402RequirementResponse:
    """Strictly parse an observed HTTP 402 into a payment requirement."""
    del auth  # authenticated-only surface; parsing itself is tenant-neutral
    try:
        requirement = get_x402_handler().parse_402(request.status_code, request.headers)
    except X402Error as exc:
        raise HTTPException(status_code=400, detail=exc.reason)
    return X402RequirementResponse(
        amount_usd=str(requirement.amount_usd),
        pay_to=requirement.pay_to,
        network=requirement.network,
        asset=requirement.asset,
    )


async def _refuse_in_progress_settle(
    request: X402SettleRequest,
    *,
    idempotency_key: str,
    in_progress: IdempotencyInProgressError,
) -> NoReturn:
    """Preserve incomplete owners until their settlement can be reconciled.

    Permit budget is committed before receipt creation. Without a durable
    reservation checkpoint, a missing receipt cannot prove that the first
    attempt was side-effect-free or fully compensated. A stale owner may also
    belong to a live worker. Never abandon either shape for automatic retry.

    A persisted receipt proves settlement, but the exact response cannot be
    reconstructed from its payload hashes; retain that distinct conflict.
    """
    record = await get_idempotency_service().get_record(
        wallet_id=request.wallet_id,
        endpoint=_SETTLE_ENDPOINT,
        idempotency_key=idempotency_key,
    )
    stale = (
        record is not None
        and record.response_json is None
        and not record.ledger_entry_id
        and record.created_at < utc_now() - timedelta(seconds=_SETTLE_STALE_SECONDS)
    )
    if not stale:
        raise HTTPException(status_code=409, detail=in_progress.args[0])
    assert record is not None
    receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
        record.record_id
    )
    if receipt is not None:
        raise HTTPException(status_code=409, detail="x402_settled_unrecoverable_replay")
    raise HTTPException(status_code=409, detail="x402_settlement_needs_review")


@router.post("/settle", response_model=X402SettleResponse)
async def settle_payment_required(
    request: X402SettleRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    auth: AuthContext = Depends(get_auth_context),
) -> X402SettleResponse:
    """Authorize a 402 demand against a permit and record the settlement.

    Facilitation only: budget is reserved on the permit, the settlement is
    metered in the shadow ledger, and a signed receipt is emitted — no real
    ledger entry is written and no credits are minted (settlement freeze,
    docs/settlement-rails.md).
    """
    handler = get_x402_handler()
    try:
        requirement = handler.build_requirement(
            amount=request.amount,
            pay_to=request.pay_to,
            network=request.network,
            asset=request.asset,
        )
    except X402Error as exc:
        raise HTTPException(status_code=400, detail=exc.reason)

    auth.require_wallet_access(request.wallet_id)

    idem = get_idempotency_service()
    try:
        begun = await idem.begin_with_record(
            wallet_id=request.wallet_id,
            endpoint=_SETTLE_ENDPOINT,
            idempotency_key=idempotency_key,
            request_payload=request.model_dump(mode="json"),
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.args[0])
    except IdempotencyInProgressError as exc:
        await _refuse_in_progress_settle(
            request,
            idempotency_key=idempotency_key,
            in_progress=exc,
        )
    if begun.replay and begun.replay.response_json:
        return X402SettleResponse(**begun.replay.response_json)

    try:
        settlement = await handler.settle(
            permit_id=request.permit_id,
            wallet_id=request.wallet_id,
            key_id=auth.key_id,
            requirement=requirement,
            idempotency_key=idempotency_key,
            payer=request.payer,
            idempotency_record_id=begun.record_id,
        )
    except X402SettlementUncertainError as exc:
        raise HTTPException(status_code=409, detail=exc.reason)
    except X402Error as exc:
        # Only confirmed pre-reservation or fully compensated failures release
        # this owner. Uncertain settlement/compensation keeps it above.
        await idem.abandon(
            wallet_id=request.wallet_id,
            endpoint=_SETTLE_ENDPOINT,
            idempotency_key=idempotency_key,
            expected_record_id=begun.record_id,
        )
        status_code = 404 if exc.reason == "permit_not_found" else 400
        raise HTTPException(status_code=status_code, detail=exc.reason)
    except PermitError as exc:
        await idem.abandon(
            wallet_id=request.wallet_id,
            endpoint=_SETTLE_ENDPOINT,
            idempotency_key=idempotency_key,
            expected_record_id=begun.record_id,
        )
        # permit_write_contended is transient: 503 so the caller retries the
        # same key (matching the AWI governance mapping).
        status_code = 503 if exc.reason == "permit_write_contended" else 400
        raise HTTPException(status_code=status_code, detail=exc.reason)

    response = X402SettleResponse(
        permit_id=settlement.permit_id,
        wallet_id=settlement.wallet_id,
        network=requirement.network,
        pay_to=requirement.pay_to,
        asset=requirement.asset,
        amount_usd=str(requirement.amount_usd),
        credits=str(settlement.credits),
        authorization=settlement.authorization,
        attestation=X402AttestationResponse(
            alg="Ed25519",
            signature=settlement.attestation_signature,
            key_id=settlement.attestation_key_id,
            payload_hash=settlement.attestation_payload_hash,
        ),
        receipt_id=settlement.receipt_id,
        shadow_session_id=settlement.shadow_session_id,
        shadow_charge_id=settlement.shadow_charge_id,
        audit_event_id=settlement.audit_event_id,
    )
    await idem.complete(
        wallet_id=request.wallet_id,
        endpoint=_SETTLE_ENDPOINT,
        idempotency_key=idempotency_key,
        response_reference=settlement.receipt_id,
        response_json=response.model_dump(mode="json"),
        status_code=200,
    )
    return response
