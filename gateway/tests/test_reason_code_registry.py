"""The refusal reason-code registry is pinned, documented, and complete."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.core.reason_codes import REFUSAL_REASON_CODES, is_refusal_reason_code

GATEWAY = Path(__file__).resolve().parents[1]
APP = GATEWAY / "app"
DOC = GATEWAY / "docs" / "denial-details.md"

#: Pinned values. Changing a string here is a breaking API change for every
#: agent that branches on it; add a new code instead of editing one.
PINNED = frozenset(
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
        "action_permit_requires_trusted_issuance",
        "action_quote_unsupported",
        "action_tool_binding_required",
        "aggregate_value_cap_not_storable",
        "daily_spend_limit_exceeded",
        "daily_spend_unknown",
        "delivery_uncertain",
        "demo_auto_permit_budget_exceeded",
        "demo_permit_out_of_bounds",
        "demo_tool_not_allowed",
        "duplicate_request_new_key",
        "failed_refunded",
        "human_approval_consumed",
        "human_approval_expired",
        "human_approval_not_configured",
        "human_approval_rejected",
        "human_approval_request_mismatch",
        "human_approval_request_rejected",
        "human_approval_required",
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
        "idempotency_key_blank",
        "idempotency_key_conflict",
        "idempotency_key_control_characters",
        "idempotency_key_not_a_string",
        "idempotency_key_not_utf8",
        "idempotency_key_required",
        "idempotency_key_required_for_human_approval",
        "idempotency_key_reused",
        "idempotency_key_too_long",
        "insufficient_funds",
        "invalid_action_binding",
        "invalid_action_digest",
        "invalid_action_permit_scope",
        "invalid_action_schema",
        "issuer_wallet_not_found",
        "key_tool_not_allowed",
        "max_credits_must_be_positive",
        "max_credits_not_storable",
        "max_cost_per_action_exceeded",
        "permit_aggregate_value_cap_exceeded",
        "permit_budget_exceeds_wallet_balance",
        "permit_budget_exceeded",
        "permit_constraint_unsupported_for_upstream",
        "permit_credits_invalid",
        "permit_denied",
        "permit_expired",
        "permit_expired_at_creation",
        "permit_forbidden_field",
        "permit_key_mismatch",
        "permit_max_calls_exceeded",
        "permit_not_found",
        "permit_recipient_domain_mismatch",
        "permit_revoked",
        "permit_scope_missing",
        "permit_signature_invalid",
        "permit_tool_not_allowed",
        "permit_wallet_mismatch",
        "policy_constraint_corrupt",
        "policy_cost_invalid",
        "policy_denied",
        "real_effects_required",
        "refund_failed",
        "response_rejected",
        "repeat_window_issuance_disabled",
        "risk_tier_not_allowed",
        "service_category_not_allowed",
        "tool_execution_failed",
        "tool_not_allowed",
        "subject_wallet_access_denied",
        "subject_wallet_not_found",
        "upstream_pre_dispatch_failed",
        "upstream_prepare_failed",
        "upstream_returned_error",
        "unsupported_action_constraints",
        "unsupported_action_contract_version",
        "unsupported_action_schema",
        "wallet_access_denied",
        "wallet_expired",
        "wallet_frozen",
        "wallet_scoped_key_required",
    }
)

#: Constructors whose string argument is a caller-visible refusal reason.
_REFUSAL_CALLS = {"ToolPermissionDenied", "PermitValidation", "PolicyEvaluation"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_literals(node: ast.AST | None) -> list[str]:
    """String constants in ``node``, including fallback and conditional forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            prefix, separator, _ = first.value.partition(":")
            if separator and prefix:
                return [prefix.strip()]
    if isinstance(node, ast.BoolOp):
        return [s for value in node.values for s in _string_literals(value)]
    if isinstance(node, ast.IfExp):
        return _string_literals(node.body) + _string_literals(node.orelse)
    return []


