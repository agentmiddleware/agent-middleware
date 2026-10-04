"""Versioned positioning metadata shared by public discovery surfaces.

The pre-existing ``product_wedge`` and ``product_loop`` fields remain stable
for discovery clients that compare them literally.  New clients should use the
versioned ``positioning`` object returned by :func:`get_product_positioning`.
"""

from __future__ import annotations

from typing import Any


POSITIONING_SCHEMA_VERSION = "1.0"
POSITIONING_EFFECTIVE_DATE = "2026-08-28"
POSITIONING_ID = "transaction_integrity_for_consequential_autonomous_actions"
POSITIONING_LABEL = "Transaction integrity for consequential autonomous actions"
POSITIONING_TAGLINE = "Make consequential agent actions transactional."
POSITIONING_DESCRIPTION = (
    "Transaction-integrity boundary for consequential autonomous actions on the "
    "configured upstream-MCP path: one logical action binds scoped authority and "
    "configured consumption to at most one gateway dispatch and debit, records "
    "delivery_uncertain instead of automatically redispatching, and links gateway "
    "evidence for required external reconciliation."
)
POSITIONING_CLAIM_BOUNDARY = (
    "gateway_state_machine_not_distributed_acid_or_downstream_effect_proof"
)
POSITIONING_SEMANTICS = (
    "logical_action_identity",
    "bounded_authority_consumption",
    "at_most_one_gateway_dispatch_and_debit",
    "delivery_uncertain_no_automatic_redispatch",
    "linked_gateway_evidence",
    "authoritative_external_reconciliation_required",
)
POSITIONING_CANONICAL_LOOP = (
    "logical_action_identity",
    "authorize",
    "reserve_configured_allowance",
    "debit",
    "claim_gateway_dispatch",
    "confirmed_outcome_or_delivery_uncertain",
    "linked_receipt_and_audit",
    "authoritative_external_reconciliation_required",
)

# Compatibility-only v1 identifiers. Do not present these as the current
# category; exact-match consumers still rely on them while they migrate.
LEGACY_PRODUCT_WEDGE = "governed_mcp_trust_plane"
LEGACY_MCP_SERVER_NAME = "Agent Middleware MCP Trust Plane"
LEGACY_PRODUCT_LOOP = (
    "discover",
    "authenticate",
    "authorize",
    "invoke",
    "meter",
    "receipt",
    "audit",
    "govern",
)


def get_product_positioning() -> dict[str, Any]:
    """Return a fresh copy of the canonical machine-readable positioning."""

    return {
        "schema_version": POSITIONING_SCHEMA_VERSION,
        "effective_date": POSITIONING_EFFECTIVE_DATE,
        "id": POSITIONING_ID,
        "label": POSITIONING_LABEL,
        "tagline": POSITIONING_TAGLINE,
        "legacy_aliases": [LEGACY_PRODUCT_WEDGE],
        "legacy_protocol_identifiers": {
            "mcp_server_name": LEGACY_MCP_SERVER_NAME,
        },
        "supersedes": ["product_wedge", "product_loop"],
        "scope": {
            "transaction_state_machine": "configured_upstream_mcp_tool_only",
            "local_and_dogfood_tools": (
                "not_covered_by_dispatch_uncertainty_semantics"
            ),
        },
        "semantics": list(POSITIONING_SEMANTICS),
        "canonical_loop": list(POSITIONING_CANONICAL_LOOP),
        "claim_boundary": POSITIONING_CLAIM_BOUNDARY,
    }
