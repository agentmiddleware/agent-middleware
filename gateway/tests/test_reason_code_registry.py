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
        "action_binding_mismatch",
        "action_permit_denied",
        "action_permit_required",
        "action_quote_unsupported",
        "action_tool_binding_required",
        "daily_spend_limit_exceeded",
        "daily_spend_unknown",
        "delivery_uncertain",
        "demo_auto_permit_budget_exceeded",
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
        "idempotency_key_required_for_human_approval",
        "idempotency_key_reused",
        "insufficient_funds",
        "key_tool_not_allowed",
        "max_cost_per_action_exceeded",
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
        "risk_tier_not_allowed",
        "service_category_not_allowed",
        "tool_execution_failed",
        "tool_not_allowed",
        "upstream_pre_dispatch_failed",
        "upstream_prepare_failed",
        "upstream_returned_error",
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
    """String constants in ``node``, including ``x or "fallback"`` forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BoolOp):
        return [s for value in node.values for s in _string_literals(value)]
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
                name in _REFUSAL_CALLS
                and len(node.args) >= 2
                and _is_false(node.args[0])
            ):
                reason = node.args[1]
            elif name == "PolicyDecision":
                kwargs = {kw.arg: kw.value for kw in node.keywords}
                if _is_false(kwargs.get("allowed")):
                    reason = kwargs.get("reason")
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
        codes.update(re.findall(r"`([a-z_<>]+)`", row))
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
    unregistered = {
        literal: where
        for literal, where in found.items()
        if not is_refusal_reason_code(literal)
    }
    assert not unregistered, (
        f"refusal reason literals missing from app/core/reason_codes.py: {unregistered}"
    )


def test_prefixed_detail_resolves_to_registered_code() -> None:
    assert is_refusal_reason_code("wallet_scoped_key_required: use a wallet key")
    assert not is_refusal_reason_code("no_such_reason_code")