def _is_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _scan_refusal_literals() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            reason: ast.AST | None = None
            if name == "ToolPermissionDenied" and node.args:
                reason = node.args[0]
            elif (
                name in {"PermitCreationRejectedError", "InvalidIdempotencyKeyError"}
                and node.args
            ):
                reason = node.args[0]
            elif (
                name in _REFUSAL_CALLS
                and len(node.args) >= 2
                and _is_false(node.args[0])
            ):
                reason = node.args[1]
            elif name == "PolicyDecision":
                kwargs = {kw.arg: kw.value for kw in node.keywords}
                if _is_false(kwargs.get("allowed")):
                    reason = kwargs.get("reason")
            elif name == "IGAError" and node.args:
                reason = node.args[0]
            elif name == "IGADecision":
                kwargs = {kw.arg: kw.value for kw in node.keywords}
                allowed = node.args[0] if node.args else kwargs.get("allowed")
                if _is_false(allowed):
                    reason = (
                        node.args[1] if len(node.args) >= 2 else kwargs.get("reason")
                    )
            elif (
                name == "ValueError"
                and path == APP / "services" / "action_permits.py"
                and node.args
            ):
                reason = node.args[0]
            elif name == "_mcp_error" and len(node.args) >= 2:
                code = node.args[0]
                if (
                    isinstance(code, ast.UnaryOp)
                    and isinstance(code.operand, ast.Constant)
                    and code.operand.value == 32003
                ):
                    reason = node.args[1]
            for literal in _string_literals(reason):
                rel = path.relative_to(GATEWAY)
                found.setdefault(literal, f"{rel}:{node.lineno}")
    return found


def _documented_codes() -> set[str]:
    text = DOC.read_text(encoding="utf-8")
    codes: set[str] = set()
    for row in re.findall(r"^\| (`[^|]+)\|", text, re.MULTILINE):
        codes.update(re.findall(r"`([a-z0-9_<>]+)`", row))
    return codes


def test_registry_values_are_pinned() -> None:
    assert REFUSAL_REASON_CODES == PINNED


def test_registry_values_are_machine_codes() -> None:
    for code in REFUSAL_REASON_CODES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code), code


def test_every_registered_code_is_documented() -> None:
    documented = _documented_codes()
    # The doc lists non-active statuses as the ``permit_<status>`` pattern.
    status_codes = {c for c in REFUSAL_REASON_CODES if c == "permit_revoked"}
    missing = REFUSAL_REASON_CODES - documented - status_codes
    assert not missing, f"registered but undocumented: {sorted(missing)}"
    assert "permit_<status>" in documented


def test_every_documented_code_is_registered() -> None:
    documented = _documented_codes() - {"permit_<status>"}
    unregistered = documented - REFUSAL_REASON_CODES
    assert not unregistered, f"documented but unregistered: {sorted(unregistered)}"


def test_every_refusal_literal_in_app_is_registered() -> None:
    found = _scan_refusal_literals()
    assert "permit_tool_not_allowed" in found  # the scan sees real call sites
    assert "permit_forbidden_field" in found  # static prefix of a real f-string
    assert "permit_budget_exceeds_wallet_balance" in found
    assert "idempotency_key_conflict" in found
    assert "action_payload_mismatch" in found
    assert "iga_no_matching_role" in found
    unregistered = {
        literal: where
        for literal, where in found.items()
        if not is_refusal_reason_code(literal)
    }
    assert not unregistered, (
        f"refusal reason literals missing from app/core/reason_codes.py: {unregistered}"
    )


def test_conditional_refusal_literals_are_scanned() -> None:
    reason = ast.parse('"new_code" if flag else "policy_denied"', mode="eval").body
    assert _string_literals(reason) == ["new_code", "policy_denied"]


def test_prefixed_detail_resolves_to_registered_code() -> None:
    assert is_refusal_reason_code("wallet_scoped_key_required: use a wallet key")
    assert not is_refusal_reason_code("no_such_reason_code")
