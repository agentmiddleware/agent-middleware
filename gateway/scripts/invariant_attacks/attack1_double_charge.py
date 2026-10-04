"""ATTACK 1 — Double charging.

Invariant under test: a governed invocation is charged at most once. A retry —
whether a burst of identical parallel requests or a same-key/different-payload
replay — must never produce a second charge or a second side effect.

Sub-tests:
  1a. Fire N identical tools/call invocations in parallel (same idempotency key,
      same payload). Expect: exactly one debit, all responses reference the SAME
      receipt_id, exactly one note written.
  1b. Reuse that idempotency key with a DIFFERENT payload. Expect: fail-closed
      `idempotency_key_reused`, no new charge, no new note.
"""

import json
import sys

import attacklib as A

N = 10
MARKER = "atk1-parallel"


def main():
    cred = A.provision("atk1-double-charge")
    permit = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=1000,
        idem="atk1-permit",
    )
    permit_id = permit["json"]["permit_id"]

    notes0 = A.notes_count_for(MARKER)

    # 1a: N identical invocations, same idem key + same payload, fired simultaneously
    idem_key = "atk1-note-shared"
    text = f"{MARKER} identical-payload"

    def one(i):
        return A.invoke(
            cred["api_key"],
            cred["wallet_id"],
            permit_id,
            idem_key,
            text,
            rpc_id=f"atk1-{i}",
        )

    results = A.fire_parallel(N, one)

    receipt_ids = []
    per_response = []
    for i, r in enumerate(results):
        rc = A.receipt_of(r)
        rid = rc.get("receipt_id") if rc else None
        receipt_ids.append(rid)
        per_response.append(
            {
                "i": i,
                "http_status": r.get("status"),
                "carried_receipt": rid,
                "outcome": A.outcome_of(r),
                "error_or_reason": A.reason_of(r),
            }
        )
    distinct_receipts = sorted(set(x for x in receipt_ids if x))
    winners = sum(1 for p in per_response if p["carried_receipt"])
    collapsed = sum(1 for p in per_response if not p["carried_receipt"])

    led1 = A.ledger(cred)["json"]
    notes1 = A.notes_count_for(MARKER)
    debit_count = sum(1 for e in led1["entries"] if e["action"] == "debit")

    # 1b: same idem key, DIFFERENT payload
    conflict = A.invoke(
        cred["api_key"],
        cred["wallet_id"],
        permit_id,
        idem_key,
        f"{MARKER} DIFFERENT-payload-same-key",
        rpc_id="atk1-conflict",
    )
    led2 = A.ledger(cred)["json"]
    notes2 = A.notes_count_for(MARKER)

    evidence = {
        "attack": "1 - double charging",
        "parallel_count": N,
        "sub_1a_parallel_identical": {
            "distinct_receipt_ids": distinct_receipts,
            "num_distinct_receipts": len(distinct_receipts),
            "winners_with_receipt": winners,
            "collapsed_no_receipt": collapsed,
            "per_response": per_response,
            "notes_written_delta": notes1 - notes0,
            "debit_count_after": debit_count,
            "period_debits_after": led1["period_debits_exact"],
            "sample_receipt": A.receipt_of(next(r for r in results if A.receipt_of(r))),
        },
        "sub_1b_conflict_diff_payload": {
            "http_status": conflict["status"],
            "reason": A.reason_of(conflict),
            "outcome": A.outcome_of(conflict),
            "response": conflict.get("json"),
            "notes_written_delta_after_conflict": notes2 - notes1,
            "period_debits_after_conflict": led2["period_debits_exact"],
        },
        "wallet_id": cred["wallet_id"],
        "key_prefix": A.redact(cred["api_key"]),
    }

    # Verdict logic
    ok_1a = (
        len(distinct_receipts) == 1
        and (notes1 - notes0) == 1
        and float(led1["period_debits_exact"]) == 2.0
    )
    ok_1b = (
        A.reason_of(conflict) == "idempotency_key_reused"
        and (notes2 - notes1) == 0
        and float(led2["period_debits_exact"]) == 2.0
    )
    verdict = "HELD" if (ok_1a and ok_1b) else "BROKE"
    evidence["verdict"] = verdict
    evidence["checks"] = {
        "parallel_single_charge": ok_1a,
        "conflict_rejected_no_charge": ok_1b,
    }

    print(json.dumps(evidence, indent=2, default=str))
    with open("evidence_attack1.json", "w") as fh:
        json.dump(evidence, fh, indent=2, default=str)
    return A.verdict_exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
