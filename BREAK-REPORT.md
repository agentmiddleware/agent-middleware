# Break Report: gateway billing_engine / wallet_engine

## Summary

Probed the billing and wallet engines with adversarial engine-level tests
(negative, zero, NaN and Infinite amounts, empty-wallet charges, concurrent
transfer overspend, parallel double refunds), using test doubles only (local
SQLite test database, mocked Stripe where Stripe paths were touched; no live
Stripe calls, no production).

Result: 5 findings, 4 fixed and tested, 0 fixed but failing, 1 investigated
and not broken. Of the 5, three are real money-integrity breaks (negative and
infinite wallet creation, non-finite transfers), one is an error-hygiene break
(raw internal exceptions leaking out of the engine), and one area (transfer
and refund races) was already safe and is now pinned by regression tests.

Findings sorted by severity below. All tests run through the slot queue with
`~/metacode/fleet/heavy`.

## Finding 1 (high): sponsor wallet could be created with a negative balance

What broke: calling `WalletEngine.create_sponsor_wallet` with a negative
`initial_credits` (possible from the SDK and composed server-side callers
such as pods and dev keys, which bypass the HTTP schema validation) created
a wallet holding a negative balance, with no ledger entry recording the
liability. The HTTP route blocks this (`ge=0`), but the engine did not.

How to reproduce: `money.create_sponsor_wallet(sponsor_name="x",
email="x@e.com", initial_credits=Decimal("-500"))` returned a wallet with
balance `-500` (observed in probe output before the fix).

Root cause: `gateway/app/services/wallet_engine.py`, `create_sponsor_wallet`
(no validation of `initial_credits` before either the standalone or the
caller-owned-session path).

Fix: reject non-finite or negative `initial_credits` with `ValueError`
before any row is written. Zero is still allowed (empty wallet for later
top-up, matching `SelfProvisionRequest` semantics).

Test results: `test_sponsor_rejects_negative_and_non_finite_initial` (4
params) fails on old code (wallet created / IntegrityError leak), passes on
new code. Full billing/wallet set: 239 passed.

## Finding 2 (high): sponsor wallet could be created with infinite balance

What broke: `create_sponsor_wallet` with `initial_credits=Decimal("Infinity")`
created a wallet with an infinite balance plus an infinite ledger entry,
minting unlimited credits and poisoning every total computed afterward. NaN
instead leaked a raw `IntegrityError` from the database layer.

How to reproduce: same call as Finding 1 with `Decimal("Infinity")`
(balance `Infinity` observed) and `Decimal("NaN")` (`IntegrityError`
observed).

Root cause: same location as Finding 1, same missing validation.

Fix: same guard as Finding 1 (`is_finite` plus sign check).

Test results: covered by the same regression test as Finding 1 (NaN and
Infinity params). Fails before, passes after.

## Finding 3 (high): transfers accepted NaN and Infinity amounts

What broke: `WalletEngine.transfer` with `Decimal("NaN")` raised a raw
`decimal.InvalidOperation` (a 500-class leak, not a clean refusal), and with
`Decimal("Infinity")` sailed through the positivity check into an
`InsufficientFundsError` carrying infinite required and shortfall amounts,
which downstream JSON cannot render (the same failure shape the transfer
router comment documents for the HTTP path).

How to reproduce: fund a wallet with 100, call `money.transfer(a, b,
Decimal("NaN"))` (observed `InvalidOperation`) and `Decimal("Infinity")`
(observed `InsufficientFundsError` with `required_amount=Infinity`,
`shortfall=Infinity`).

Root cause: `gateway/app/services/wallet_engine.py`, `transfer` (only
`amount <= 0` was checked; NaN breaks the comparison itself and Infinity
passes it).

Fix: `if not amount.is_finite() or amount <= Decimal("0"): raise ValueError`
with a message keeping the established "positive" wording. Same pattern the
Stripe integration already uses (`stripe_integration.py` top-up intent).

Test results: `test_transfer_rejects_non_finite_amount` (3 params) fails on
old code, passes on new code; balances and ledger counts asserted unchanged.

## Finding 4 (medium): agent/child provisioning accepted non-finite budgets and bad child caps

