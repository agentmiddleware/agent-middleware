# Bugbot review focus

- Check authorization, tenant boundaries, tool input injection, and receipt or key signing and verification. Flag replay and invalid signature paths.
- Flag committed secrets, keys, `.env` files, and local databases. Check that gitleaks and the local secret scan remain enabled. Never print secret values.
- Require tests for behavior changes, including negative paths for security changes. Do not weaken a security check to pass tests.
- Use real data only. Do not create synthetic data in product code, shipped fixtures, docs, examples, or user facing copy: no invented customers, wallets, keys, receipts, ledger entries, metrics, or claims.
- Tests may supply their own inputs and inject faults. Tool arguments, client chosen idempotency keys, fault messages, and reserved documentation addresses (RFC 5737, RFC 3849) are not synthetic data. Anything the product records, such as a wallet, debit, ledger entry id, receipt, record id, stored response, or signature, must come from the product and not be written by hand.
- Keep changes small and on one branch per issue. Do not touch the archived `agent-middleware-api` repository.
- Do not use em dashes, en dashes, or spaced hyphen dashes in user facing copy.
- Require an RFC issue before normative `spec/` changes. Confirm one Alembic head before migration changes. Sign off commits with `git commit -s`.
- Do not force push or use admin merge. Merge only after QA PASS names the head SHA, `CI result`, `License result`, and `gitleaks` pass, the branch is current with main, and review threads are resolved.
