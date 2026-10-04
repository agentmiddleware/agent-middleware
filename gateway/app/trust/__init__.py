"""The trust plane — the frozen product spine of Agent Middleware API.

This package is a stable, import-light boundary over the trust-plane primitives
that constitute the core product:

    permits -> governed invocation -> idempotency -> wallet ledger (metering)
    -> receipts -> audit verification -> evidence

Everything outside this boundary is a protocol adapter or an example workload.
The modules here re-export the canonical implementations that live under
``app.services`` / ``app.policy`` so callers can depend on the product core
through one obvious namespace without coupling to internal module layout.

This is a facade, not a fork: it re-exports rather than reimplements. One
exception is worth naming plainly: ``McpGovernedAdapter`` (in ``.adapters``)
delegates the governed-invocation orchestration itself to
``app.routers.mcp._execute_registered_tool`` — that orchestration is not
duplicated under ``app.services`` today. See ``tests/test_trust_boundary.py``
and ``adapters.py`` for the documented reasoning.
"""

from __future__ import annotations

from .adapters import (
    GovernedInvocationAdapter,
    GovernedRequest,
    GovernedRequestInvalid,
    GovernedResult,
    McpGovernedAdapter,
    validate_tools_call_params,
)
from .approvals import (
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    ApprovalCheck,
    HumanApprovalError,
    HumanApprovalService,
    HumanApprovalUnavailableError,
    approval_window_seconds,
    get_human_approval_service,
    human_approval_available,
    human_approval_configured,
)
from .audit_chain import (
    AuditChainContendedError,
    AuditChainVerification,
    AuditEventConflictError,
    audit_payload,
    count_audit_events,
    count_audit_events_grouped,
    list_audit_events,
    record_audit_event,
    sign_audit_model,
    summarize_audit_events,
    verify_audit_chain,
)
from .evidence import (
    authorize_receipt_access,
    build_evidence_bundle,
    build_receipt_evidence,
)
from .idempotency import (
    GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
    MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH,
    IdempotencyBegin,
    IdempotencyConflictError,
    IdempotencyCreationDisabledError,
    IdempotencyInProgressError,
    IdempotencyReplay,
    IdempotencyService,
    InvalidIdempotencyKeyError,
    decode_idempotency_key_header,
    get_idempotency_service,
    resolve_client_idempotency_key,
    validate_client_idempotency_key,
)
from .metering import (
    DEFAULT_PRICING,
    AgentMoney,
    InsufficientFundsError,
    KYCVerificationRequiredError,
    WalletNotFoundError,
    get_agent_money,
)
from .permits import (
    PermitCreationRejectedError,
    PermitError,
    PermitService,
    PermitValidation,
    PermitWriteContendedError,
    get_permit_service,
    permit_constraints_snapshot,
    permit_model_to_response,
)
from .permit_requests import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_EXPIRED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_MINTING,
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_REJECTED,
    ApprovalCardView,
    PermitRequestConflictError,
    PermitRequestError,
    PermitRequestService,
    card_fragment_html,
    card_view,
    get_permit_request_service,
    permit_request_hash,
    poll_url,
    render_email_html,
    request_to_response,
    render_page_html,
    render_text_summary,
)
from .policy import (
    PolicyDecision,
    PolicyEvaluation,
    evaluate_governed_action,
    evaluate_tool_invocation,
    evaluate_wallet_policy,
    list_policy_bundles,
    record_governed_action,
    wallet_human_approval_required,
)
from .quotes import (
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
    charge_units_for,
    get_quote_service,
    quote_model_to_response,
    quote_ttl_seconds,
    tool_price,
)
from .receipts import (
    ReceiptError,
    ReceiptService,
    ReceiptWriteContendedError,
    get_receipt_service,
)
from .refund_reconciliation import (
    RefundReconciliationContendedError,
    RefundReconciliationError,
    RefundReconciliationService,
    build_pending_refund_reconciliation,
    get_refund_reconciliation_service,
)
from .signing import (
    RECEIPT_CANONICALIZATION,
    SigningKeyError,
    SigningKeyService,
    canonical_json,
    get_signing_key_service,
    sha256_hex,
)

