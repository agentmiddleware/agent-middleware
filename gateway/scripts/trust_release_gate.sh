#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/python_env.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/python_env.sh"

echo "[trust-gate] Railway IaC contract"
make check-railway-iac

TRUST_TESTS=(
  tests/test_golden_path.py
  tests/test_demo_trust_plane.py
  tests/test_mcp_trust.py
  tests/test_refund_reconciliation.py
  tests/test_mcp_trust_mode.py
  tests/test_trust_operator_inspection.py
  tests/test_signing_key_lifecycle.py
  tests/test_trust_mode_guardrails.py
  tests/test_permits.py
  tests/test_receipts.py
  tests/test_audit_chain.py
  tests/test_idempotency.py
  tests/test_adversarial_five_claims.py
  tests/test_dispatch_claim_migration.py
  tests/test_governed_debit_dispatch_fence.py
  tests/test_late_debit_reconciliation.py
  tests/test_mcp_dispatch_claim.py
  tests/test_mcp_dispatch_router_claim.py
  tests/test_schema_boot.py
  tests/test_standard_mcp_endpoint.py
)

echo "[trust-gate] focused trust-plane pytest suite"
"${PYTEST_CMD[@]}" -q "${TRUST_TESTS[@]}"

echo "[trust-gate] trust-core coverage gate"
scripts/trust_coverage_gate.sh

echo "[trust-gate] trust-plane demo proof"
"${PYTHON_CMD[@]}" scripts/demo_trust_plane.py --assert

echo "[trust-gate] discovery drift checks"
"${PYTEST_CMD[@]}" -q tests/test_discovery_drift.py

echo "[trust-gate] OpenAPI parity"
"${PYTHON_CMD[@]}" scripts/export_openapi.py --check

echo "[trust-gate] simulation inventory parity"
"${PYTHON_CMD[@]}" scripts/generate_sim_inventory.py --check

echo "[trust-gate] trust release gate passed"
