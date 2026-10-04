# AGENTS.md — Tests

Tests should verify business-critical behavior, not just happy paths.

Prefer:

- negative-path tests
- authorization failure tests
- tenant isolation tests
- billing/idempotency tests
- receipt verification tests
- replay prevention tests
- malformed input tests

Do not remove tests unless replacing them with equal or better coverage.
