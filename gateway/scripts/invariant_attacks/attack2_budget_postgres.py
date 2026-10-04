"""Same budget-overspend race as attack2, but against the POSTGRES-backed
instance (port 8001). If Postgres holds the cap under concurrency while SQLite
overspends, the root cause is confirmed: SELECT ... FOR UPDATE is a no-op on
SQLite but a real row lock on Postgres."""

import os
import json
import sys

import attacklib as A

A.API = os.environ.get("API_URL", "http://127.0.0.1:8001")  # PG-backed instance


def race(cap, N, label):
    cred = A.provision(f"pgrace-{label}")
    p = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=cap,
        idem=f"pgrace-permit-{label}",
    )["json"]
    pid = p["permit_id"]

    def one(i):
        return A.invoke(
            cred["api_key"],
            cred["wallet_id"],
            pid,
            f"pg-{label}-{i}",
            f"pg {label} {i}",
            rpc_id=f"pg{label}-{i}",
        )

    results = A.fire_parallel(N, one)
    succ = sum(1 for r in results if A.outcome_of(r) == "success")
    denied = sum(1 for r in results if A.reason_of(r) == "permit_budget_exceeded")
    other = N - succ - denied
    led = A.ledger(cred)["json"]
    debited = float(led["period_debits_exact"])
    return {
        "label": label,
        "cap": cap,
        "N": N,
        "successes": succ,
        "cap_allows": cap // 2,
        "denied_budget": denied,
        "other": other,
        "total_debited": debited,
        "OVERSPENT": debited > cap,
    }


def main():
    rows = [
        race(cap, N, label)
        for label, cap, N in [
            ("A_cap7_N10", 7, 10),
            ("B_cap2_N5", 2, 5),
            ("C_cap6_N8", 6, 8),
        ]
    ]
    # HELD needs every race to stay within its cap AND to have actually filled
    # it: a race whose calls all failed for some other reason debits nothing,
    # so "not overspent" alone would pass vacuously.
    held = all(
        not r["OVERSPENT"] and r["successes"] == r["cap_allows"] and r["other"] == 0
        for r in rows
    )
    verdict = "HELD" if held else "BROKE"
    evidence = {
        "attack": "2 - budget overspend (postgres)",
        "verdict": verdict,
        "races": rows,
    }
    print(json.dumps(evidence, indent=2, default=str))
    with open("evidence_attack2_postgres.json", "w") as fh:
        json.dump(evidence, fh, indent=2, default=str)
    return A.verdict_exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
