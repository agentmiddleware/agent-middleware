"""x402 payment facilitation against permit budgets.

Posture — read this before extending (docs/settlement-rails.md keeps the
settlement freeze in force): x402 here is a *facilitator* surface. It parses
HTTP 402 payment requirements, authorizes them against permit budgets, and
records micro-settlements in the shadow ledger plus signed receipts. It never
mints credits and never writes real ledger entries. The payer wallet — not the
trust plane — signs the on-chain authorization (``eth_signTypedData_v4`` over
the EIP-712 payload for EVM USDC; Solana's native Ed25519 for Solana). What
this module signs is an Ed25519 *facilitator attestation*: evidence that the
trust plane authorized and metered the payment, never the secp256k1 on-chain
authorization itself. There is no keccak and no EVM key anywhere in this repo,
by design.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.core.config import get_settings
from app.schemas.billing import ServiceCategory
from app.services.audit_log import record_audit_event
from app.services.permits import get_permit_service
from app.services.pricing import charge_units_for
from app.services.receipts import ReceiptWriteContendedError, get_receipt_service
from app.services.shadow_ledger import get_shadow_ledger
from app.services.signing_keys import get_signing_key_service

logger = logging.getLogger(__name__)


class X402Error(RuntimeError):
    """Fail-closed x402 facilitation error with a snake_case reason."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.details = details
        super().__init__(reason)


class X402SettlementUncertainError(X402Error):
    """Settlement or compensation may be durable; retain the original owner."""


# The tool name every x402 settlement is authorized and receipted under. A
# permit must list it in allowed_tools (scopes "tool:x402.payment:invoke" +
# "billing:charge") before any settlement can reserve budget.
X402_TOOL_NAME = "x402.payment"

# USDC is a 6-decimal token on every supported network; amounts with more
# precision cannot be represented on-chain and are rejected rather than
# rounded (rounding would settle a different number than was demanded).
_USDC_DECIMALS = 6

# Facilitation guardrail, not a business rule: one x402 demand above this is
# outside micro-settlement territory and refused outright rather than being
# allowed to encumber an entire permit budget in one call.
_MAX_AMOUNT_USD = Decimal("10000")