__all__ = [
    "AuditChainContendedError",
    "AuditEventConflictError",
    # adapters
    "GovernedInvocationAdapter",
    "GovernedRequest",
    "GovernedRequestInvalid",
    "GovernedResult",
    "McpGovernedAdapter",
    "validate_tools_call_params",
    # human approval gate
    "APPROVAL_STATUS_APPROVED",
    "APPROVAL_STATUS_EXPIRED",
    "APPROVAL_STATUS_PENDING",
    "APPROVAL_STATUS_REJECTED",
    "ApprovalCheck",
    "HumanApprovalError",
    "HumanApprovalService",
    "HumanApprovalUnavailableError",
    "get_human_approval_service",
    "human_approval_available",
    "human_approval_configured",
    "approval_window_seconds",
    # permits
    "PermitCreationRejectedError",
    "PermitService",
    "PermitValidation",
    "PermitError",
    "PermitWriteContendedError",
    "get_permit_service",
    "permit_constraints_snapshot",
    "permit_model_to_response",
    # permit requests (agent asks, human approves, middleware mints)
    "ApprovalCardView",
    "PermitRequestConflictError",
    "PermitRequestError",
    "PermitRequestService",
    "REQUEST_STATUS_APPROVED",
    "REQUEST_STATUS_EXPIRED",
    "REQUEST_STATUS_FAILED",
    "REQUEST_STATUS_MINTING",
    "REQUEST_STATUS_PENDING",
    "REQUEST_STATUS_REJECTED",
    "card_fragment_html",
    "card_view",
    "get_permit_request_service",
    "permit_request_hash",
    "poll_url",
    "render_email_html",
    "request_to_response",
    "render_page_html",
    "render_text_summary",
    # signed price quotes
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
    # receipts
    "ReceiptService",
    "ReceiptError",
    "ReceiptWriteContendedError",
    "get_receipt_service",
    # failed-refund reconciliation
    "RefundReconciliationContendedError",
    "RefundReconciliationError",
    "RefundReconciliationService",
    "build_pending_refund_reconciliation",
    "get_refund_reconciliation_service",
    # signing (cryptographic root)
    "RECEIPT_CANONICALIZATION",
    "SigningKeyService",
    "SigningKeyError",
    "get_signing_key_service",
    "canonical_json",
    "sha256_hex",
    # idempotency
    "GOVERNED_MCP_IDEMPOTENCY_ENDPOINT",
    "MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH",
    "IdempotencyBegin",
    "IdempotencyService",
    "IdempotencyReplay",
    "IdempotencyConflictError",
    "IdempotencyCreationDisabledError",
    "IdempotencyInProgressError",
    "InvalidIdempotencyKeyError",
    "decode_idempotency_key_header",
    "get_idempotency_service",
    "resolve_client_idempotency_key",
    "validate_client_idempotency_key",
    # audit
    "verify_audit_chain",
    "sign_audit_model",
    "audit_payload",
    "AuditChainVerification",
    "record_audit_event",
    "list_audit_events",
    "count_audit_events",
    "count_audit_events_grouped",
    "summarize_audit_events",
    # policy
    "evaluate_tool_invocation",
    "evaluate_governed_action",
    "evaluate_wallet_policy",
    "list_policy_bundles",
    "record_governed_action",
    "wallet_human_approval_required",
    "PolicyDecision",
    "PolicyEvaluation",
    # metering
    "AgentMoney",
    "get_agent_money",
    "DEFAULT_PRICING",
    "InsufficientFundsError",
    "KYCVerificationRequiredError",
    "WalletNotFoundError",
    # evidence
    "authorize_receipt_access",
    "build_receipt_evidence",
    "build_evidence_bundle",
]
