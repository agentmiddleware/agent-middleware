"""Trust-plane facade: the buyer-facing evidence bundle.

Assembles the cryptographic evidence that ties a receipt to its permit, ledger
entry, and audit event, and verifies each link. Two shapes are produced:

- :func:`build_receipt_evidence` -> :class:`ReceiptEvidenceResponse`, the
  detailed ``checks`` list used by ``GET /v1/receipts/{id}/evidence``.
- :func:`build_evidence_bundle` -> :class:`EvidenceBundleResponse`, the flat
  buyer-facing artifact (``verification`` map) served at
  ``GET /v1/evidence/{receipt_id}``.

The verification logic lives here once; both endpoints call it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from app.core.auth import AuthContext
from app.db.converters import ledger_entry_model_to_schema
from app.db.database import get_session_factory
from app.db.models import (
    ControlPlaneAuditEventModel,
    LedgerEntryModel,
    McpDispatchAttemptModel,
    PermitModel,
)
from app.schemas.audit import AuditEventResponse
from app.schemas.billing import LedgerAction
from app.schemas.trust import (
    AuditChainVerifyResponse,
    DispatchEvidenceResponse,
    EvidenceBundleResponse,
    PermitResponse,
    ReceiptEvidenceCheck,
    ReceiptEvidenceResponse,
    ReceiptResponse,
)
from app.services.audit_chain import verify_audit_chain
from app.services.permits import get_permit_service, permit_model_to_response
from app.services.receipts import get_receipt_service


_DISPATCH_OUTCOMES = {
    "succeeded": frozenset({"success"}),
    # A failed refund leaves a deliberately charged failed_unrefunded receipt
    # for operator reconciliation; it is still the same returned_error dispatch.
    "returned_error": frozenset({"failed_refunded", "failed_unrefunded"}),
    "delivery_uncertain": frozenset({"delivery_uncertain"}),
    "response_rejected": frozenset({"response_rejected"}),
}
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _check(
    name: str,
    passed: bool,
    *,
    reason: str | None = None,
    details: dict | None = None,
) -> ReceiptEvidenceCheck:
    return ReceiptEvidenceCheck(
        name=name,
        status="passed" if passed else "failed",
        reason=None if passed else reason,
        details=details or {},
    )


def _skipped(
    name: str,
    reason: str,
    *,
    details: dict | None = None,
) -> ReceiptEvidenceCheck:
    return ReceiptEvidenceCheck(
        name=name,
        status="skipped",
        reason=reason,
        details=details or {},
    )


def _metadata_from_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _audit_event_response(
    event: ControlPlaneAuditEventModel,
) -> AuditEventResponse:
    return AuditEventResponse(
        event_id=event.event_id,
        created_at=event.created_at,
        event=event.event,
        wallet_id=event.wallet_id,
        tool=event.tool,
        endpoint=event.endpoint,
        auth_source=event.auth_source,
        key_id=event.key_id,
        policy_decision_id=event.policy_decision_id,
        request_id=event.request_id,
        ok=event.ok,
        error=event.error,
        metadata=_metadata_from_json(event.metadata_json),
        payload_hash=event.payload_hash,
        previous_hash=event.previous_hash,
        chain_hash=event.chain_hash,
        signature=event.signature,
        signature_key_id=event.signature_key_id,
    )


async def _get_permit_model(
    *,
    permit_id: str,
    receipt_wallet_id: str,
) -> PermitModel | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(PermitModel).where(
                cast(ColumnElement[bool], PermitModel.permit_id == permit_id),
                cast(
                    ColumnElement[bool],
                    PermitModel.subject_wallet_id == receipt_wallet_id,
                ),
            )
        )
        return result.scalar_one_or_none()


async def _get_audit_event_model(
    *,
    audit_event_id: str,
    receipt_wallet_id: str,
) -> ControlPlaneAuditEventModel | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ControlPlaneAuditEventModel).where(
                cast(
                    ColumnElement[bool],
                    ControlPlaneAuditEventModel.event_id == audit_event_id,
                ),
                cast(
                    ColumnElement[bool],
                    ControlPlaneAuditEventModel.wallet_id == receipt_wallet_id,
                ),
            )
        )
        return result.scalar_one_or_none()


async def _get_ledger_entry_model(
    *,
    ledger_entry_id: str,
    receipt_wallet_id: str,
) -> LedgerEntryModel | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(LedgerEntryModel).where(
                cast(ColumnElement[bool], LedgerEntryModel.entry_id == ledger_entry_id),
                cast(
                    ColumnElement[bool],
                    LedgerEntryModel.wallet_id == receipt_wallet_id,
                ),
            )
        )
        return result.scalar_one_or_none()


async def _get_dispatch_attempt_model(
    *,
    dispatch_attempt_id: str,
    receipt_wallet_id: str,
) -> McpDispatchAttemptModel | None:
    """Load dispatch state through the receipt's tenant boundary.

    Filtering by wallet in SQL mirrors the permit, audit, and ledger lookups
    above: a tampered cross-wallet link is indistinguishable from a missing
    attempt and cannot disclose the other wallet's dispatch metadata.
    """
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(McpDispatchAttemptModel).where(
                cast(
                    ColumnElement[bool],
                    McpDispatchAttemptModel.attempt_id == dispatch_attempt_id,
                ),
                cast(
                    ColumnElement[bool],
                    McpDispatchAttemptModel.wallet_id == receipt_wallet_id,
                ),
            )
        )
        return result.scalar_one_or_none()


def _dispatch_linkage_failures(
    *,
    attempt: McpDispatchAttemptModel,
    receipt: ReceiptResponse,
) -> list[str]:
    """Verify every signed receipt invariant that dispatch state must share."""
    failures: list[str] = []
    if attempt.wallet_id != receipt.wallet_id:
        failures.append("dispatch_wallet_mismatch")
    if attempt.permit_id != receipt.permit_id:
        failures.append("dispatch_permit_mismatch")
    if attempt.key_id != receipt.key_id:
        failures.append("dispatch_key_mismatch")
    if attempt.public_tool_id != receipt.tool:
        failures.append("dispatch_tool_mismatch")
    if attempt.request_hash != receipt.request_hash:
        failures.append("dispatch_request_hash_mismatch")
    if attempt.ledger_entry_id != receipt.ledger_entry_id:
        failures.append("dispatch_ledger_entry_mismatch")
    if attempt.response_hash != receipt.response_hash:
        failures.append("dispatch_response_hash_mismatch")
    if attempt.idempotency_record_id != receipt.idempotency_record_id:
        failures.append("dispatch_idempotency_mismatch")
    if attempt.credits_authorized != receipt.credits_authorized:
        failures.append("dispatch_authorized_credits_mismatch")

    expected_outcomes = _DISPATCH_OUTCOMES.get(attempt.state)
    if expected_outcomes is None:
        failures.append("dispatch_state_not_terminal")
    elif receipt.outcome not in expected_outcomes:
        failures.append("dispatch_outcome_mismatch")

    if attempt.state == "returned_error" and receipt.outcome == "failed_refunded":
        # The attempt records the original debit. A failed-refunded receipt
        # records zero net charge; ledger_linkage separately proves the refund.
        if receipt.credits_charged != Decimal("0"):
            failures.append("dispatch_refunded_credits_mismatch")
    elif attempt.credits_charged != receipt.credits_charged:
        failures.append("dispatch_charged_credits_mismatch")
    return failures


def _dispatch_audit_linkage_failures(
    *,
    attempt: McpDispatchAttemptModel,
    metadata: dict,
) -> list[str]:
    """Bind persisted dispatch state to the signed audit-event metadata.

    The audit chain authenticates ``metadata_json``.  These comparisons make
    that signature meaningful for remote dispatch evidence by proving the
    signed dispatch identity and terminal result describe the same persisted
    attempt linked by the receipt.
    """
    expected = (
        (
            "dispatch_attempt_id",
            attempt.attempt_id,
            "audit_dispatch_attempt_mismatch",
        ),
        ("dispatch_state", attempt.state, "audit_dispatch_state_mismatch"),
        (
            "upstream_tool_name",
            attempt.upstream_tool_name,
            "audit_upstream_tool_mismatch",
        ),
        (
            "upstream_origin",
            attempt.upstream_origin,
            "audit_upstream_origin_mismatch",
        ),
        (
            "dispatch_response_hash",
            attempt.response_hash,
            "audit_dispatch_response_hash_mismatch",
        ),
        ("permit_id", attempt.permit_id, "audit_dispatch_permit_mismatch"),
        (
            "request_hash",
            attempt.request_hash,
            "audit_dispatch_request_hash_mismatch",
        ),
        (
            "ledger_entry_id",
            attempt.ledger_entry_id,
            "audit_dispatch_ledger_entry_mismatch",
        ),
    )
    return [
        failure
        for field, expected_value, failure in expected
        if metadata.get(field) != expected_value
    ]


def _dispatch_evidence(
    attempt: McpDispatchAttemptModel,
) -> DispatchEvidenceResponse:
    """Project only bounded, non-secret dispatch metadata into evidence."""
    parsed_origin = urlsplit(attempt.upstream_origin)
    hostname = parsed_origin.hostname
    sanitized_origin = "invalid"
    if parsed_origin.scheme in {"http", "https"} and hostname:
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parsed_origin.port
        except ValueError:
            port = None
        sanitized_origin = f"{parsed_origin.scheme}://{host}"
        if port is not None:
            sanitized_origin = f"{sanitized_origin}:{port}"
    error_code = attempt.error_code
    if error_code is not None and not _SAFE_ERROR_CODE.fullmatch(error_code):
        error_code = "upstream_error"
    return DispatchEvidenceResponse(
        attempt_id=attempt.attempt_id,
        state=attempt.state,
        public_tool_id=attempt.public_tool_id,
        upstream_tool_name=attempt.upstream_tool_name,
        upstream_origin=sanitized_origin,
        request_hash=attempt.request_hash,
        response_hash=attempt.response_hash,
        ledger_entry_id=attempt.ledger_entry_id,
        credits_authorized=attempt.credits_authorized,
        credits_charged=attempt.credits_charged,
        error_code=error_code,
        created_at=attempt.created_at,
        dispatched_at=attempt.dispatched_at,
        completed_at=attempt.completed_at,
    )


async def _refund_exists(
    *,
    wallet_id: str,
    ledger_entry_id: str,
) -> bool:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(LedgerEntryModel).where(
                cast(ColumnElement[bool], LedgerEntryModel.wallet_id == wallet_id),
                cast(
                    ColumnElement[bool],
                    LedgerEntryModel.action == LedgerAction.REFUND.value,
                ),
                cast(
                    ColumnElement[bool],
                    LedgerEntryModel.correlation_id == ledger_entry_id,
                ),
            )
        )
        return result.scalar_one_or_none() is not None


async def authorize_receipt_access(
    *,
    auth: AuthContext,
    receipt: ReceiptResponse,
) -> None:
    """Authorize read access to a receipt and its evidence.

    Bootstrap admins and the receipt's own wallet always pass; otherwise the
    caller must be the permit's issuer or subject wallet, or hold explicit
    access to the receipt's wallet.
    """
    if auth.is_bootstrap_admin:
        return
    if auth.wallet_id == receipt.wallet_id:
        return
    permit = await get_permit_service().get_permit(receipt.permit_id)
    if permit and auth.wallet_id in {
        permit.issuer_wallet_id,
        permit.subject_wallet_id,
    }:
        return
    auth.require_wallet_access(receipt.wallet_id)


async def build_receipt_evidence(
    *,
    receipt: ReceiptResponse,
    auth: AuthContext,
) -> ReceiptEvidenceResponse:
    """Assemble the detailed evidence bundle for an (already authorized) receipt."""
    checks: list[ReceiptEvidenceCheck] = [
        _check(
            "wallet_access",
            True,
            details={
                "auth_source": auth.source,
                "auth_wallet_id": auth.wallet_id,
                "bootstrap_admin": auth.is_bootstrap_admin,
            },
        )
    ]

    receipt_valid, receipt_reason, _ = await get_receipt_service().verify_receipt(
        receipt.receipt_id
    )
    checks.append(_check("receipt_signature", receipt_valid, reason=receipt_reason))

    permit: PermitResponse | None = None
    permit_model = await _get_permit_model(
        permit_id=receipt.permit_id,
        receipt_wallet_id=receipt.wallet_id,
    )
    if not permit_model:
        checks.append(_check("permit_exists", False, reason="permit_not_found"))
    else:
        permit = permit_model_to_response(permit_model)
        checks.append(_check("permit_exists", True))
        permit_signature_ok = await get_permit_service().verify_signature(permit_model)
        checks.append(
            _check(
                "permit_signature",
                permit_signature_ok,
                reason="permit_signature_invalid",
            )
        )
        checks.append(
            _check(
                "permit_wallet_binding",
                permit.subject_wallet_id == receipt.wallet_id,
                reason="permit_subject_wallet_mismatch",
                details={
                    "receipt_wallet_id": receipt.wallet_id,
                    "permit_subject_wallet_id": permit.subject_wallet_id,
                },
            )
        )
        tool_allowed = not permit.allowed_tools or receipt.tool in permit.allowed_tools
        required_scope = f"tool:{receipt.tool}:invoke"
        scope_allowed = (
            required_scope in permit.scopes and "billing:charge" in permit.scopes
        )
        checks.append(
            _check(
                "permit_tool_scope",
                tool_allowed and scope_allowed,
                reason="permit_scope_or_tool_mismatch",
                details={
                    "tool_allowed": tool_allowed,
                    "required_scope": required_scope,
                    "scope_allowed": scope_allowed,
                },
            )
        )

    dispatch_attempt = None
    if receipt.dispatch_attempt_id:
        dispatch_attempt = await _get_dispatch_attempt_model(
            dispatch_attempt_id=receipt.dispatch_attempt_id,
            receipt_wallet_id=receipt.wallet_id,
        )

    audit_event_payload = None
    audit_chain_payload = None
    if not receipt.audit_event_id:
        checks.append(
            _check("audit_event_linkage", False, reason="audit_event_id_missing")
        )
        checks.append(_skipped("request_hash", "audit_event_id_missing"))
    else:
        audit_event = await _get_audit_event_model(
            audit_event_id=receipt.audit_event_id,
            receipt_wallet_id=receipt.wallet_id,
        )
        if not audit_event:
            checks.append(
                _check("audit_event_linkage", False, reason="audit_event_not_found")
            )
            checks.append(_skipped("request_hash", "audit_event_not_found"))
        else:
            audit_event_response = _audit_event_response(audit_event)
            audit_event_payload = audit_event_response.model_dump(mode="json")
            metadata = audit_event_response.metadata
            request_hash_ok = metadata.get("request_hash") == receipt.request_hash
            linkage_failures = []
            if audit_event.wallet_id != receipt.wallet_id:
                linkage_failures.append("audit_wallet_mismatch")
            if audit_event.tool != receipt.tool:
                linkage_failures.append("audit_tool_mismatch")
            if metadata.get("permit_id") != receipt.permit_id:
                linkage_failures.append("audit_permit_mismatch")
            if not request_hash_ok:
                linkage_failures.append("audit_request_hash_mismatch")
            if receipt.ledger_entry_id and (
                metadata.get("ledger_entry_id") != receipt.ledger_entry_id
            ):
                linkage_failures.append("audit_ledger_entry_mismatch")
            if receipt.dispatch_attempt_id:
                if metadata.get("dispatch_attempt_id") != receipt.dispatch_attempt_id:
                    linkage_failures.append("audit_dispatch_attempt_mismatch")
                if dispatch_attempt is not None:
                    for failure in _dispatch_audit_linkage_failures(
                        attempt=dispatch_attempt,
                        metadata=metadata,
                    ):
                        if failure not in linkage_failures:
                            linkage_failures.append(failure)
            checks.append(
                _check(
                    "audit_event_linkage",
                    not linkage_failures,
                    reason=";".join(linkage_failures) if linkage_failures else None,
                    details={"audit_event_id": receipt.audit_event_id},
                )
            )
            # First-class request-hash check so the flat evidence bundle and the
            # detailed checks list never disagree about request integrity.
            checks.append(
                _check(
                    "request_hash",
                    request_hash_ok,
                    reason="audit_request_hash_mismatch",
                    details={
                        "receipt_request_hash": receipt.request_hash,
                        "audit_request_hash": metadata.get("request_hash"),
                    },
                )
            )

            chain = await verify_audit_chain(wallet_id=receipt.wallet_id)
            audit_chain_payload = AuditChainVerifyResponse(**asdict(chain))
            checks.append(
                _check(
                    "audit_chain",
                    chain.valid,
                    reason=chain.reason,
                    details={
                        "checked_events": chain.checked_events,
                        "broken_event_id": chain.broken_event_id,
                    },
                )
            )

    ledger_entry_payload = None
    if not receipt.ledger_entry_id:
        if receipt.credits_charged > Decimal("0"):
            checks.append(
                _check("ledger_linkage", False, reason="ledger_entry_id_missing")
            )
        else:
            checks.append(
                _skipped(
                    "ledger_linkage",
                    "receipt_has_no_charged_credits",
                    details={"credits_charged": str(receipt.credits_charged)},
                )
            )
    else:
        ledger_entry = await _get_ledger_entry_model(
            ledger_entry_id=receipt.ledger_entry_id,
            receipt_wallet_id=receipt.wallet_id,
        )
        if not ledger_entry:
            checks.append(_check("ledger_linkage", False, reason="ledger_not_found"))
        else:
            ledger_schema = ledger_entry_model_to_schema(ledger_entry)
            ledger_entry_payload = ledger_schema.model_dump(mode="json")
            ledger_failures = []
            if ledger_entry.wallet_id != receipt.wallet_id:
                ledger_failures.append("ledger_wallet_mismatch")
            if ledger_entry.action != LedgerAction.DEBIT.value:
                ledger_failures.append("ledger_action_not_debit")
            if receipt.credits_charged > Decimal("0"):
                expected_amount = -receipt.credits_charged
                if ledger_entry.amount != expected_amount:
                    ledger_failures.append("ledger_amount_mismatch")
            elif receipt.outcome == "failed_refunded":
                refund_found = await _refund_exists(
                    wallet_id=receipt.wallet_id,
                    ledger_entry_id=receipt.ledger_entry_id,
                )
                if not refund_found:
                    ledger_failures.append("refund_entry_not_found")
            checks.append(
                _check(
                    "ledger_linkage",
                    not ledger_failures,
                    reason=";".join(ledger_failures) if ledger_failures else None,
                    details={"ledger_entry_id": receipt.ledger_entry_id},
                )
            )

    dispatch_payload = None
    if not receipt.dispatch_attempt_id:
        checks.append(
            _skipped(
                "dispatch_linkage",
                "receipt_has_no_dispatch_attempt",
            )
        )
    else:
        if dispatch_attempt is None:
            checks.append(
                _check(
                    "dispatch_linkage",
                    False,
                    reason="dispatch_attempt_not_found",
                    details={"dispatch_attempt_id": receipt.dispatch_attempt_id},
                )
            )
        else:
            linkage_failures = _dispatch_linkage_failures(
                attempt=dispatch_attempt,
                receipt=receipt,
            )
            checks.append(
                _check(
                    "dispatch_linkage",
                    not linkage_failures,
                    reason=";".join(linkage_failures) if linkage_failures else None,
                    details={"dispatch_attempt_id": receipt.dispatch_attempt_id},
                )
            )
            # Keep the stored MCP result and idempotency key private. This
            # projection includes only bounded linkage/operational metadata.
            dispatch_payload = _dispatch_evidence(dispatch_attempt)

    return ReceiptEvidenceResponse(
        receipt_id=receipt.receipt_id,
        valid=all(check.status != "failed" for check in checks),
        checks=checks,
        receipt=receipt,
        permit=permit,
        audit_event=audit_event_payload,
        audit_chain=audit_chain_payload,
        ledger_entry=ledger_entry_payload,
        dispatch=dispatch_payload,
    )


def _verification_status(
    checks_by_name: dict[str, ReceiptEvidenceCheck],
    name: str,
) -> str:
    check = checks_by_name.get(name)
    if check is None:
        return "skipped"
    if check.status == "passed":
        return "ok"
    if check.status == "skipped":
        return "skipped"
    return check.reason or "failed"


def build_evidence_bundle(evidence: ReceiptEvidenceResponse) -> EvidenceBundleResponse:
    """Map the detailed evidence into the flat buyer-facing artifact."""
    checks_by_name = {check.name: check for check in evidence.checks}

    verification = {
        "receipt_signature": _verification_status(checks_by_name, "receipt_signature"),
        "permit_signature": _verification_status(checks_by_name, "permit_signature"),
        "audit_chain": _verification_status(checks_by_name, "audit_chain"),
        "request_hash": _verification_status(checks_by_name, "request_hash"),
        "dispatch_linkage": _verification_status(
            checks_by_name,
            "dispatch_linkage",
        ),
    }

    return EvidenceBundleResponse(
        receipt_id=evidence.receipt_id,
        valid=evidence.valid,
        receipt=evidence.receipt,
        permit=evidence.permit,
        ledger_entry=evidence.ledger_entry,
        audit_event=evidence.audit_event,
        dispatch=evidence.dispatch,
        verification=verification,
    )


__all__ = [
    "authorize_receipt_access",
    "build_receipt_evidence",
    "build_evidence_bundle",
]
