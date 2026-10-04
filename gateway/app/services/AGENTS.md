# AGENTS.md — Services (trust-critical core)

This directory holds the trust-critical service code: billing/metering, permits, receipts, signing keys, idempotency, audit, governance, auth, and KYC. Treat everything here as both integrity-critical and security-critical.

## Billing / Metering Integrity

Never change billing, metering, budget, usage, or charge logic without checking:

- idempotency
- retry behavior
- double-charge risk
- tenant isolation
- budget limits
- over-budget behavior
- audit trail
- receipt linkage

Required tests for billing changes:

- retry does not double-charge
- over-budget invocation fails
- invalid tenant cannot access billing records
- idempotency key prevents duplicate charge
- usage event links to correct permit/tool/action

## Security

Check:

- authentication
- authorization
- tenant isolation
- delegation scope
- permit lifecycle
- revocation
- replay prevention
- key handling
- secret exposure
- logging of sensitive data

Required tests for security changes:

- unauthorized request fails
- expired/revoked credential fails
- permit cannot exceed delegation scope
- Tenant A cannot access Tenant B data
- malicious input is rejected
