"""ATTACK 5c — decisive SQLite crash test (ledger-centric, kill after commit).

Fixes attack5b's too-narrow orphan definition. Kills the live server the instant
committed debits cross a threshold (so the crash lands amid real charges whose
receipts are still unwritten), then after restart REPLAYS EVERY attempted key
with the identical payload and applies the hard money invariants directly to the
ledger:

  * TRIPWIRE — no double charge: total committed debits must never exceed the
    number of DISTINCT idempotency keys ever attempted. One key => at most one
    debit, forever, even across a crash+replay.
  * no receipt without a charge: every success receipt's ledger_entry_id must
    exist in ledger_entries.
  * money accounted: every debit is matched by a success receipt, an exactly
    correlated refund, or a still-pending record the client sees as
    idempotency_in_progress — never a silent, permanent "charged with no proof
    AND no way to retrieve it". A surviving proof-pending record is reported as
    PARTIAL until reconciliation resolves it; it cannot count as HELD.

Ledger-centric classification of each committed debit uses operation_key
(== idempotency_record_id) joined to idempotency_records and receipts.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import attacklib as A

SCRATCH = os.environ.get("TP_WORKDIR", os.getcwd())
PIDFILE = os.environ.get("TP_PIDFILE", f"{SCRATCH}/server.pid")
LOG = os.environ.get("TP_SERVER_LOG", f"{SCRATCH}/server.log")
BOOT = os.environ.get("TP_BOOT", f"{SCRATCH}/boot.sh")
THRESHOLD = 10
WORKERS = 12
U = 200  # distinct idempotency keys available


def current_pid():
    return int(open(PIDFILE).read().strip())


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def restart_server():
    logf = open(LOG, "ab")
    proc = subprocess.Popen(
        ["bash", BOOT, "8000"],
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    open(PIDFILE, "w").write(str(proc.pid))
    t0 = time.time()
    while time.time() - t0 < 90:
        if A.http("GET", "/health").get("status") == 200:
            return proc.pid
        time.sleep(0.3)
    raise RuntimeError("no health after restart")


def committed_debits(wallet):
    try:
        return A.db_rows(
            "SELECT COUNT(*) c FROM ledger_entries WHERE wallet_id=? AND action='debit'",
            (wallet,),
        )[0]["c"]
    except Exception:
        return -1


def ledger_analysis(wallet, attempted):
    debits = A.db_rows(
        "SELECT entry_id, amount, operation_key FROM ledger_entries WHERE wallet_id=? AND action='debit'",
        (wallet,),
    )
    refunds = A.db_rows(
        "SELECT entry_id, amount, correlation_id FROM ledger_entries WHERE wallet_id=? AND action='refund'",
        (wallet,),
    )
    idem = {
        r["record_id"]: r
        for r in A.db_rows(
            "SELECT record_id, idempotency_key, ledger_entry_id, response_json, status_code FROM idempotency_records WHERE wallet_id=?",
            (wallet,),
        )
    }
    receipts = A.db_rows(
        "SELECT idempotency_record_id, ledger_entry_id, outcome FROM receipts WHERE wallet_id=?",
        (wallet,),
    )
    receipt_ledger_ids = {
        r["ledger_entry_id"] for r in receipts if r["outcome"] == "success"
    }
    # per-key debit count (best effort: needs the idem record to still exist)
    key_debit_count = Counter()
    checkpointed = uncheckpointed = record_missing = 0
    debit_classification = Counter()
    for d in debits:
        op = d["operation_key"]
        rec = idem.get(op)
        has_receipt = d["entry_id"] in receipt_ledger_ids
        exact_refund = any(
            refund.get("correlation_id") == d["entry_id"]
            and refund.get("entry_id") == f"refund-{d['entry_id']}"
            and abs(Decimal(str(refund.get("amount") or 0)))
            == abs(Decimal(str(d["amount"])))
            for refund in refunds
        )
        if rec:
            key_debit_count[rec["idempotency_key"]] += 1
            if rec["ledger_entry_id"] == d["entry_id"]:
                checkpointed += 1
            else:
                uncheckpointed += 1
        else:
            record_missing += 1
        if has_receipt:
            debit_classification["success_receipted"] += 1
        elif exact_refund:
            debit_classification["correlated_refunded"] += 1
        elif rec:
            debit_classification["proof_pending"] += 1
        else:
            debit_classification["silent_orphan"] += 1
    double_charged_keys = {k: c for k, c in key_debit_count.items() if c > 1}
    # success receipts whose ledger entry is missing entirely
    debit_ids = {d["entry_id"] for d in debits}
    receipt_without_charge = [
        r
        for r in receipts
        if r["outcome"] == "success" and r["ledger_entry_id"] not in debit_ids
    ]
    return {
        "num_debits": len(debits),
        "num_success_receipts": len(receipt_ledger_ids),
        "num_refund_credits": len(refunds),
        "debits_checkpointed": checkpointed,
        "debits_uncheckpointed": uncheckpointed,
        "debits_record_missing": record_missing,
        "debits_without_success_receipt": sum(
            1 for d in debits if d["entry_id"] not in receipt_ledger_ids
        ),
        "success_receipted_debits": debit_classification["success_receipted"],
        "correlated_refunded_debits": debit_classification["correlated_refunded"],
        "proof_pending_debits": debit_classification["proof_pending"],
        "silent_orphan_debits": debit_classification["silent_orphan"],
        "debit_classification": dict(debit_classification),
        "double_charged_keys": double_charged_keys,
        "receipt_without_charge_count": len(receipt_without_charge),
    }


def classify_verdict(
    *,
    crash_happened,
    no_double_by_total,
    no_double_by_key,
    no_receipt_without_charge,
    silent_orphan_count,
    proof_pending_count,
    worker_error_count,
):
    """Classify hard corruption separately from unresolved proof delivery."""
    hard_invariants_hold = (
        crash_happened
        and no_double_by_total
        and no_double_by_key
        and no_receipt_without_charge
        and silent_orphan_count == 0
        and worker_error_count == 0
    )
    if not hard_invariants_hold:
        return "BROKE"
    if proof_pending_count:
        return "PARTIAL"
    return "HELD"


def run_worker_request(request_tag, invoke, launch_hist, worker_errors, lock):
    """Record an invocation outcome, failing closed on an escaped exception."""
    try:
        response = invoke()
    except Exception:
        with lock:
            worker_errors[request_tag] += 1
        return
    with lock:
        launch_hist[
            "receipt"
            if A.receipt_of(response)
            else (A.reason_of(response) or "conn_err")
        ] += 1


def main():
    cred = A.provision("atk5c-crash")
    permit = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=490,
        idem="atk5c-permit",
    )["json"]
    pid_c, wallet = permit["permit_id"], cred["wallet_id"]
    pid_before = current_pid()
    attempted = [f"atk5c-{i}" for i in range(U)]
    stop = threading.Event()
    ctr = {"n": 0}
    lock = threading.Lock()
    launch_hist = Counter()
    worker_errors = Counter()
    killed = {"happened": False, "at_debits": None}

    def worker():
        while not stop.is_set():
            with lock:
                idx = ctr["n"]
                ctr["n"] += 1
            if idx >= U:
                return
            request_tag = attempted[idx]
            run_worker_request(
                request_tag,
                lambda: A.invoke(
                    cred["api_key"],
                    wallet,
                    pid_c,
                    request_tag,
                    f"note {idx}",
                    rpc_id=request_tag,
                ),
                launch_hist,
                worker_errors,
                lock,
            )

    def killer():
        while not stop.is_set():
            debit_count = committed_debits(wallet)
            if debit_count >= THRESHOLD:
                try:
                    os.kill(pid_before, signal.SIGKILL)
                except OSError:
                    pass
                else:
                    killed["happened"] = True
                    killed["at_debits"] = debit_count
                stop.set()
                return
            time.sleep(0.002)

    kt = threading.Thread(target=killer)
    kt.start()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(worker) for _ in range(WORKERS)]
        for f in futs:
            try:
                f.result(timeout=30)
            except Exception:
                with lock:
                    worker_errors["worker_future"] += 1
    stop.set()
    kt.join()
    t0 = time.time()
    while alive(pid_before) and time.time() - t0 < 15:
        time.sleep(0.1)

    time.sleep(0.4)
    crashed = ledger_analysis(wallet, attempted)

    restart_server()
    time.sleep(0.8)
    post = ledger_analysis(wallet, attempted)

    # REPLAY every attempted key once, identical payload
    replay_hist = Counter()
    d_before = committed_debits(wallet)
    for i, k in enumerate(attempted):
        r = A.invoke(
            cred["api_key"], wallet, pid_c, k, f"note {i}", rpc_id=f"replay-{k}"
        )
        replay_hist[
            "receipt" if A.receipt_of(r) else (A.reason_of(r) or "conn_err")
        ] += 1
    d_after = committed_debits(wallet)
    final = ledger_analysis(wallet, attempted)

    # Invariants
    no_double_by_total = final["num_debits"] <= U
    no_double_by_key = (
        not final["double_charged_keys"] and not crashed["double_charged_keys"]
    )
    no_receipt_without_charge = (
        final["receipt_without_charge_count"] == 0
        and crashed["receipt_without_charge_count"] == 0
    )
    silent_orphan_count = max(
        crashed["silent_orphan_debits"], final["silent_orphan_debits"]
    )
    proof_pending_count = final["proof_pending_debits"]
    verdict = classify_verdict(
        crash_happened=killed["happened"],
        no_double_by_total=no_double_by_total,
        no_double_by_key=no_double_by_key,
        no_receipt_without_charge=no_receipt_without_charge,
        silent_orphan_count=silent_orphan_count,
        proof_pending_count=proof_pending_count,
        worker_error_count=sum(worker_errors.values()),
    )
    ev = {
        "attack": "5c - decisive sqlite crash (kill after commit, replay all)",
        "verdict": verdict,
        "params": {
            "distinct_keys_U": U,
            "threshold_debits": THRESHOLD,
            "engine": "sqlite (quickstart)",
        },
        "launch_outcome_histogram": dict(launch_hist),
        "replay_outcome_histogram": dict(replay_hist),
        "debits_before_replay": d_before,
        "debits_after_replay": d_after,
        "new_debits_from_replay": d_after - d_before,
        "checks": {
            "crash_happened": killed["happened"],
            "no_double_charge_total_le_keys": no_double_by_total,
            "no_double_charge_per_key": no_double_by_key,
            "no_receipt_without_charge": no_receipt_without_charge,
            "no_silent_orphan_debit": silent_orphan_count == 0,
            "no_unresolved_proof_pending": proof_pending_count == 0,
            "no_worker_errors": not worker_errors,
        },
        "worker_errors_by_request_tag": dict(worker_errors),
        "killed_at_committed_debits": killed["at_debits"],
        "unresolved_proof_pending_final": proof_pending_count,
        "crashed_state": crashed,
        "post_restart_state": post,
        "final_after_replay_state": final,
        "wallet": wallet,
    }
    print(json.dumps(ev, indent=2, default=str))
    with open("evidence_attack5c.json", "w") as fh:
        json.dump(ev, fh, indent=2, default=str)
    return 0 if verdict == "HELD" else 1


if __name__ == "__main__":
    sys.exit(main())
