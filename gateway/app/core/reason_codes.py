"""Registry of stable refusal reason codes.

Every machine-readable reason a governed call can be refused with is listed
here. These strings are a public contract: they appear as the JSON-RPC error
message, in REST ``detail.error``, and as the signed ``reason_code`` on denial
receipts, so an agent can branch on them. Values must never be renamed; add a
new code instead and document it in ``docs/denial-details.md``.

``tests/test_reason_code_registry.py`` pins this set, checks it against the
documentation, and scans ``app/`` so a refusal literal that is not registered
fails CI.
"""

from __future__ import annotations

#: Non-active permit statuses that surface as ``permit_<status>``.
PERMIT_STATUS_REASON_CODES: frozenset[str] = frozenset({"permit_revoked"})

#: Permit validation and recipient binding.
PERMIT_REASON_CODES: frozenset[str] = frozenset(
    {
        "permit_aggregate_value_cap_exceeded",
        "permit_budget_exceeded",
        "permit_constraint_unsupported_for_upstream",
        "permit_credits_invalid",
        "permit_denied",
        "permit_expired",
        "permit_forbidden_field",
        "permit_key_mismatch",
        "permit_max_calls_exceeded",
        "permit_not_found",
        "permit_recipient_domain_mismatch",
        "permit_scope_missing",
        "permit_signature_invalid",
        "permit_tool_not_allowed",
        "permit_wallet_mismatch",
        "duplicate_request_new_key",
    }
)

#: Action-bound tools (permits tied to an upstream action binding).
ACTION_REASON_CODES: frozenset[str] = frozenset(
    {
        "action_binding_mismatch",
        "action_permit_denied",
        "action_permit_required",
        "action_quote_unsupported",
        "action_tool_binding_required",
    }
)

#: Caller credential, tenant, and request-shape admission.
ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        "demo_auto_permit_budget_exceeded",
        "demo_tool_not_allowed",
        "idempotency_key_required_for_human_approval",
        "idempotency_key_reused",
        "key_tool_not_allowed",
        "wallet_access_denied",
        "wallet_scoped_key_required",
    }
)

#: Wallet state and standing wallet policy.
POLICY_REASON_CODES: frozenset[str] = frozenset(
    {
        "daily_spend_limit_exceeded",
        "daily_spend_unknown",
        "human_approval_required",
        "insufficient_funds",
        "max_cost_per_action_exceeded",
        "policy_constraint_corrupt",
        "policy_cost_invalid",
        "policy_denied",
        "real_effects_required",
        "risk_tier_not_allowed",
        "service_category_not_allowed",
        "tool_not_allowed",
        "wallet_expired",
        "wallet_frozen",
    }
)

#: Terminal human-approval outcomes.
APPROVAL_REASON_CODES: frozenset[str] = frozenset(
    {
        "human_approval_consumed",
        "human_approval_expired",
        "human_approval_not_configured",
        "human_approval_rejected",
        "human_approval_request_mismatch",
        "human_approval_request_rejected",
    }
)

#: Dispatch, tool execution, and refund outcomes.
DISPATCH_REASON_CODES: frozenset[str] = frozenset(
    {
        "delivery_uncertain",
        "failed_refunded",
        "refund_failed",
        "response_rejected",
        "tool_execution_failed",
        "upstream_pre_dispatch_failed",
        "upstream_prepare_failed",
        "upstream_returned_error",
    }
)

REFUSAL_REASON_CODES: frozenset[str] = (
    PERMIT_STATUS_REASON_CODES
    | PERMIT_REASON_CODES
    | ACTION_REASON_CODES
    | ADMISSION_REASON_CODES
    | POLICY_REASON_CODES
    | APPROVAL_REASON_CODES
    | DISPATCH_REASON_CODES
)


def is_refusal_reason_code(value: str) -> bool:
    """True when ``value`` (or its ``code: detail`` prefix) is registered."""
    return value.split(":", 1)[0].strip() in REFUSAL_REASON_CODES