# Canonical USDC deployments per EVM network:
# (chainId, verifyingContract, EIP-712 domain name). These pin the EIP-712
# domain so the payer wallet's signature can only ever authorize the real
# USDC contract on the intended chain. The domain name is the FiatToken
# contract's initialized name(), which feeds its DOMAIN_SEPARATOR — Circle's
# mainnet deployments register "USD Coin" while the Base Sepolia test
# deployment registers "USDC"; a mismatched name makes every signature
# unverifiable on-chain.
_EVM_NETWORKS: dict[str, tuple[int, str, str]] = {
    "base": (8453, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USD Coin"),
    "base-sepolia": (84532, "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "USDC"),
    "ethereum": (1, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USD Coin"),
}

_SOLANA_NETWORKS = frozenset({"solana", "solana-devnet"})

SUPPORTED_NETWORKS = frozenset(_EVM_NETWORKS) | _SOLANA_NETWORKS

_EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}\Z")
# Base58 alphabet (no 0, O, I, l), the shape of a Solana account address.
_SOLANA_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}\Z")

_AMOUNT_HEADER = "x-402-amount"
_PAY_TO_HEADER = "x-402-payto"
_NETWORK_HEADER = "x-402-network"

# Least-wrong existing ServiceCategory for a micro-settlement's shadow-ledger
# entry: PLATFORM_FEE is the only platform-level (non-proof-surface) category,
# and the enum is not extended here because its members feed real pricing and
# ledger rows (adding one is a product decision, not a facilitation detail).
_SETTLEMENT_CATEGORY = ServiceCategory.PLATFORM_FEE


@dataclass(frozen=True)
class X402PaymentRequirement:
    """A validated x402 payment demand extracted from an HTTP 402 response."""

    amount_usd: Decimal
    pay_to: str
    network: str
    asset: str = "USDC"

    @property
    def amount_base_units(self) -> int:
        """The amount as USDC base units (6 decimals); exact by validation."""
        return int(self.amount_usd.scaleb(_USDC_DECIMALS))


@dataclass(frozen=True)
class X402Settlement:
    """One completed facilitation: authorization + attestation + receipt.

    ``authorization`` is what the payer wallet must sign; the attestation
    fields are the trust plane's Ed25519 evidence that this settlement was
    permit-authorized and metered — they are not an on-chain signature.
    """

    requirement: X402PaymentRequirement
    permit_id: str
    wallet_id: str
    credits: Decimal
    authorization: dict[str, Any]
    attestation_signature: str
    attestation_key_id: str
    attestation_payload_hash: str
    receipt_id: str
    shadow_session_id: str
    shadow_charge_id: str | None
    audit_event_id: str | None
    idempotency_key: str


def _epoch_seconds(value: datetime) -> int:
    """Naive-UTC column value -> unix seconds (naive is UTC by repo contract)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


class X402PaymentHandler:
    """Parse, authorize, and facilitate x402 micro-settlements."""

    def parse_402(
        self,
        status_code: int,
        headers: Mapping[str, str],
    ) -> X402PaymentRequirement:
        """Parse the X-402-* headers of an HTTP 402 response, failing closed.

        Only status 402 is parseable; every header is validated strictly with
        a distinct reason so a caller can tell a malformed demand from a
        missing one.
        """
        if status_code != 402:
            raise X402Error("x402_not_payment_required")
        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        amount = lowered.get(_AMOUNT_HEADER)
        if amount is None:
            raise X402Error("x402_amount_missing")
        pay_to = lowered.get(_PAY_TO_HEADER)
        if pay_to is None:
            raise X402Error("x402_pay_to_missing")
        network = lowered.get(_NETWORK_HEADER)
        if network is None:
            raise X402Error("x402_network_missing")
        return self.build_requirement(amount=amount, pay_to=pay_to, network=network)

    def build_requirement(
        self,
        *,
        amount: str | Decimal,
        pay_to: str,
        network: str,
        asset: str = "USDC",
    ) -> X402PaymentRequirement:
        """Validate raw requirement fields into an X402PaymentRequirement.

        Shared by header parsing and the settle endpoint so both surfaces
        enforce identical constraints.
        """
        if isinstance(amount, Decimal):
            parsed = amount
        else:
            try:
                parsed = Decimal(str(amount).strip())
            except InvalidOperation as exc:
                raise X402Error("x402_amount_invalid") from exc
        if not parsed.is_finite():
            raise X402Error("x402_amount_invalid")
        if parsed <= 0:
            raise X402Error("x402_amount_not_positive")
        exponent = parsed.as_tuple().exponent
        if isinstance(exponent, int) and -exponent > _USDC_DECIMALS:
            raise X402Error("x402_amount_precision_exceeded")
        if parsed > _MAX_AMOUNT_USD:
            raise X402Error("x402_amount_too_large")

        network = network.strip()
        if network not in SUPPORTED_NETWORKS:
            raise X402Error("x402_network_unsupported")

        # The engine only knows USDC: amounts convert at six USDC decimals and
        # EVM authorizations pin the per-network USDC contract. Accepting any
        # other asset string would emit settlement evidence claiming one token
        # while the typed data moves another. Normalize before storing so the
        # receipts/audit evidence carry exactly "USDC", never a padded variant.
        asset = asset.strip()
        if asset != "USDC":
            raise X402Error("x402_asset_unsupported")

        pay_to = pay_to.strip()
        if network in _EVM_NETWORKS:
            if not _EVM_ADDRESS_RE.fullmatch(pay_to):
                raise X402Error("x402_pay_to_invalid")
        else:
            if not _SOLANA_ADDRESS_RE.fullmatch(pay_to):
                raise X402Error("x402_pay_to_invalid")

        return X402PaymentRequirement(
            amount_usd=parsed,
            pay_to=pay_to,
            network=network,
            asset=asset,
        )

    @staticmethod
    def _derive_nonce_hex(permit_id: str, idempotency_key: str) -> str:
        """32-byte hex nonce, deterministic per (permit, idempotency key).

        Determinism is the replay defense: an idempotent retry rebuilds the
        byte-identical authorization instead of minting a second, differently
        nonced transfer the payer wallet might also sign.
        """
        return hashlib.sha256(f"{permit_id}:{idempotency_key}".encode()).hexdigest()

    def build_transfer_authorization(
        self,
        requirement: X402PaymentRequirement,
        *,
        permit_id: str,
        wallet_id: str,
        idempotency_key: str,
        payer: str | None = None,
        valid_after: int = 0,
        valid_before: int | None = None,
    ) -> dict[str, Any]:
        """Build the transfer authorization the *payer wallet* must sign.

        EVM networks get a full EIP-712 TransferWithAuthorization (EIP-3009)
        typed-data dict in ``eth_signTypedData_v4`` shape. ``payer`` is the
        payer wallet's on-chain address and is REQUIRED for EVM networks:
        the facilitator attestation signs this exact payload, so a blank
        ``from`` would leave the actually-signed transfer unbound from the
        attestation the receipt carries. The payer wallet still computes the
        EIP-712 digest and produces the on-chain signature itself. Solana
        networks get a structured transfer message (Ed25519 is Solana's
        native scheme); ``payer`` is optional there and included when given.
        Nothing here is signed by this method; the wallet binding lives in
        the facilitator attestation that ``settle`` produces over this
        payload.
        """
        del wallet_id  # bound via the attestation payload, not the on-chain bytes
        nonce_hex = self._derive_nonce_hex(permit_id, idempotency_key)
        value = str(requirement.amount_base_units)
        if requirement.network in _EVM_NETWORKS:
            if valid_before is None:
                raise X402Error("x402_validity_window_required")
            if payer is None:
                raise X402Error("x402_payer_required")
            payer = payer.strip()
            if not _EVM_ADDRESS_RE.fullmatch(payer):
                raise X402Error("x402_payer_invalid")
            chain_id, usdc_contract, domain_name = _EVM_NETWORKS[requirement.network]
            return {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "TransferWithAuthorization": [
                        {"name": "from", "type": "address"},
                        {"name": "to", "type": "address"},
                        {"name": "value", "type": "uint256"},
                        {"name": "validAfter", "type": "uint256"},
                        {"name": "validBefore", "type": "uint256"},
                        {"name": "nonce", "type": "bytes32"},
                    ],
                },
                "primaryType": "TransferWithAuthorization",
                "domain": {
                    "name": domain_name,
                    "version": "2",
                    "chainId": chain_id,
                    "verifyingContract": usdc_contract,
                },
                "message": {
                    "from": payer,
                    "to": requirement.pay_to,
                    "value": value,
                    "validAfter": str(valid_after),
                    "validBefore": str(valid_before),
                    "nonce": f"0x{nonce_hex}",
                },
            }
        if payer is not None:
            payer = payer.strip()
            if not _SOLANA_ADDRESS_RE.fullmatch(payer):
                raise X402Error("x402_payer_invalid")
        authorization: dict[str, Any] = {
            "scheme": "x402-solana-transfer/1",
            "network": requirement.network,
            "asset": requirement.asset,
            "pay_to": requirement.pay_to,
            "amount": value,
            "decimals": _USDC_DECIMALS,
            "memo": f"awi-permit:{permit_id}",
            "nonce": nonce_hex,
            # The permit validity window rides in the structured message —
            # mirroring the EVM validAfter/validBefore — so a payer wallet
            # signing late, or replaying a stored payload, can see that the
            # permit window has closed instead of blindly signing it.
            "valid_after": str(valid_after),
            "valid_before": str(valid_before) if valid_before is not None else None,
        }
        if payer is not None:
            authorization["payer"] = payer
        return authorization

    async def settle(
        self,
        *,
        permit_id: str,
        wallet_id: str,
        key_id: str | None,
        requirement: X402PaymentRequirement,
        idempotency_key: str,
        payer: str | None = None,
        idempotency_record_id: str | None = None,
    ) -> X402Settlement:
        """Authorize a 402 demand against a permit and record the settlement.

        Sequence: reserve permit budget atomically, build the transfer
        authorization, sign the facilitator attestation, meter the amount in a
        shadow-ledger dry-run session, append an audit event, emit the signed
        receipt. Known unreceipted failures compensate the reservation;
        uncertain receipt writes or incomplete compensation retain ownership
        for review so retries cannot reserve the same authority again.
        """
        settings = get_settings()
        # settings.EXCHANGE_RATE is the single credits-per-USD source of truth
        # (see app/core/config.py — do not re-declare it as a constant here).
        credits = requirement.amount_usd * settings.EXCHANGE_RATE

        # Validate the payer before touching the permit so a missing or
        # malformed address never reserves budget it must then compensate.
        # build_transfer_authorization re-checks as defense in depth.
        if requirement.network in _EVM_NETWORKS:
            if payer is None:
                raise X402Error("x402_payer_required")
            if not _EVM_ADDRESS_RE.fullmatch(payer.strip()):
                raise X402Error("x402_payer_invalid")
        elif payer is not None and not _SOLANA_ADDRESS_RE.fullmatch(payer.strip()):
            raise X402Error("x402_payer_invalid")

        permits = get_permit_service()
        validation = await permits.authorize_and_reserve(
            permit_id=permit_id,
            wallet_id=wallet_id,
            tool_name=X402_TOOL_NAME,
            estimated_credits=credits,
            key_id=key_id,
            # pay_to / network / amount ride along so permit v2 constraints
            # (forbidden_fields, and recipient checks where enforced) can bite.
            arguments={
                "pay_to": requirement.pay_to,
                "network": requirement.network,
                "amount_usd": str(requirement.amount_usd),
            },
        )
        if not validation.allowed:
            # Budget-cap denials surface reason "permit_budget_exceeded"
            # untouched, with the service's own numbers in details.
            raise X402Error(
                validation.reason or "x402_authorization_denied",
                validation.details,
            )
        permit_model = validation.permit
        assert permit_model is not None  # allowed=True always carries the row

        # Budget is reserved; compensate only when receipt absence is proven.
        shadow_session_id: str | None = None
        receipt_attempted = False
        try:
            authorization = self.build_transfer_authorization(
                requirement,
                permit_id=permit_id,
                wallet_id=wallet_id,
                idempotency_key=idempotency_key,
                payer=payer,
                valid_after=_epoch_seconds(permit_model.issued_at),
                valid_before=_epoch_seconds(permit_model.expires_at),
            )

            # Ed25519 facilitator attestation: trust-plane evidence over the
            # authorization + permit binding, NOT the on-chain signature.
            attestation_payload: dict[str, Any] = {
                "authorization": authorization,
                "permit_id": permit_id,
                "wallet_id": wallet_id,
                "credits": credits,
                "idempotency_key": idempotency_key,
            }
            signing = get_signing_key_service()
            (
                attestation_signature,
                attestation_key_id,
                attestation_payload_hash,
            ) = await signing.sign_payload(attestation_payload)

            # Micro-settlement metering goes to the shadow ledger only: one
            # dry-run session per settle call, funded with exactly this
            # settlement's credit value, charged once, then ended. Never
            # commit_session — that debits real money, which the settlement
            # freeze forbids.
            ledger = get_shadow_ledger()
            session = await ledger.create_session(wallet_id, real_balance=credits)
            shadow_session_id = session.session_id
            units = charge_units_for(credits, _SETTLEMENT_CATEGORY)
            charge = await ledger.simulate_charge(
                session.session_id,
                _SETTLEMENT_CATEGORY,
                units=float(units),
                description=(
                    f"x402 {requirement.amount_usd} USD -> {requirement.pay_to} "
                    f"on {requirement.network} under permit {permit_id}"
                ),
            )
            if not charge.would_succeed:
                raise X402Error("x402_shadow_ledger_rejected")
            # Ending the session before the receipt means a receipt-step
            # failure leaves zero live shadow state: the ephemeral session is
            # already terminally claimed, and the durable receipt — the actual
            # settlement record — is written last or not at all.
            summary = await ledger.end_session(session.session_id)
            if summary is None:
                raise X402Error("x402_shadow_session_lost")

            audit_event = await record_audit_event(
                event="x402.settlement",
                wallet_id=wallet_id,
                tool=X402_TOOL_NAME,
                endpoint="/v1/x402/settle",
                key_id=key_id,
                request_id=idempotency_key[:100],
                ok=True,
                metadata={
                    "permit_id": permit_id,
                    "pay_to": requirement.pay_to,
                    "network": requirement.network,
                    "asset": requirement.asset,
                    "amount_usd": str(requirement.amount_usd),
                    "credits": str(credits),
                    "shadow_session_id": session.session_id,
                    "shadow_charge_id": charge.charge_id,
                    "nonce": self._derive_nonce_hex(permit_id, idempotency_key),
                    "idempotency_key": idempotency_key,
                },
            )

            # credits_charged feeds _validate_model_for_action's aggregate sum
            # (SUM of ReceiptModel.credits_charged), so these receipts count
            # toward the permit's aggregate_value_cap like any governed spend.
            receipt_attempted = True
            receipt = await get_receipt_service().create_receipt(
                permit_id=permit_id,
                wallet_id=wallet_id,
                key_id=key_id,
                tool=X402_TOOL_NAME,
                request_payload={
                    "amount_usd": str(requirement.amount_usd),
                    "pay_to": requirement.pay_to,
                    "network": requirement.network,
                    "asset": requirement.asset,
                    "idempotency_key": idempotency_key,
                },
                response_payload={
                    "authorization": authorization,
                    "attestation": {
                        "alg": "Ed25519",
                        "signature": attestation_signature,
                        "key_id": attestation_key_id,
                        "payload_hash": attestation_payload_hash,
                    },
                    "shadow_session_id": session.session_id,
                    "shadow_charge_id": charge.charge_id,
                    "credits": str(credits),
                },
                ledger_entry_id=None,
                credits_authorized=credits,
                credits_charged=credits,
                outcome="success",
                audit_event_id=audit_event.event_id,
                idempotency_record_id=idempotency_record_id,
            )
        except Exception as exc:
            if receipt_attempted and not isinstance(exc, ReceiptWriteContendedError):
                # An insert/commit acknowledgement can fail after persistence.
                # Presence proves settlement; absence or a failed read cannot
                # prove rollback. Only the typed exhaustion above guarantees
                # that no receipt was written and compensation is safe.
                reason = "x402_settlement_needs_review"
                if idempotency_record_id is not None:
                    try:
                        persisted = await get_receipt_service().get_receipt_by_idempotency_record_id(
                            idempotency_record_id
                        )
                        if persisted is not None:
                            reason = "x402_settled_unrecoverable_replay"
                    except Exception:
                        logger.exception(
                            "x402 receipt lookup failed for %s", idempotency_record_id
                        )
                raise X402SettlementUncertainError(reason) from exc

            compensation_complete = True
            budget_released = False
            tool_call_released = False
            if shadow_session_id is not None:
                try:
                    await get_shadow_ledger().revert_session(shadow_session_id)
                except Exception:
                    compensation_complete = False
                    logger.exception(
                        "x402 shadow session discard failed for %s",
                        shadow_session_id,
                    )
            try:
                await permits.release_budget(permit_id, credits)
                budget_released = True
            except Exception:
                compensation_complete = False
                logger.exception("x402 budget release failed for permit %s", permit_id)
            # authorize_and_reserve consumed one max_calls_per_tool use along
            # with the budget; a compensated failure must give both back or a
            # one-call permit's legitimate retry is denied
            # permit_max_calls_exceeded with no receipt to show for it.
            try:
                await permits.release_tool_call(permit_id, X402_TOOL_NAME)
                tool_call_released = True
            except Exception:
                compensation_complete = False
                logger.exception(
                    "x402 tool-call release failed for permit %s", permit_id
                )
            # The success audit event (if it was written) must not stand as
            # the last word on an attempt that did not settle: append a
            # compensating failure event so the chain records what actually
            # happened instead of a success with no receipt.
            reason = (
                exc.reason if isinstance(exc, X402Error) else "x402_settlement_failed"
            )
            if not compensation_complete:
                reason = "x402_settlement_needs_review"
            try:
                await record_audit_event(
                    event="x402.settlement_failed",
                    wallet_id=wallet_id,
                    tool=X402_TOOL_NAME,
                    endpoint="/v1/x402/settle",
                    key_id=key_id,
                    request_id=idempotency_key[:100],
                    ok=False,
                    error=reason,
                    metadata={
                        "permit_id": permit_id,
                        "idempotency_key": idempotency_key,
                        "credits_released": str(credits) if budget_released else None,
                        "tool_call_released": tool_call_released,
                        "compensation_complete": compensation_complete,
                    },
                )
            except Exception:
                logger.exception(
                    "x402 compensating audit event failed for permit %s",
                    permit_id,
                )
            if not compensation_complete:
                raise X402SettlementUncertainError(reason) from exc
            if isinstance(exc, X402Error):
                raise
            raise X402Error("x402_settlement_failed") from exc

        return X402Settlement(
            requirement=requirement,
            permit_id=permit_id,
            wallet_id=wallet_id,
            credits=credits,
            authorization=authorization,
            attestation_signature=attestation_signature,
            attestation_key_id=attestation_key_id,
            attestation_payload_hash=attestation_payload_hash,
            receipt_id=receipt.receipt_id,
            shadow_session_id=session.session_id,
            shadow_charge_id=charge.charge_id,
            audit_event_id=audit_event.event_id,
            idempotency_key=idempotency_key,
        )


_handler: X402PaymentHandler | None = None


def get_x402_handler() -> X402PaymentHandler:
    global _handler
    if _handler is None:
        _handler = X402PaymentHandler()
    return _handler
