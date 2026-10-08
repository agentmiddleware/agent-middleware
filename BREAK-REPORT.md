# Audit chain break report

4 breaks found. 4 fixed and tested. 0 fixed but failing. 0 blocked.

Middle-link deletion was already rejected (`audit_previous_hash_mismatch`). That case is covered by a test and is not counted above.

Local SQLite tests only. No production calls.

## High: a time window hid a deleted tail

What broke: delete the last audit event for a wallet, then verify with any time window that still covers the survivors (a start date in the past, an end date in the future, or both). The answer was valid. The same deletion with no time window was already reported as `audit_chain_truncated`.

How to reproduce: record three events, delete the last row, call `verify_audit_chain` with `created_after=2000-01-01` or `created_before=2099-01-01`.

Root cause: `gateway/app/services/audit_chain.py` loaded the chain head only when both time filters were absent. A window skipped the anchor that detects a missing tail. The head is now loaded on every verify (snapshot at `audit_chain.py:508`, tail check at `audit_chain.py:667`).

Fix: one snapshot loads every event and the head. Time filters choose which links get a hash walk. The tail is still compared to the head.

Test: `test_time_window_does_not_hide_a_deleted_tail` (three windows). Before the fix each window returned `valid=True` with 2 events. After the fix, `audit_chain_truncated`.

## High: sequence holes and duplicate sequence numbers verified as valid

What broke: `seq` is not part of the signature. Renumber the tail from 3 to 4 and set the head's `last_seq` to 4. Hash order and the head hash still match, so verify returned valid for a chain whose sequences are 1, 2, 4. Collapsing the tail onto the previous sequence (1, 2, 2) and pointing the head at that sequence also returned valid.

How to reproduce: record three events, update the last row's `seq` and the head's `last_seq` as above, call `verify_audit_chain`.

Root cause: `gateway/app/services/audit_chain.py` compared only the last event's seq and chain hash to the head. It never required the sequences to be 1, 2, 3, and so on up to that head. The check is `_first_sequence_break` at `audit_chain.py:70`, applied at `audit_chain.py:679`.

Fix: after the hash walk, if the head row exists, reject the chain unless the stored sequences are exactly 1 through `last_seq`. Reason: `audit_sequence_gap`. A time window does not skip this check.

Test: `test_renumbered_sequence_gap_is_rejected` and `test_duplicate_sequence_is_rejected`. Before the fix both returned `valid=True`. After the fix, `audit_sequence_gap`. The windowed form of the gap test fails the same way.

## High: a missing head looked like a complete chain, and the next append split it

What broke: delete the `audit_chain_heads` row and leave the events. Verify returned valid. The following append treated the wallet as empty, signed the new event as a genesis (no previous hash, seq 1), and stored a new head. The older links then failed verification.

How to reproduce: record two events, delete the head row, call `verify_audit_chain`, then `record_audit_event` again.

Root cause: `gateway/app/services/audit_chain.py` ran the tail check only when a head row was present, and `append_chained_audit_event` inserted a new head whenever the select found none (`audit_chain.py:317`).

Fix: a modern chain (any seq other than 0) with events and no head fails verify with `audit_chain_head_missing` (`audit_chain.py:654`). Append raises `AuditChainHeadMissingError` with that same message and does not insert a row (`audit_chain.py:328`). Chains whose rows are still the old default seq 0, and which have no head, still verify.

Test: `test_missing_head_on_a_modern_chain_is_rejected` and `test_append_refuses_to_start_a_new_genesis_when_the_head_is_gone`. Before the fix, verify returned `valid=True` and the append did not raise. After the fix, verify returns `audit_chain_head_missing`, the append raises `AuditChainHeadMissingError`, and the event count stays 2.

## High: a blank wallet id split the wallet-less chain

What broke: an event with `wallet_id=""` and an event with `wallet_id=None` share one head key (`""`). Verify walked them as two chains. Global verify then reported `audit_chain_truncated` and counted one event.

How to reproduce: `record_audit_event(..., wallet_id=None)` then `record_audit_event(..., wallet_id="")`, then `verify_audit_chain(wallet_id=None)`.

Root cause: `append_chained_audit_event` uses `wallet_id or ""` as the head key, while verify filtered `wallet_id IS NULL` and `wallet_id == ""` separately. The shared filter is `_chain_wallet_filter` at `audit_chain.py:57`. Global wallet ids fold `""` into the null chain at `audit_chain.py:436`.

Fix: NULL and blank wallet ids are one chain on verify, including the predecessor link and the head. Stored wallet ids are unchanged.

Test: `test_blank_wallet_id_shares_the_wallet_less_chain`. Before the fix, global verify returned `valid=False`, reason `audit_chain_truncated`, `checked_events=1`. After the fix, global verify and `verify_audit_chain(wallet_id="")` both return valid with `checked_events=2`.

## Tests

New file: `gateway/tests/test_audit_chain_tamper.py`.

Before any source change:

```
cd gateway && ~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_audit_chain_tamper.py --tb=line -p no:cacheprovider
```

Result: `8 failed, 3 passed in 1.74s`. Failures were the sequence gap, the duplicate sequence, all three time windows, the missing head, the append, and the blank wallet. The three passes were middle-link deletion (already rejected), an empty window on a healthy chain, and a seq 0 chain with no head.

After the fix:

```
cd gateway && ~/metacode/fleet/heavy .venv/bin/ruff check app/services/audit_chain.py tests/test_audit_chain_tamper.py
cd gateway && ~/metacode/fleet/heavy .venv/bin/pytest -q tests/test_audit_chain_tamper.py tests/test_audit_chain.py tests/test_audit_snapshot.py tests/test_audit_chain_contention_surface.py --tb=short -p no:cacheprovider
```

Ruff: `All checks passed!`

Pytest: `35 passed in 13.44s`.

A later run of `tests/test_audit_chain_tamper.py` alone, after the append test was pointed at `AuditChainHeadMissingError`, was `11 passed in 0.44s`.

## Open questions

Rewinding the head to an earlier contiguous prefix is still invisible. Delete the tail, set `last_seq` and `last_chain_hash` to the new last event, and leave sequences 1..k untouched. The rows look like the chain stopped there. Seeing that requires an anchor outside this database. This change does not add one.

A chain that is still all seq 0 and has no head still verifies. The next append on that chain now raises `AuditChainHeadMissingError` instead of signing a new genesis. If any deployment still has those rows, audit writes for that wallet stop until a head is repaired.

`AuditChainHeadMissingError` is not the contention error (`audit_chain_head_contention`). A request that hits it is not retried as a busy chain.
