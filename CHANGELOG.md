# Changelog

This repository starts with a fresh history. Releases are listed here once tagged.

## [Unreleased]

- Initial import of the gateway (Fair Source, FSL-1.1-ALv2), the Python SDK and
  framework integrations (Apache-2.0), and examples (Apache-2.0).
- New: permit, receipt, and key-set format draft (`spec/`, v0.1) with test vectors.
- New: TypeScript offline receipt verifier (`sdk/typescript/`).
- Replay: recorded `approval_id` must be missing, `null`, or a non-empty string, else
  loading the calls file raises `ReplayError` naming the record.
- Replay: new `amw-replay --require-verified` flag exits 2 unless every
  recorded receipt verified against `--recorded-keys`.