What broke, three parts: (a) `create_agent_wallet` with a NaN budget leaked
`decimal.InvalidOperation`, and with an Infinite budget escaped into an
`InsufficientFundsError`; (b) `create_child_wallet` accepted NaN budgets the
same way; (c) `create_child_wallet` accepted zero, negative, NaN and Infinite
`max_spend` caps, even though the API contract requires `gt=0`. A zero or
negative cap bricks the wallet at birth (no charge can ever fit inside it),
NaN poisons every later cap comparison with `InvalidOperation`, and Infinity
silently disables the cap the parent thought it set.

How to reproduce: probes created child wallets with `max_spend` of `-5`,
`0`, `NaN` and `Infinity` (all accepted, observed `max_spend` echoed back);
agent provisioning with NaN/Infinity budgets produced the leaked errors.

Root cause: `gateway/app/services/wallet_engine.py`, `create_agent_wallet`
(negative-only check) and `create_child_wallet` (negative-only budget check,
no `max_spend` check at all).

Fix: finiteness plus sign checks on both budgets (zero still allowed) and a
new finite-and-positive check on `max_spend`. Messages keep the established
"negative" wording so the existing
`test_negative_provisioning_budget_is_refused` contract (`match="negative"`)
still holds.

Test results: `test_agent_provision_rejects_non_finite_budget`,
`test_child_rejects_bad_budget`, and
`test_child_rejects_non_positive_or_non_finite_max_spend` fail on old code
(11 failing params total across the file), pass on new code. The pre-existing
`test_wallet_concurrency.py` suite passes unchanged.

## Finding 5 (investigated, not broken): transfer and refund races conserve credits

What was tested: five parallel transfers of 60 out of a 100-credit wallet,
and two parallel refunds of a single charge, run with real `asyncio.gather`
concurrency against local SQLite. Transfer races were the named target, so
they were tested first.

Outcome: exactly one transfer won and the wallet pair always summed to the
starting total (guarded debit UPDATE plus ordered row locking hold); the
double refund credited exactly once (deterministic `refund-{charge}` entry id
makes the second attempt idempotent under sequential scheduling, and a true
overlap can only fail on the primary key, which rolls back without moving
money). No fix needed; both behaviors are now pinned by
`test_concurrent_transfers_conserve_credits` and
`test_concurrent_double_refund_credits_once`, which pass on old and new code.

## Verification commands and results

New suite: `gateway/tests/test_billing_engine_adversarial.py`, 23 tests.

- With fix: 23 passed.
- Without fix (engine file temporarily restored from HEAD via read-only
  `git show`, then the fixed file copied back and diff-verified identical):
  14 failed, 9 passed. Every failing case is one of Findings 1-4; every
  passing case documents behavior that was already correct.
- Regression set (adversarial plus billing, transfers, swarm, pods, wallet
  concurrency, ledger integrity, stripe integration, stripe accounting
  concurrency, receipt linkage, spendable status, status enforcement,
  boundary regressions): 239 passed.
- `ruff check` on the changed engine file and the new test file: clean.
- One interim failure worth noting: the existing
  `test_negative_provisioning_budget_is_refused` asserts `match="negative"`,
  which the first draft of the new messages broke. The messages were reworded
  to keep the established keywords ("non-negative", "positive") instead of
  touching the existing test. All green after.

## Open questions

1. Should the engine also enforce the `MAX_STORABLE_AMOUNT` (1e12) ceiling
   the HTTP schemas use? Very large but finite amounts (for example 1e30)
   are still accepted at the engine level; SQLite stores them while Postgres
   Numeric(20, 8) would refuse them, so a huge SQLite-created balance is both
   a parity break and potential credit minting on SQLite deployments. Left
   out as needing Christopher's decision, since it adds a new cap rather than
   closing an existing contract gap.
2. A truly simultaneous double refund (forced interleave inside the
   check-then-insert window) would surface an `IntegrityError` (500-class) to
   the loser rather than an idempotent success. Money is safe either way
   because the loser rolls back, so this was left alone as error hygiene, not
   integrity. Worth a follow-up only if refund retries start 500ing in logs.
3. `daily_limit`, `auto_refill_threshold` and `auto_refill_amount` accept
   odd-but-harmless engine-level values (for example a negative daily limit
   bricks the wallet the same way a zero cap did). Same class as Finding 4c
   but self-harm only, no cross-wallet effect, so left for a later pass.
