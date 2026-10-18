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

Know which level proves what before quoting a green run:
[test tiers](../docs/TESTING_TIERS.md) (plain pytest vs Postgres opt-in vs live).
The default run is tier 1 only: SQLite, permissive trust flags, no rate
limits, golden snapshots compared not regenerated.
