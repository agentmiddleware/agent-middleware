"""Trust-plane facade: policy-constrained governance.

Re-exports the canonical policy-decision functions from
:mod:`app.policy.decisions`, the wallet-policy evaluation and bundle helpers
from :mod:`app.services.policies`, the governed-action recorder from
:mod:`app.services.governance`, and the enterprise IGA evaluation surface
(OIDC group/role -> PolicyBundle enforcement) from :mod:`app.core.oidc_iga`.
"""

from __future__ import annotations

from app.core.oidc_iga import (
    EnterprisePrincipal,
    IGADecision,
    IGAError,
    IGAGrant,
    enforce_tool_call,
    parse_enterprise_token,
    resolve_policy_grants,
)
from app.policy.decisions import (
    PolicyDecision,
    evaluate_governed_action,
    evaluate_tool_invocation,
)
from app.services.governance import record_governed_action
from app.services.policies import (
    PolicyEvaluation,
    evaluate_wallet_policy,
    list_policy_bundles,
    wallet_human_approval_required,
)

__all__ = [
    "EnterprisePrincipal",
    "IGADecision",
    "IGAError",
    "IGAGrant",
    "PolicyDecision",
    "PolicyEvaluation",
    "enforce_tool_call",
    "evaluate_tool_invocation",
    "evaluate_governed_action",
    "evaluate_wallet_policy",
    "list_policy_bundles",
    "parse_enterprise_token",
    "record_governed_action",
    "resolve_policy_grants",
    "wallet_human_approval_required",
]
