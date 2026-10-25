# Changelog

This repository starts with a fresh history. Releases are listed here once tagged.

## [Unreleased]

- Changed: the AutoGen (`autogen-agent-middleware`) and CrewAI (`crewai-agent-middleware`) integrations now default `base_url` to `https://api.agentmiddleware.dev` instead of `https://api.thisisatest.tech`. Pass `base_url` explicitly to keep the old host.
- Initial import of the gateway (Fair Source, FSL-1.1-ALv2), the Python SDK and
  framework integrations (Apache-2.0), and examples (Apache-2.0).
- New: permit, receipt, and key-set format draft (`spec/`, v0.1) with test vectors.
- New: TypeScript offline receipt verifier (`sdk/typescript/`).
