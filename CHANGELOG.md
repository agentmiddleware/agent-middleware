# Changelog

This repository starts with a fresh history. Releases are listed here once tagged.

## [Unreleased]

- Upstream MCP: optional multi-tool discovery and exact-name allowlist with
  stable public IDs, per-tool permit scope, and unchanged per-call credits.
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
- Migrations: 053 recreates `receipt_resolutions`, `resolution_chain_heads`,
  `approval_gate_approvals`, `api_key_usage_events` and the `api_key_usage_daily`
  view on databases built by the archived api chain, only when missing.
- Replay: whitespace-only recorded `approval_id` now raises `ReplayError` like the
  empty string, and `amw-replay` writes `receipts.jsonl`, `summary.json`, and
  `keys.json` atomically (temp file in the output dir, then rename), leaving no
  partial outputs or temp files when a write fails.
