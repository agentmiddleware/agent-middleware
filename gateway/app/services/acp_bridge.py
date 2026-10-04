"""ACP commerce bridge: checkouts translated into trust-plane bounds.

ACP (Agentic Commerce Protocol) checkouts are translated into PermitV2
tool-execution bounds and settled via Stripe Shared Payment Tokens. The
adapter never mints credits and never writes real ledger entries: the durable
money artifacts it produces are a budget reservation on a purpose-minted
permit, a signed receipt, and a hash-chained audit event. Order ids
(``acp-{intent_id}``) are bound to the tamper-evident audit chain by being
signed into the event's metadata and indexed via ``request_id``.

Settlement-rails conformance (docs/settlement-rails.md):

* item 3 — the charged amount is derived server-side from the line items,
  never from client metadata;
* item 4 — the client-asserted ``client_total`` must equal the derived total
  exactly or the checkout is refused (``acp_total_mismatch``);
* item 5 — currency is validated against a fail-closed allowlist at the
  schema boundary and revalidated by the Stripe helper;
* item 7 — a repeated ``intent_id`` replays the original checkout result via
  the durable idempotency service and provably never charges twice (a
  wallet-scoped key derived from the intent id also rides to Stripe as the
  PaymentIntent idempotency key — see ``stripe_idempotency_key``).

The ``spt_token`` is never logged and never persisted: it is excluded from
the idempotency request payload, the receipt payloads, and the audit
metadata.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.time import utc_now
from app.schemas.acp import ACPCheckoutRequest, ACPCheckoutResponse, ACPLineItem
from app.schemas.trust import PermitCreateRequest
from app.services.audit_log import record_audit_event
from app.services.idempotency import (
    IdempotencyBegin,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    get_idempotency_service,
)
from app.services.permits import PermitError, get_permit_service
from app.services.receipts import get_receipt_service
from app.services.signing_keys import sha256_hex
from app.services.stripe_integration import get_stripe_integration

logger = logging.getLogger(__name__)

ACP_CHECKOUT_TOOL = "acp.checkout"
ACP_CHECKOUT_ENDPOINT = "/v1/billing/acp/checkout"
# Short expiry: the permit exists only to bound this one checkout, so it must
# not remain reservable long after the settlement window has passed.
ACP_PERMIT_TTL = timedelta(minutes=15)
_SETTLED_STRIPE_STATUSES = frozenset({"succeeded"})
# PaymentIntent statuses Stripe allows to be canceled. Deliberately excludes
# "processing": Stripe refuses to cancel an intent that is already being
# captured, so attempting it would only add a guaranteed API error on top of
# the failure being handled.
_CANCELABLE_STRIPE_STATUSES = frozenset(
    {"requires_action", "requires_confirmation", "requires_payment_method"}
)

# An in-progress intent record idle past this threshold is treated as a
# crashed attempt and recovered (see execute_checkout); mirrors the 300s
# staleness standard used by the governed-MCP reconciliation sweep.
# Safety invariant, not a heuristic: the only long-blocking step in a live
# checkout is the outbound Stripe call, and the Stripe HTTP client's explicit
# timeout (stripe_integration.STRIPE_HTTP_TIMEOUT_SECONDS, 80s) sits far
# below this threshold — so a record older than this cannot belong to a live
# attempt. Backstop if that ever regresses: abandon() refuses completed or
# charged records, and the receipts table's idempotency_record_id FK stops a
# record a receipt references from being deleted.
_INTENT_STALE_SECONDS = 300


class ACPBridgeError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def derive_total_minor(line_items: list[ACPLineItem]) -> int:
    """Server-side total in minor units: sum(quantity * unit_amount)."""
    return sum(item.quantity * item.unit_amount for item in line_items)


def total_minor_to_credits(total_minor: int) -> Decimal:
    """Convert a minor-unit USD amount into internal credits.

    Same formula as the Stripe top-up path: credits = cents * EXCHANGE_RATE
    / 100, with the rate read from settings at call time (never re-declared
    as a module constant).
    """
    return Decimal(total_minor) * get_settings().EXCHANGE_RATE / Decimal("100")


def line_items_digest(line_items: list[ACPLineItem]) -> str:
    """Stable digest of the line items for receipts/idempotency payloads."""
    return sha256_hex(
        {"line_items": [item.model_dump(mode="json") for item in line_items]}
    )


def stripe_idempotency_key(order_id: str, agent_wallet_id: str) -> str:
    """Wallet-scoped Stripe PaymentIntent idempotency key for one checkout.

    Deterministic for (agent_wallet_id, intent_id) — that determinism IS the
    double-charge defense: every re-run of the same wallet's intent (a crash
    retry, the stale-intent recovery re-run in ``execute_checkout``) presents
    the same key, so Stripe replays the original PaymentIntent instead of
    creating a second charge. Never derive anything time- or attempt-varying
    into this key.

    Scoped by the agent wallet because our intent idempotency records are per
    wallet: a bare order id would be one GLOBAL Stripe key, so a second wallet
    reusing the same client-chosen intent id would either silently replay the
    first wallet's PaymentIntent (identical parameters) or draw a Stripe
    idempotency_error (different parameters). The order id itself stays the
    caller-visible order handle and is unchanged by this scoping.

    Length is provably within Stripe's 255-char idempotency-key cap with no
    digest fallback needed: the order id is at most 132 chars ("acp-" plus
    the schema's 128-char intent_id cap) and wallet ids are capped at 50
    chars by the wallets table, so the joined key never exceeds 183 chars.
    """
    return f"{order_id}:{agent_wallet_id}"


def audit_request_id(order_id: str) -> str:
    """Collision-safe audit ``request_id`` for an ``acp-{intent_id}`` order id.

    The audit table's indexed request_id column is capped at 100 chars;
    order ids derived from a maximum-length intent_id (128 chars) exceed it.
    Fall back to a deterministic digest rather than a truncation that could
    collide. The full order id is always in the signed metadata regardless.
    Public so the router's governance events can index under the SAME key as
    the bridge's settlement events — one checkout, one request_id.
    """
    if len(order_id) <= 100:
        return order_id
    return f"acp-{sha256_hex(order_id)}"


def translate_to_permit_bounds(
    request: ACPCheckoutRequest,
    *,
    sponsor_wallet_id: str,
    agent_wallet_id: str,
    key_id: str | None,
) -> PermitCreateRequest:
    """Translate an ACP checkout into PermitV2 creation bounds.

    Pure (no wallet/DB access) so the translation is unit-testable on its
    own: the derived credit budget doubles as both ``max_credits`` and the
    ``aggregate_value_cap``, the tool is single-use via ``max_calls_per_tool``,
    and the merchant hostname becomes the ``recipient_domain`` constraint.
    """
    credits = total_minor_to_credits(derive_total_minor(request.line_items))
    # The permit schema refuses a budget no permit can carry. Raise that as
    # the permit refusal it is, so the caller abandons the intent instead of
    # leaving it in progress behind a ValidationError. A $0 checkout keeps
    # the reason create_permit gave it.
    if credits <= 0:
        raise PermitError("max_credits_must_be_positive")
    try:
        return PermitCreateRequest(
            issuer_wallet_id=sponsor_wallet_id,
            subject_wallet_id=agent_wallet_id,
            subject_key_id=key_id,
            allowed_tools=[ACP_CHECKOUT_TOOL],
            max_credits=credits,
            expires_at=utc_now() + ACP_PERMIT_TTL,
            max_calls_per_tool={ACP_CHECKOUT_TOOL: 1},
            aggregate_value_cap=credits,
            forbidden_fields=[],
            recipient_domain=request.merchant_domain,
        )
    except ValidationError as exc:
        # Beyond the Numeric(20, 8) budget column: more whole digits than any
        # wallet balance holds, or an exchange rate finer than 8 decimals.
        raise PermitError("acp_permit_bounds_invalid") from exc


class ACPCommerceAdapter:
    """Executes ACP checkouts through the governed trust-plane loop."""

    @staticmethod
    def _idempotency_payload(
        request: ACPCheckoutRequest,
        *,
        sponsor_wallet_id: str,
        agent_wallet_id: str,
        key_id: str | None,
    ) -> dict[str, Any]:
        # Deliberately excludes spt_token: the credential must never be
        # persisted (not even hashed into a stored request identity), and a
        # merchant re-issuing a token for the same intent must still replay
        # the original settled checkout.
        return {
            "intent_id": request.intent_id,
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_wallet_id": agent_wallet_id,
            "key_id": key_id,
            "merchant_domain": request.merchant_domain,
            "client_total": request.client_total,
            "line_items_digest": line_items_digest(request.line_items),
        }

    @staticmethod
    async def _abandon_intent(*, agent_wallet_id: str, intent_id: str) -> None:
        # Failed checkouts must not pin the intent id forever: abandon only
        # deletes an uncompleted, uncharged record, so a settled checkout's
        # replay is never destroyed by this call.
        await get_idempotency_service().abandon(
            wallet_id=agent_wallet_id,
            endpoint=ACP_CHECKOUT_ENDPOINT,
            idempotency_key=intent_id,
        )

    @staticmethod
    async def _cancel_unsettled_charge(
        charge: dict[str, Any],
        *,
        order_id: str,
        stripe_idem_key: str,
    ) -> dict[str, Any]:
        """Best-effort cancel of a confirmed-but-not-settled PaymentIntent.

        A charge that came back non-settled is refused and rolled back on
        our side, but the PaymentIntent itself stays live at Stripe and can
        still settle later with no governance record. Cancel it when Stripe
        allows (see _CANCELABLE_STRIPE_STATUSES) and report the outcome —
        "canceled", "cancel_failed", or "not_cancelable" — as evidence
        metadata for the rollback audit event. Never raises: a failing
        cancel must not mask the original non-settled error, and the
        cancel-failed evidence is what makes the orphaned intent findable.
        """
        payment_intent_id = charge.get("payment_intent_id")
        payment_status = charge.get("status")
        evidence: dict[str, Any] = {
            "stripe_payment_intent_id": payment_intent_id,
            "stripe_payment_status": payment_status,
        }
        if not payment_intent_id or payment_status not in _CANCELABLE_STRIPE_STATUSES:
            evidence["payment_intent_cancel"] = "not_cancelable"
            return evidence
        try:
            await get_stripe_integration().cancel_payment_intent(
                payment_intent_id,
                # Deterministic per (wallet, intent), like the charge key, so
                # a crash-retried cancel replays instead of erroring.
                idempotency_key=f"{stripe_idem_key}:cancel",
            )
            evidence["payment_intent_cancel"] = "canceled"
        except Exception:
            logger.exception(
                "Failed to cancel unsettled PaymentIntent %s for order %s",
                payment_intent_id,
                order_id,
            )
            evidence["payment_intent_cancel"] = "cancel_failed"
        return evidence

    async def execute_checkout(
        self,
        request: ACPCheckoutRequest,
        *,
        sponsor_wallet_id: str,
        agent_wallet_id: str,
        key_id: str | None,
    ) -> ACPCheckoutResponse:
        """Derive → permit → reserve → charge → audit → receipt, atomically.

        The subject (agent) wallet must hold balance >= the derived credit
        budget: ``create_permit`` refuses otherwise
        (``permit_budget_exceeds_wallet_balance``), so callers must use a
        funded wallet. The wallet itself is never debited here — the permit
        reservation is the economic bound.

        Any failure after the budget reservation releases the exact reserved
        amount and abandons the intent's idempotency record before
        re-raising, so no partial checkout state survives.
        """
        # 1. Server-side total; the client-asserted number is checked for
        # exact equality and never trusted (checklist items 3 and 4).
        total_minor = derive_total_minor(request.line_items)
        if request.client_total != total_minor:
            raise ACPBridgeError("acp_total_mismatch")
        credits = total_minor_to_credits(total_minor)
        # Quantize to exactly two decimals so 50 cents renders as "0.50",
        # never "0.5": receipts and replays compare this string byte-for-byte.
        derived_total = str(
            (Decimal(total_minor) / Decimal("100")).quantize(Decimal("0.01"))
        )
        currency = request.line_items[0].currency
        order_id = f"acp-{request.intent_id}"
        stripe_idem_key = stripe_idempotency_key(order_id, agent_wallet_id)

        # 2. Idempotency on intent_id (checklist item 7): a repeated intent
        # replays the original result and must not reach the charge path.
        idem = get_idempotency_service()
        idem_payload = self._idempotency_payload(
            request,
            sponsor_wallet_id=sponsor_wallet_id,
            agent_wallet_id=agent_wallet_id,
            key_id=key_id,
        )
        try:
            begun = await idem.begin_with_record(
                wallet_id=agent_wallet_id,
                endpoint=ACP_CHECKOUT_ENDPOINT,
                idempotency_key=request.intent_id,
                request_payload=idem_payload,
            )
        except IdempotencyConflictError as exc:
            raise ACPBridgeError("acp_intent_conflict") from exc
        except IdempotencyInProgressError as exc:
            # A record left in progress can be a live concurrent checkout — or
            # the wreckage of a process that died between the Stripe charge
            # and `idem.complete`. The charge is guarded by mark_charged() which
            # sets ledger_entry_id immediately upon settlement, preventing
            # abandon() from deleting the record on audit/receipt failure.
            # So a record in progress with response_json=None and ledger_entry_id set
            # is always a crash (since mark_charged() runs before audit/receipt).
            # Without this branch a crashed intent is wedged forever: charged at
            # Stripe, no order, every retry rejected. A record idle past the repo's
            # standard 300s staleness threshold is treated as crashed and
            # recovered by crash shape:
            #  - died AFTER the receipt (the durable settlement record): the
            #    checkout DID settle — reconstruct the response from the
            #    receipt (order_id and derived_total are deterministic) and
            #    complete the record with it, exactly what
            #    reconcile_stuck_records does for receipted MCP records.
            #  - died BEFORE the receipt: abandon the record (abandon refuses
            #    completed/charged ones) and re-run. Re-execution cannot
            #    double-charge — the Stripe idempotency key is deterministic
            #    for (wallet, intent) via stripe_idempotency_key, so Stripe
            #    returns the original PaymentIntent (checklist item 7). The
            #    crashed attempt's single-use permit is left to expire; the
            #    budget reconciliation sweep already handles expired permits.
            record = await idem.get_record(
                wallet_id=agent_wallet_id,
                endpoint=ACP_CHECKOUT_ENDPOINT,
                idempotency_key=request.intent_id,
            )
            stale = (
                record is not None
                and record.response_json is None
                and record.created_at
                < utc_now() - timedelta(seconds=_INTENT_STALE_SECONDS)
            )
            if not stale:
                raise ACPBridgeError("acp_intent_in_progress") from exc
            assert record is not None
            receipt = await get_receipt_service().get_receipt_by_idempotency_record_id(
                record.record_id
            )
            if receipt is not None and receipt.audit_event_id is not None:
                recovered = ACPCheckoutResponse(
                    order_id=order_id,
                    intent_id=request.intent_id,
                    permit_id=receipt.permit_id,
                    receipt_id=receipt.receipt_id,
                    audit_event_id=receipt.audit_event_id,
                    derived_total=derived_total,
                    status="settled",
                )
                await idem.complete(
                    wallet_id=agent_wallet_id,
                    endpoint=ACP_CHECKOUT_ENDPOINT,
                    idempotency_key=request.intent_id,
                    response_reference=receipt.receipt_id,
                    response_json=recovered.model_dump(mode="json"),
                    status_code=200,
                )
                return recovered
            # Mark the recovery on the audit chain BEFORE abandoning: after
            # the re-run this order legitimately shows two permits and one
            # receipt, and this event lets an operator read that shape as
            # crash recovery rather than a defect. Best-effort — recovering
            # a wedged intent must not fail on evidence bookkeeping.
            try:
                await record_audit_event(
                    event="acp_intent_recovered",
                    wallet_id=agent_wallet_id,
                    tool=ACP_CHECKOUT_TOOL,
                    endpoint=ACP_CHECKOUT_ENDPOINT,
                    key_id=key_id,
                    request_id=audit_request_id(order_id),
                    ok=True,
                    metadata={
                        "order_id": order_id,
                        "intent_id": request.intent_id,
                        "abandoned_record_id": record.record_id,
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to record acp_intent_recovered evidence for order %s",
                    order_id,
                )
            await self._abandon_intent(
                agent_wallet_id=agent_wallet_id, intent_id=request.intent_id
            )
            try:
                begun = await idem.begin_with_record(
                    wallet_id=agent_wallet_id,
                    endpoint=ACP_CHECKOUT_ENDPOINT,
                    idempotency_key=request.intent_id,
                    request_payload=idem_payload,
                )
            except IdempotencyConflictError as retry_exc:
                raise ACPBridgeError("acp_intent_conflict") from retry_exc
            except IdempotencyInProgressError as retry_exc:
                # If abandon() couldn't delete the record (because ledger_entry_id
                # is set), the record is still in progress. For stale recovery, we
                # can re-use it since we know it's a crash (created_at is stale).
                # We just need to treat it like a fresh begin for the re-run.
                if record.ledger_entry_id:
                    begun = IdempotencyBegin(
                        record_id=record.record_id,
                        request_hash=record.request_hash,
                        replay=None,
                    )
                else:
                    raise ACPBridgeError("acp_intent_in_progress") from retry_exc
        if begun.replay is not None:
            payload = begun.replay.response_json
            if not isinstance(payload, dict):
                raise ACPBridgeError("acp_intent_conflict")
            try:
                return ACPCheckoutResponse.model_validate(payload)
            except ValueError as exc:
                raise ACPBridgeError("acp_intent_conflict") from exc

        # 3. Translate to PermitV2 bounds and mint the single-use permit.
        try:
            permit = await get_permit_service().create_permit(
                translate_to_permit_bounds(
                    request,
                    sponsor_wallet_id=sponsor_wallet_id,
                    agent_wallet_id=agent_wallet_id,
                    key_id=key_id,
                )
            )
        except PermitError as exc:
            await self._abandon_intent(
                agent_wallet_id=agent_wallet_id, intent_id=request.intent_id
            )
            raise ACPBridgeError(exc.reason) from exc

        # 4. Atomic authorize + budget reservation under the permit. A
        # PermitError here (e.g. permit_write_contended) must free the intent
        # id exactly like a create_permit failure: nothing was reserved, so
        # leaving the begun record in progress would wedge the intent until
        # the stale-recovery window instead of letting the caller retry.
        try:
            validation = await get_permit_service().authorize_and_reserve(
                permit_id=permit.permit_id,
                wallet_id=agent_wallet_id,
                tool_name=ACP_CHECKOUT_TOOL,
                estimated_credits=credits,
                key_id=key_id,
                arguments={
                    "merchant_domain": request.merchant_domain,
                    "intent_id": request.intent_id,
                },
            )
        except PermitError as exc:
            await self._abandon_intent(
                agent_wallet_id=agent_wallet_id, intent_id=request.intent_id
            )
            raise ACPBridgeError(exc.reason) from exc
        if not validation.allowed:
            await self._abandon_intent(
                agent_wallet_id=agent_wallet_id, intent_id=request.intent_id
            )
            raise ACPBridgeError(validation.reason or "acp_authorization_denied")

        # 5. From here on, every failure must release the exact reservation
        # (and free the intent id) before re-raising: no partial state. Each
        # compensation step is guarded on its own: this runs inside an except
        # block, so an unguarded failure here would REPLACE the original
        # error being handled — and a failing release must never stop the
        # abandon from freeing the intent id (the reservation dies with the
        # short-TTL permit; a wedged intent record has no such expiry).
        async def _rollback() -> None:
            try:
                await get_permit_service().release_budget(permit.permit_id, credits)
            except Exception:
                logger.exception(
                    "ACP rollback failed to release %s credits on permit %s "
                    "for order %s",
                    credits,
                    permit.permit_id,
                    order_id,
                )
            try:
                await self._abandon_intent(
                    agent_wallet_id=agent_wallet_id, intent_id=request.intent_id
                )
            except Exception:
                logger.exception(
                    "ACP rollback failed to abandon the intent record for order %s",
                    order_id,
                )

        async def _record_rollback_evidence(
            event: str,
            failure: BaseException,
            extra_metadata: dict[str, Any] | None = None,
        ) -> None:
            # A rollback after the charge attempt may be hiding real money
            # movement: a transport failure (timeout/connection reset) can
            # land AFTER Stripe captured, and a definitively non-settled
            # status can still resolve later on Stripe's side. Best-effort
            # append an ok=False audit event carrying the Stripe idempotency
            # key so any orphaned charge is always discoverable from the
            # audit chain. Guarded so this write can never mask the original
            # error — and it never includes the token.
            try:
                await record_audit_event(
                    event=event,
                    wallet_id=agent_wallet_id,
                    tool=ACP_CHECKOUT_TOOL,
                    endpoint=ACP_CHECKOUT_ENDPOINT,
                    key_id=key_id,
                    request_id=audit_request_id(order_id),
                    ok=False,
                    metadata={
                        "order_id": order_id,
                        "intent_id": request.intent_id,
                        "merchant_domain": request.merchant_domain,
                        "derived_total_minor": total_minor,
                        "currency": currency,
                        "stripe_idempotency_key": stripe_idem_key,
                        "failure": type(failure).__name__,
                        **(extra_metadata or {}),
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to record %s audit evidence for order %s",
                    event,
                    order_id,
                )

        # 6. Outbound settlement via the Shared Payment Token. The
        # wallet-scoped intent key rides to Stripe as the PaymentIntent
        # idempotency key, so even a crash-retry that reaches Stripe twice
        # cannot charge twice — and two wallets reusing one client-chosen
        # intent id never share a Stripe key (see stripe_idempotency_key).
        charge_evidence: dict[str, Any] = {}
        try:
            charge = await get_stripe_integration().charge_shared_payment_token(
                spt_token=request.spt_token.get_secret_value(),
                amount_minor=total_minor,
                currency=currency,
                idempotency_key=stripe_idem_key,
            )
            if charge.get("status") not in _SETTLED_STRIPE_STATUSES:
                # The confirmed PaymentIntent is still live at Stripe: left
                # alone it could settle later (e.g. requires_action resolved
                # out-of-band) with no governance record on our side. Cancel
                # it best-effort before rolling back — only for statuses
                # Stripe can cancel ("processing" cannot be), and never
                # letting a cancel failure mask the original non-settled
                # error. The outcome lands in the rollback evidence either
                # way, so an operator can find any intent left live.
                charge_evidence = await self._cancel_unsettled_charge(
                    charge,
                    order_id=order_id,
                    stripe_idem_key=stripe_idem_key,
                )
                raise RuntimeError("acp_spt_not_settled")
        except Exception as exc:
            await _record_rollback_evidence(
                "acp_checkout_charge_failed", exc, charge_evidence
            )
            await _rollback()
            raise ACPBridgeError("acp_spt_charge_failed") from exc

        # 6b. Mark the charge landed IMMEDIATELY after successful settlement.
        # Sets ledger_entry_id on the idempotency record, preventing abandon()
        # from deleting it if audit/receipt creation fails below. This prevents
        # cart rebinding even if post-charge bookkeeping fails.
        await idem.mark_charged(
            wallet_id=agent_wallet_id,
            endpoint=ACP_CHECKOUT_ENDPOINT,
            idempotency_key=request.intent_id,
            ledger_entry_id=charge.get("payment_intent_id") or "unknown",
        )

        try:
            # 7. Bind the order to the tamper-evident chain: the order id is
            # signed into the metadata (payload_hash → signature → chain
            # hash) and indexed via request_id for lookup.
            event = await record_audit_event(
                event="acp_checkout_settled",
                wallet_id=agent_wallet_id,
                tool=ACP_CHECKOUT_TOOL,
                endpoint=ACP_CHECKOUT_ENDPOINT,
                key_id=key_id,
                request_id=audit_request_id(order_id),
                ok=True,
                metadata={
                    "order_id": order_id,
                    "intent_id": request.intent_id,
                    "merchant_domain": request.merchant_domain,
                    "derived_total_minor": total_minor,
                    "derived_total": derived_total,
                    "currency": currency,
                    "credits": str(credits),
                    "permit_id": permit.permit_id,
                    "stripe_payment_intent_id": charge.get("payment_intent_id"),
                    "stripe_idempotency_key": stripe_idem_key,
                },
            )

            # 8. Signed receipt; ledger_entry_id is None because no real
            # ledger entry exists (the adapter never mints or debits
            # credits). The idempotency record link makes the receipt itself
            # replay-safe.
            receipt = await get_receipt_service().create_receipt(
                permit_id=permit.permit_id,
                wallet_id=agent_wallet_id,
                key_id=key_id,
                tool=ACP_CHECKOUT_TOOL,
                request_payload={
                    "intent_id": request.intent_id,
                    "merchant_domain": request.merchant_domain,
                    "line_items_digest": line_items_digest(request.line_items),
                    "client_total": request.client_total,
                },
                response_payload={
                    "order_id": order_id,
                    "stripe_payment_intent_id": charge.get("payment_intent_id"),
                    "payment_status": charge.get("status"),
                    "derived_total": derived_total,
                    "currency": currency,
                },
                ledger_entry_id=None,
                credits_authorized=credits,
                credits_charged=credits,
                outcome="success",
                audit_event_id=event.event_id,
                idempotency_record_id=begun.record_id,
            )

        except ACPBridgeError as exc:
            await _record_rollback_evidence("acp_settlement_record_failed", exc)
            await _rollback()
            raise
        except Exception as exc:
            await _record_rollback_evidence("acp_settlement_record_failed", exc)
            await _rollback()
            raise ACPBridgeError("acp_settlement_record_failed") from exc

        response = ACPCheckoutResponse(
            order_id=order_id,
            intent_id=request.intent_id,
            permit_id=permit.permit_id,
            receipt_id=receipt.receipt_id,
            audit_event_id=event.event_id,
            derived_total=derived_total,
            status="settled",
        )
        # 9. Complete the intent's idempotency record so a replayed intent_id
        # returns this exact result without charging again. Deliberately
        # outside the rollback guard: once the charge, audit event, and
        # receipt exist the checkout IS settled, and releasing the budget
        # over a bookkeeping failure here would fabricate un-settled state.
        await idem.complete(
            wallet_id=agent_wallet_id,
            endpoint=ACP_CHECKOUT_ENDPOINT,
            idempotency_key=request.intent_id,
            response_reference=receipt.receipt_id,
            response_json=response.model_dump(mode="json"),
            status_code=200,
        )
        return response


_adapter: ACPCommerceAdapter | None = None


def get_acp_commerce_adapter() -> ACPCommerceAdapter:
    global _adapter
    if _adapter is None:
        _adapter = ACPCommerceAdapter()
    return _adapter
