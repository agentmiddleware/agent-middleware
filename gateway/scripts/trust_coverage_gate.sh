#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/python_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/python_env.sh"

TRUST_COVERAGE_TESTS=(
  tests/test_golden_path.py
  tests/test_demo_trust_plane.py
  tests/test_agent_ops_war_room_demo.py
  tests/test_me_trust_ledger.py
  tests/test_mcp_trust.py
  tests/test_governed_persistence.py
  tests/test_mcp_dispatch_evidence.py
  tests/test_mcp_dispatch_reconciliation.py
  tests/test_mcp_upstream_governed.py
  tests/test_upstream_mcp.py
  tests/test_upstream_retry_cap_enforcement.py
  tests/test_refund_reconciliation.py
  tests/test_mcp_trust_mode.py
  tests/test_trust_operator_inspection.py
  tests/test_signing_key_lifecycle.py
  tests/test_trust_mode_guardrails.py
  tests/test_permits.py
  tests/test_receipts.py
  tests/test_audit_chain.py
  tests/test_idempotency.py
  tests/test_human_approval_gate.py
  tests/test_permit_request_flow.py
  tests/test_signed_quotes.py
  tests/test_denial_details_and_self_service.py
  tests/test_adversarial_five_claims.py
  tests/test_standard_mcp_endpoint.py
  tests/test_api_keys.py
  tests/test_key_management.py
  tests/test_revocation_containment.py
  tests/test_jwt_auth.py
  tests/test_tenant_isolation_hardening.py
  tests/test_static_dev_api_keys.py
  tests/test_dev_key_self_provision.py
  tests/test_policy_bundles.py
  tests/test_policy_decisions.py
  tests/test_evidence_bundle.py
  tests/test_preflight.py
  tests/test_governed_metering.py
  tests/test_billing.py
  tests/test_public_mcp_endpoint.py
  tests/test_discovery.py
  tests/test_public_metadata.py
  tests/test_trust_coverage_gate_scope.py
)

# tests/test_trust_coverage_gate_scope.py fails when a CORE_TRUST_ROUTERS
# module or a core-loop service is neither listed here nor exempted there with
# a written reason, so a new trust module cannot sit outside the floor.
TRUST_COVERAGE_MODULES=(
  app.routers.mcp
  app.routers.permits
  app.routers.receipts
  app.routers.keys
  app.routers.audit
  app.routers.me
  app.services.permits
  app.services.receipts
  app.services.signing_keys
  app.services.idempotency
  app.services.refund_reconciliation
  app.services.billing_engine
  app.services.mcp_dispatch_attempts
  app.services.mcp_dispatch_reconciliation
  app.services.upstream_mcp
  app.trust.evidence
  app.services.human_approval
  app.services.permit_requests
  app.services.quotes
  app.routers.quotes
  app.routers.permit_requests
  app.core.trust_mode
  app.core.auth
  app.core.jwt
  app.services.api_key_service
  app.services.audit_chain
  app.services.policies
  app.services.governed_metering
  app.routers.mcp_standard
  app.routers.api_keys
  app.routers.dev_keys
  app.routers.policies
  app.routers.evidence
  app.routers.preflight
  app.routers.billing
  app.routers.mcp_public
  app.routers.well_known
  app.routers.discover
)

COV_ARGS=()
for module in "${TRUST_COVERAGE_MODULES[@]}"; do
  COV_ARGS+=("--cov=$module")
done

echo "[trust-coverage] enforcing 80% coverage over trust-plane control modules"
"${PYTEST_CMD[@]}" -q \
  "${TRUST_COVERAGE_TESTS[@]}" \
  "${COV_ARGS[@]}" \
  --cov-report=term-missing \
  --cov-fail-under=80

echo "[trust-coverage] trust coverage gate passed"
