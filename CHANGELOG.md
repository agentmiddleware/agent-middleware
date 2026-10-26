# Changelog

This repository starts with a fresh history. Releases are listed here once tagged.

## [Unreleased]

- Optional PostHog product tracking (`POSTHOG_API_KEY`, `POSTHOG_HOST`; off when
  unset): identify with email and `signup` on account creation, `activation` once
  per account on first key use. Fail-safe and non-blocking.
- Upstream MCP (Preview): optional multi-tool discovery and exact-name allowlist with
  stable public IDs, per-tool permit scope, and unchanged per-call credits.
  Health reports `upstream_tool_mode: multi_tool_preview` while it is on.
- Initial import of the gateway (Fair Source, FSL-1.1-ALv2), the Python SDK and
  framework integrations (Apache-2.0), and examples (Apache-2.0).
- New: permit, receipt, and key-set format draft (`spec/`, v0.1) with test vectors.
- New: TypeScript offline receipt verifier (`sdk/typescript/`).
- Replay: recorded `approval_id` must be missing, `null`, or a non-empty string, else
  loading the calls file raises `ReplayError` naming the record.
- Replay: new `amw-replay --require-verified` flag exits 2 unless every
  recorded receipt verified against `--recorded-keys`.
- Receipts: new optional signed fields `enforcement_mode` ("enforce" or
  "permissive") and `gateway_build` (build commit). Each is signed only when set,
  so older receipts still verify. Migration 052 adds the columns.
- Ops: the startup `runtime_posture` log line and `GET /health` now report
  `tenant_rls_mode` (`off` or `enforce`). The default stays `off`.
- Boot: the schema check now requires `receipts.enforcement_mode` and
  `receipts.gateway_build` (migration 052), so a database missing them fails at
  startup instead of on the first receipt write.
- CI: the Postgres proofs job sets `TENANT_RLS_OFF_MIGRATION_DATABASE_URL`, so the
  048 default-off migration proof runs instead of skipping.
- Build: the image commit stamp now resolves `RELEASE_COMMIT_SHA` (explicit
  override), then `RAILWAY_GIT_COMMIT_SHA`, then a CI `GIT_COMMIT_SHA` build arg,
  then the staged `.build_commit_sha`. Each set value must be a 7-40 hex SHA and
  all set values must agree, so a stale pin fails the build instead of stamping it.
- Migrations: 053 recreates `receipt_resolutions`, `resolution_chain_heads`,
  `approval_gate_approvals`, `api_key_usage_events` and the `api_key_usage_daily`
  view on databases built by the archived api chain, only when missing.
- Replay: whitespace-only recorded `approval_id` now raises `ReplayError` like the
  empty string, and `amw-replay` writes `receipts.jsonl`, `summary.json`, and
  `keys.json` atomically (temp file in the output dir, then rename), leaving no
  partial outputs or temp files when a write fails.
