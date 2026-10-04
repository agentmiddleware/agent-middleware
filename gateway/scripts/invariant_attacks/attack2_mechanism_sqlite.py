"""Nail the budget-overspend mechanism: a lost update on permit.spent_credits.

Design intent (app/services/permits.py:407-443): authorize_and_reserve locks the
permit row FOR UPDATE, re-checks max_credits, then increments spent_credits — all
in one transaction. On PostgreSQL the second concurrent caller would block and
re-read the incremented value. On SQLite, FOR UPDATE is a silent no-op, so every
concurrent caller reads the SAME pre-spend value, all pass the cap check, and the
increments clobber each other (lost update). Evidence: after the race the permit's
own spent_credits is far below what the wallet ledger actually debited.
"""

import json
import sys

import attacklib as A

CAP = 6  # cost 2/call -> cap should allow exactly 3 successful calls
N = 8


def main():
    cred = A.provision("atk2-mechanism")
    permit = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=CAP,
        idem="atk2-mech-permit",
    )["json"]
    pid = permit["permit_id"]

    def one(i):
        return A.invoke(
            cred["api_key"],
            cred["wallet_id"],
            pid,
            f"mech-{i}",
            f"mech {i}",
            rpc_id=f"mech-{i}",
        )

    results = A.fire_parallel(N, one)
    successes = sum(1 for r in results if A.outcome_of(r) == "success")

    led = A.ledger(cred)["json"]
    wallet_debited = float(led["period_debits_exact"])

    # Read the permit row straight from the DB (server's own persisted view of spend).
    prow = A.db_rows(
        "SELECT permit_id, max_credits, spent_credits, status FROM permits WHERE permit_id=?",
        (pid,),
    )[0]
    db_debits = A.db_rows(
        "SELECT amount FROM ledger_entries WHERE wallet_id=? AND action='debit'",
        (cred["wallet_id"],),
    )

    out = {
        "cap": CAP,
        "cost_per_call": 2,
        "N_parallel": N,
        "cap_should_allow_successes": CAP // 2,
        "observed_successes": successes,
        "wallet_actually_debited": wallet_debited,
        "permit_max_credits_db": float(prow["max_credits"]),
        "permit_spent_credits_db": float(prow["spent_credits"]),
        "ledger_debit_rows": len(db_debits),
        "lost_update": float(prow["spent_credits"]) < wallet_debited,
        "overspent_vs_cap": wallet_debited > CAP,
    }
    # The race must also fill the cap: with no successes there is nothing to
    # lose or overspend, and the two flags above would pass vacuously.
    verdict = (
        "HELD"
        if (
            not out["lost_update"]
            and not out["overspent_vs_cap"]
            and out["observed_successes"] == CAP // 2
        )
        else "BROKE"
    )
    out["verdict"] = verdict
    print(json.dumps(out, indent=2))
    with open("evidence_attack2_mechanism.json", "w") as fh:
        json.dump(out, fh, indent=2)
    return A.verdict_exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
