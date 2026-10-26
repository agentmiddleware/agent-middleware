"""Registry of stable refusal reason codes.

This catalog covers governed denials and the related permit-issuance and
idempotency admission reasons documented in ``docs/denial-details.md``.
Generic API errors such as ``invalid_api_key``, ``rate_limited``, and
``internal_error`` are outside this catalog. Terminal permit-bound denials
can sign ``reason_code`` on a receipt; earlier API errors have no receipt.
Keep registered values stable:
add a new code instead of renaming one.

``tests/test_reason_code_registry.py`` pins this set, checks it against the
documentation, and scans known refusal producers for unregistered literals.
"""

from __future__ import annotations

#: Non-active permit statuses that surface as ``permit_<status>``.
PERMIT_STATUS_REASON_CODES: frozenset[str] = frozenset({"permit_revoked"})

#: Permit creation refusals returned by the permit API or auto-mint path.
PERMIT_ISSUANCE_REASON_CODES: frozenset[str] = frozenset(
    {
        "action_permit_requires_trusted_issuance",
        "aggregate_value_cap_not_storable",
        "demo_permit_out_of_bounds",
        "issuer_wallet_not_found",
        "max_credits_must_be_positive",
        "max_credits_not_storable",
        "permit_budget_exceeds_wallet_balance",
        "permit_expired_at_creation",
        "repeat_window_issuance_disabled",
        "subject_wallet_access_denied",
        "subject_wallet_not_found",
    }
)

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
        "action_argument_const",
        "action_argument_constraint",
        "action_argument_enum",
        "action_argument_required",
        "action_argument_type_mismatch",
        "action_argument_unknown_property",
        "action_arguments_must_be_object",
        "action_arguments_must_be_strict_json",
        "action_binding_mismatch",
        "action_payload_mismatch",
        "action_permit_denied",
        "action_permit_required",
        "action_quote_unsupported",
        "action_tool_binding_required",
        "invalid_action_binding",
        "invalid_action_digest",
        "invalid_action_permit_scope",
        "invalid_action_schema",
        "unsupported_action_constraints",
        "unsupported_action_contract_version",
        "unsupported_action_schema",
    }
)

#: Enterprise identity and group-policy admission.
IGA_REASON_CODES: frozenset[str] = frozenset(
    {
        "iga_algorithm_not_allowed",
        "iga_audience_mismatch",
        "iga_config_invalid",
        "iga_issuer_not_trusted",
        "iga_max_uses_exceeded",
        "iga_no_matching_role",
        "iga_policy_inactive",
        "iga_policy_not_found",
        "iga_signature_invalid",
        "iga_signing_key_not_found",
        "iga_token_expired",
        "iga_token_invalid",
        "iga_token_malformed",
        "iga_token_not_yet_valid",
        "iga_tool_not_allowed",
        "iga_velocity_exceeded",
    }
)

#: Caller credential, tenant, and request-shape admission.
ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        "demo_auto_permit_budget_exceeded",
        "demo_tool_not_allowed",
        "idempotency_key_blank",
        "idempotency_key_conflict",
        "idempotency_key_control_characters",
        "idempotency_key_not_a_string",
        "idempotency_key_not_utf8",
        "idempotency_key_required_for_human_approval",
        "idempotency_key_reused",
        "idempotency_key_too_long",
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
    | PERMIT_ISSUANCE_REASON_CODES
    | PERMIT_REASON_CODES
    | ACTION_REASON_CODES
    | IGA_REASON_CODES
    | ADMISSION_REASON_CODES
    | POLICY_REASON_CODES
    | APPROVAL_REASON_CODES
    | DISPATCH_REASON_CODES
)


def is_refusal_reason_code(value: str) -> bool:
    """True when ``value`` (or its ``code: detail`` prefix) is registered."""
    return value.split(":", 1)[0].strip() in REFUSAL_REASON_CODES
