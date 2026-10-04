"""ATTACK 2 — Budget / permit overspend.

Invariant under test: cumulative spend under a permit can never exceed its
`max_credits` cap, even under concurrent invocations racing the spend check.

Sub-tests:
  2a. Sequential boundary: cap=7, cost=2/call. Calls 1..3 succeed (2,4,6);
      call 4 must be denied `permit_budget_exceeded` with credits_charged=0,
      ledger_entry_id=null, no side effect.
  2b. Concurrency race: fresh cap=7 permit. Fire N=10 invocations with DISTINCT
      idempotency keys IN PARALLEL. Idempotency cannot collapse them, so the
      only thing stopping overspend is an atomic check-and-debit. Expect: at
      most 3 charges (<=6 credits <=7 cap), the rest denied; total debited
      never exceeds 7.
"""

import json
import sys

import attacklib as A


def sequential_boundary():
    cred = A.provision("atk2a-budget-seq")
    permit = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=7,
        idem="atk2a-permit",
    )
    pid = permit["json"]["permit_id"]
    calls = []
    for n in (1, 2, 3, 4):
        r = A.invoke(
            cred["api_key"],
            cred["wallet_id"],
            pid,
            f"atk2a-note-{n}",
            f"atk2a note {n}",
            rpc_id=f"atk2a-{n}",
        )
        rc = A.receipt_of(r)
        calls.append(
            {
                "n": n,
                "http": r["status"],
                "outcome": A.outcome_of(r),
                "reason": A.reason_of(r),
                "credits_charged": rc.get("credits_charged") if rc else None,
                "ledger_entry_id": rc.get("ledger_entry_id") if rc else None,
                "receipt_id": rc.get("receipt_id") if rc else None,
                "details": (r.get("json") or {})
                .get("result", {})
                .get("receipt", {})
                .get("constraints_evaluated")
                if rc
                else (r.get("json")),
            }
        )
    led = A.ledger(cred)["json"]
    denial = next(c for c in calls if c["n"] == 4)
    ok = (
        calls[0]["outcome"] == "success"
        and calls[1]["outcome"] == "success"
        and calls[2]["outcome"] == "success"
        and denial["reason"] == "permit_budget_exceeded"
        and str(denial["credits_charged"]) in ("0", "0.00000000", "0E-8", "0.0")
        and denial["ledger_entry_id"] in (None, "null")
        and float(led["period_debits_exact"]) == 6.0
    )
    return {
        "calls": calls,
        "period_debits": led["period_debits_exact"],
        "denial_receipt": denial,
        "ok": ok,
    }


def concurrency_race(N=10, cap=7):
    cred = A.provision("atk2b-budget-race")
    permit = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=cap,
        idem="atk2b-permit",
    )
    pid = permit["json"]["permit_id"]

    def one(i):
        # DISTINCT idempotency key per call so idempotency does not dedupe them
        return A.invoke(
            cred["api_key"],
            cred["wallet_id"],
            pid,
            f"atk2b-note-{i}",
            f"atk2b race {i}",
            rpc_id=f"atk2b-{i}",
        )

    results = A.fire_parallel(N, one)

    successes, denials, other = [], [], []
    for i, r in enumerate(results):
        rc = A.receipt_of(r)
        oc = A.outcome_of(r)
        rec = {
            "i": i,
            "http": r["status"],
            "outcome": oc,
            "reason": A.reason_of(r),
            "credits_charged": rc.get("credits_charged") if rc else None,
            "receipt_id": rc.get("receipt_id") if rc else None,
        }
        if oc == "success":
            successes.append(rec)
        elif A.reason_of(r) == "permit_budget_exceeded":
            denials.append(rec)
        else:
            other.append(rec)

    led = A.ledger(cred)["json"]
    total_debited = float(led["period_debits_exact"])
    # DB-truth: sum of debits recorded against the wallet
    recon = A.reconcile_wallet(cred["wallet_id"])
    debit_sum_db = sum(abs(float(d["amount"])) for d in recon["debits"])
    ok = (
        total_debited <= cap
        and debit_sum_db <= cap
        and len(successes) == int(cap // 2)  # 3 successes for cap=7,cost=2
        and total_debited == len(successes) * 2.0
    )
    return {
        "N": N,
        "cap": cap,
        "successes": len(successes),
        "denials": len(denials),
        "other": other,
        "total_debited_ledger": total_debited,
        "total_debited_db": debit_sum_db,
        "success_detail": successes,
        "denial_sample": denials[0] if denials else None,
        "ok": ok,
    }


def main():
    a = sequential_boundary()
    b = concurrency_race()
    verdict = "HELD" if (a["ok"] and b["ok"]) else "BROKE"
    evidence = {
        "attack": "2 - budget overspend",
        "verdict": verdict,
        "sub_2a_sequential_boundary": a,
        "sub_2b_concurrency_race": b,
    }
    print(json.dumps(evidence, indent=2, default=str))
    with open("evidence_attack2.json", "w") as fh:
        json.dump(evidence, fh, indent=2, default=str)
    return A.verdict_exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
