"""ATTACK 7 — Shared five-vector crash storm plus offline forgery check.

The per-vector scripts (attacks 1-6) each attack ONE invariant in isolation.
This schedules five operational attack families against shared victim + attacker
state, so cross-vector contention is exercised: a budget race, a dedup race, a
scope escape, credential misuse, and a mid-flight crash all hammer the SAME
victim wallet. After restart, a sixth invariant—receipt anti-forgery—is checked
offline. Serial tests cannot surface an interaction that only appears when the
live invariants are attacked together.

A pool of workers drives an interleaved job queue against victim wallet V. The
harness proves that every required live variant started before it can report
HELD; it does not claim every variant overlapped at the exact kill instant:

  1 double-charge : K threads fire the SAME idempotency key + payload on a
                    high-cap "load" permit (P_load).
  2 budget cap    : M threads fire DISTINCT keys racing a small-cap permit
                    (P_budget, cap = BUDGET_CAP) — overspend attempt.
  3 scope escape  : threads invoke partner.notes.write through P_scope, whose
                    allowed_tools does NOT include it.
  6 credential    : garbage key / missing key / confused-deputy (attacker key B
                    driving V's permit) / cross-tenant permit issuance / a
                    revoked key.
  5 crash         : a killer SIGKILLs the server the instant committed debits on
                    V cross THRESHOLD — mid-storm. After restart every P_load and
                    P_budget key is replayed with its identical payload.
  4 forgery       : after restart, a SUCCESS receipt is tamper-checked offline
                    with the shipped b2a_sdk verifier. A storm-minted receipt is
                    preferred; a pre-storm control receipt is the fallback.

Invariants asserted after restart + replay (ledger-centric; engine = sqlite):

  - no double charge : total committed debits on V <= distinct keys ever
                       attempted, and no single key is debited twice.
  - budget contained : P_budget.spent_credits <= P_budget.max_credits, and the
                       count of its success receipts * cost <= cap.
  - charge<->receipt : every success receipt's ledger_entry_id exists as a debit;
                       no permanent charged-without-proof beyond transient
                       idempotency_in_progress.
  - deny vectors held: no scope / credential request ever produced a SUCCESS or a
                       charge on V — even amid the crash. A connection reset on an
                       in-flight denied request is acceptable; an admitted call is
                       not. Verified by: (a) no live response carried a success
                       receipt, and (b) every success receipt on V belongs to
                       P_load or P_budget.
  - forgery held     : the selected genuine receipt verifies, every tested
                       high-value signed field is INVALID after tampering, and an
                       unknown key is UNDETERMINED (never a false INVALID).

Needs a controllable server (so it can kill -9 + restart). See the README
"Shared crash-storm attack" recipe. Env: TP_PIDFILE, TP_BOOT, TP_SERVER_LOG,
TP_DB_PATH (the controlled instance's api.db), API_URL.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import attacklib as A  # noqa: N812

SCRATCH = os.environ.get("TP_WORKDIR", os.getcwd())
PIDFILE = os.environ.get("TP_PIDFILE", f"{SCRATCH}/server.pid")
LOG = os.environ.get("TP_SERVER_LOG", f"{SCRATCH}/server.log")
BOOT = os.environ.get("TP_BOOT", f"{SCRATCH}/boot_controlled_uv.sh")
PORT = os.environ.get("TP_PORT", "8000")

BUDGET_CAP = 8  # cost 2/call -> 4 calls allowed under P_budget
COST = 2
LOAD_KEYS = 120  # distinct P_load keys (crash volume + pairing corpus)
BUDGET_KEYS = 12  # distinct P_budget keys racing the cap
DEDUP_KEYS = 4  # keys fired K-parallel each (double-charge)
DEDUP_FANOUT = 8  # identical parallel requests per dedup key
THRESHOLD = 12  # committed debits on V that trigger the mid-storm kill
REQUIRED_STORM_VECTOR_TAGS = (
    "load",
    "budget",
    "dedup",
    "scope",
    "credential_garbage",
    "credential_missing",
    "credential_deputy",
    "credential_revoked",
    "cross_tenant",
)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(tempfile.gettempdir(), "atk7_combined")
os.makedirs(WORK, exist_ok=True)


def all_required_storm_vectors_started(started_by_vector):
    """Fail closed unless every required live attack variant actually started."""

    return all(started_by_vector.get(tag, 0) > 0 for tag in REQUIRED_STORM_VECTOR_TAGS)


# ---- server control ---------------------------------------------------------


def current_pid():
    return int(open(PIDFILE).read().strip())


def proc_stat_state(stat_text):
    """Return the Linux /proc process state without trusting the comm field."""
    _, separator, fields = stat_text.rpartition(")")
    if not separator:
        return None
    state_fields = fields.strip().split()
    return state_fields[0] if state_fields else None


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as proc_stat:
            if proc_stat_state(proc_stat.read()) == "Z":
                return False
    except (FileNotFoundError, OSError):
        # /proc is unavailable on macOS, and a disappearing PID is handled by
        # the portable signal probe below.
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def restart_server():
    logf = open(LOG, "ab")
    env = dict(os.environ)
    env["TP_PORT"] = PORT
    proc = subprocess.Popen(
        ["bash", BOOT],
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
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


# ---- offline forgery verifier (same contract as attack4) --------------------


def verify(bundle_path, keys_path):
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "b2a_sdk.verify_cli",
            "--bundle",
            bundle_path,
            "--keys",
            keys_path,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.path.join(ROOT, "..", "sdk", "python", "src"),
        },
        capture_output=True,
        text=True,
    )
    return {
        "exit": p.returncode,
        "stdout": p.stdout.strip(),
        "stderr": p.stderr.strip(),
    }


# ---- ledger reconciliation --------------------------------------------------


def reconcile(wallet, attempted_keys, load_permit, budget_permit):
    debits = A.db_rows(
        "SELECT entry_id, amount, operation_key FROM ledger_entries "
        "WHERE wallet_id=? AND action='debit'",
        (wallet,),
    )
    idem = {
        r["record_id"]: r
        for r in A.db_rows(
            "SELECT record_id, idempotency_key, ledger_entry_id FROM idempotency_records "
            "WHERE wallet_id=?",
            (wallet,),
        )
    }
    receipts = A.db_rows(
        "SELECT receipt_id, permit_id, idempotency_record_id, ledger_entry_id, "
        "outcome FROM receipts WHERE wallet_id=?",
        (wallet,),
    )
    success_receipts = [r for r in receipts if r["outcome"] == "success"]
    debit_ids = {d["entry_id"] for d in debits}
    receipt_success_ledger_ids = {r["ledger_entry_id"] for r in success_receipts}

    per_key = Counter()
    for d in debits:
        rec = idem.get(d["operation_key"])
        if rec:
            per_key[rec["idempotency_key"]] += 1
    double_charged = {k: c for k, c in per_key.items() if c > 1}

    receipt_without_charge = [
        r for r in success_receipts if r["ledger_entry_id"] not in debit_ids
    ]

    # Every debit must be covered by EITHER a success receipt (proof of charge) OR
    # a surviving idempotency record (the client sees idempotency_in_progress and
    # the record blocks a second charge — a transient "charged, proof-pending"
    # state). A debit with neither is a SILENT charged-without-proof loss.
    idem_record_ids = set(idem.keys())
    silent_orphan_debits = [
        d
        for d in debits
        if d["entry_id"] not in receipt_success_ledger_ids
        and d["operation_key"] not in idem_record_ids
    ]
    debits_without_success_receipt = [
        d for d in debits if d["entry_id"] not in receipt_success_ledger_ids
    ]

    # permit-scoped success accounting
    load_success = [r for r in success_receipts if r["permit_id"] == load_permit]
    budget_success = [r for r in success_receipts if r["permit_id"] == budget_permit]
    other_success = [
        r
        for r in success_receipts
        if r["permit_id"] not in (load_permit, budget_permit)
    ]

    perm = A.db_rows(
        "SELECT permit_id, spent_credits, max_credits FROM permits WHERE permit_id=?",
        (budget_permit,),
    )
    budget_row = perm[0] if perm else None

    return {
        "num_debits": len(debits),
        "distinct_attempted_keys": len(attempted_keys),
        "double_charged_keys": double_charged,
        "num_success_receipts": len(success_receipts),
        "receipt_without_charge_count": len(receipt_without_charge),
        "debits_without_success_receipt": len(debits_without_success_receipt),
        "silent_orphan_debit_count": len(silent_orphan_debits),
        "load_permit_successes": len(load_success),
        "budget_permit_successes": len(budget_success),
        "other_permit_successes": len(other_success),
        "budget_permit_row": budget_row,
        "success_receipt_ledger_ids": len(receipt_success_ledger_ids),
    }


# ---- the storm --------------------------------------------------------------


def main():
    # Reset the dogfood side-effect store so the governed tool can write. It caps
    # at DOGFOOD_MAX_NOTES and returns failed_refunded when full, which would
    # starve the run of the success receipts the forgery vector needs (the money
    # invariants still hold under tool failure, but forgery cannot be exercised).
    # Matches `make quickstart --reset`; the store is a local gitignored file.
    try:
        open(A.NOTES_PATH, "w").close()
    except OSError:
        pass

    victim = A.provision("atk7-victim")
    attacker = A.provision("atk7-attacker")
    revocable = A.provision("atk7-revocable")
    wallet = victim["wallet_id"]

    # Self-provisioned wallets hold 1000 synthetic credits and a permit's
    # max_credits may not exceed the wallet balance, so P_load is capped well
    # above the load corpus's worst-case spend ((LOAD_KEYS + DEDUP_KEYS) * COST).
    p_load = A.issue_permit(
        victim,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=400,
        idem="atk7-p-load",
    )["json"]["permit_id"]
    p_budget = A.issue_permit(
        victim,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=BUDGET_CAP,
        idem="atk7-p-budget",
    )["json"]["permit_id"]
    p_scope = A.issue_permit(
        victim,
        allowed_tools=["some.other.tool"],
        scopes=["tool:some.other.tool:invoke", "billing:charge"],
        max_credits=100,
        idem="atk7-p-scope",
    )["json"]["permit_id"]

    # revoke the revocable key up front so its storm requests hit a dead
    # credential. Assert the revoke actually took — otherwise the "revoked key"
    # vector would be driving a LIVE credential and still report the invariant held.
    revoke_resp = A.http(
        "DELETE",
        f"/v1/api-keys/{revocable['wallet_id']}/{revocable['key_id']}",
        api_key=revocable["api_key"],
    )
    revocation_succeeded = revoke_resp.get("status") in (200, 204)

    # Mint one guaranteed clean success receipt up front. The forgery vector
    # prefers a receipt minted DURING the storm (captured after restart), but an
    # adverse crash timing can leave every storm debit proof-pending with no
    # success receipt yet written — this fallback ensures the anti-forgery
    # property is still exercised rather than silently skipped.
    pre = A.invoke(
        victim["api_key"],
        wallet,
        p_load,
        "atk7-preforge",
        "preforge",
        rpc_id="atk7-preforge",
    )
    pre_storm_rid = (A.receipt_of(pre) or {}).get("receipt_id")

    load_keys = [f"atk7-load-{i}" for i in range(LOAD_KEYS)]
    budget_keys = [f"atk7-budget-{i}" for i in range(BUDGET_KEYS)]
    dedup_keys = [f"atk7-dedup-{i}" for i in range(DEDUP_KEYS)]
    # include the pre-storm forge key so its debit is counted in the key budget
    attempted_keys = (
        set(load_keys) | set(budget_keys) | set(dedup_keys) | {"atk7-preforge"}
    )

    # collected live outcomes, tagged by vector
    live = {
        "load": [],
        "budget": [],
        "scope": [],
        "cred": [],
        "dedup": [],
        "xtenant": [],
    }
    live_lock = threading.Lock()

    def record(bucket, resp):
        with live_lock:
            live[bucket].append(
                {
                    "outcome": A.outcome_of(resp),
                    "reason": A.reason_of(resp),
                    "status": resp.get("status"),
                    "has_receipt": A.receipt_of(resp) is not None,
                    "ledger_entry_id": (A.receipt_of(resp) or {}).get(
                        "ledger_entry_id"
                    ),
                }
            )

    def record_xtenant(resp):
        # Cross-tenant permit issuance returns a plain HTTP permit response, not a
        # governed invoke receipt — so it cannot be judged by outcome/receipt.
        # A regression shows up as a 2xx that CREATED a permit (permit_id present).
        j = resp.get("json") if isinstance(resp.get("json"), dict) else {}
        with live_lock:
            live["xtenant"].append(
                {
                    "status": resp.get("status"),
                    "permit_created": bool(j.get("permit_id")),
                    "reason": A.reason_of(resp),
                }
            )

    # Build the heterogeneous job list — each entry is one thread's work.
    jobs = []
    for k in load_keys:
        jobs.append(
            (
                "load",
                lambda k=k: record(
                    "load",
                    A.invoke(
                        victim["api_key"], wallet, p_load, k, f"load {k}", rpc_id=k
                    ),
                ),
            )
        )
    for k in budget_keys:
        jobs.append(
            (
                "budget",
                lambda k=k: record(
                    "budget",
                    A.invoke(
                        victim["api_key"], wallet, p_budget, k, f"budget {k}", rpc_id=k
                    ),
                ),
            )
        )
    for k in dedup_keys:
        for _ in range(DEDUP_FANOUT):
            jobs.append(
                (
                    "dedup",
                    lambda k=k: record(
                        "dedup",
                        A.invoke(
                            victim["api_key"], wallet, p_load, k, f"dedup {k}", rpc_id=k
                        ),
                    ),
                )
            )
    for i in range(20):
        jobs.append(
            (
                "scope",
                lambda i=i: record(
                    "scope",
                    A.invoke(
                        victim["api_key"],
                        wallet,
                        p_scope,
                        f"atk7-scope-{i}",
                        f"scope {i}",
                        rpc_id=f"scope-{i}",
                    ),
                ),
            )
        )
    # credential-misuse vectors (denied; never a charge on V)
    bogus = "-".join(["not", "a", "valid", "key"])
    for i in range(6):
        jobs.append(
            (
                "credential_garbage",
                lambda i=i: record(
                    "cred",
                    A.http(
                        "POST",
                        "/mcp/messages",
                        api_key=bogus,
                        body=A.invoke_body(
                            wallet, p_load, f"atk7-garbage-{i}", "garbage"
                        ),
                    ),
                ),
            )
        )
        jobs.append(
            (
                "credential_missing",
                lambda i=i: record(
                    "cred",
                    A.http(
                        "POST",
                        "/mcp/messages",
                        api_key=None,
                        body=A.invoke_body(wallet, p_load, f"atk7-nokey-{i}", "nokey"),
                    ),
                ),
            )
        )
        # confused deputy: attacker key B drives victim's permit
        jobs.append(
            (
                "credential_deputy",
                lambda i=i: record(
                    "cred",
                    A.http(
                        "POST",
                        "/mcp/messages",
                        api_key=attacker["api_key"],
                        body=A.invoke_body(
                            wallet, p_load, f"atk7-deputy-{i}", "deputy"
                        ),
                    ),
                ),
            )
        )
        # revoked key drives victim's permit
        jobs.append(
            (
                "credential_revoked",
                lambda i=i: record(
                    "cred",
                    A.http(
                        "POST",
                        "/mcp/messages",
                        api_key=revocable["api_key"],
                        body=A.invoke_body(
                            wallet, p_load, f"atk7-revoked-{i}", "revoked"
                        ),
                    ),
                ),
            )
        )
        # cross-tenant: attacker issues a permit against the victim's wallet.
        # Recorded in its own bucket + judged by "did a permit get created", since
        # an issue_permit response carries no receipt for the cred check to see.
        jobs.append(
            (
                "cross_tenant",
                lambda i=i: record_xtenant(
                    A.issue_permit(
                        attacker,
                        allowed_tools=["partner.notes.write"],
                        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
                        max_credits=100,
                        idem=f"atk7-xtenant-{i}",
                        subject_wallet_id=wallet,
                    )
                ),
            )
        )

    # Interleave the variants so a pool of workers creates sustained cross-vector
    # contention and enough load for the mid-storm kill to land amid real charges.
    # A single Barrier deadlocks once jobs > pool size, so the harness records
    # starts explicitly instead of claiming exact overlap at the kill instant.
    buckets = {}
    for tag, fn in jobs:
        buckets.setdefault(tag, []).append((tag, fn))
    interleaved = []
    while any(buckets.values()):
        for tag in list(buckets.keys()):
            if buckets[tag]:
                interleaved.append(buckets[tag].pop(0))
    jobs = interleaved

    n = len(jobs)
    stop = threading.Event()
    q_idx = {"i": 0}
    q_lock = threading.Lock()
    started_by_vector = Counter()
    worker_errors = Counter()

    def worker():
        while not stop.is_set():
            with q_lock:
                i = q_idx["i"]
                if i >= n:
                    return
                q_idx["i"] += 1
                tag = jobs[i][0]
                started_by_vector[tag] += 1
            try:
                jobs[i][1]()
            except Exception:
                # attacklib turns expected connection resets into status=None.
                # Any exception escaping a job is therefore a harness failure,
                # not evidence that an authorization invariant held.
                with q_lock:
                    worker_errors[tag] += 1

    worker_count = 24
    pid_before = current_pid()
    killed = {"at_debits": None, "pid": pid_before}

    def hard_kill(pid):
        # uv run spawns uvicorn as a child, so kill the whole process group.
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return True
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
                return True
            except OSError:
                return False

    def killer():
        while not stop.is_set():
            d = committed_debits(wallet)
            if d >= THRESHOLD:
                if hard_kill(pid_before):
                    killed["at_debits"] = d
                stop.set()
                return
            time.sleep(0.002)

    kt = threading.Thread(target=killer)
    kt.start()
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        futs = [ex.submit(worker) for _ in range(worker_count)]
        for f in futs:
            try:
                f.result(timeout=60)
            except Exception:
                pass
    stop.set()
    kt.join()
    storm_jobs_started = sum(started_by_vector.values())

    # wait for the old process to actually die, then restart
    t0 = time.time()
    while alive(pid_before) and time.time() - t0 < 15:
        time.sleep(0.1)
    old_process_stopped = not alive(pid_before)
    crashed_debits = committed_debits(wallet)
    restart_server()
    time.sleep(0.6)

    # Capture a receipt MINTED DURING THE STORM *before* replay can create new
    # ones, so the forgery check operates on genuinely storm-time evidence.
    # Exclude the pre-storm fallback receipt so "receipt_source: storm" is true.
    storm_minted = A.db_rows(
        "SELECT receipt_id FROM receipts WHERE wallet_id=? AND permit_id=? "
        "AND outcome='success' AND receipt_id != ? ORDER BY created_at DESC LIMIT 1",
        (wallet, p_load, pre_storm_rid or ""),
    )

    # REPLAY every load + budget key once, identical payload
    replay_hist = Counter()
    for _i, k in enumerate(load_keys):
        r = A.invoke(
            victim["api_key"], wallet, p_load, k, f"load {k}", rpc_id=f"replay-{k}"
        )
        replay_hist[
            "receipt" if A.receipt_of(r) else (A.reason_of(r) or "conn_err")
        ] += 1
    for _i, k in enumerate(budget_keys):
        r = A.invoke(
            victim["api_key"], wallet, p_budget, k, f"budget {k}", rpc_id=f"replay-{k}"
        )
        replay_hist[
            "receipt" if A.receipt_of(r) else (A.reason_of(r) or "conn_err")
        ] += 1

    rec = reconcile(wallet, attempted_keys, p_load, p_budget)

    # Persisted-state check for denied vectors: a credential/cross-tenant request
    # can COMMIT server-side before the SIGKILL resets its connection, so the
    # live response (status=None) would miss it. Judge from the DB instead:
    #  - no permit was persisted with the attacker as issuer over the victim wallet
    #  - no success receipt on the victim wallet was signed by an intruder key
    #    (attacker's confused-deputy key or the revoked key)
    xtenant_permits_persisted = A.db_rows(
        "SELECT permit_id FROM permits WHERE issuer_wallet_id=? AND subject_wallet_id=?",
        (attacker["wallet_id"], wallet),
    )
    intruder_key_ids = {attacker["key_id"], revocable["key_id"]}
    intruder_success = [
        r
        for r in A.db_rows(
            "SELECT receipt_id, key_id FROM receipts WHERE wallet_id=? AND outcome='success'",
            (wallet,),
        )
        if r["key_id"] in intruder_key_ids
    ]

    # ---- forgery (vector 4): prefer a storm-minted receipt, fall back to the
    # pre-storm clean receipt if adverse crash timing left none. ----
    forgery = {"ran": False}
    rid = storm_minted[0]["receipt_id"] if storm_minted else pre_storm_rid
    receipt_source = "storm" if storm_minted else "pre_storm"
    if rid:
        bundle = A.http(
            "GET", f"/v1/receipts/{rid}/portable", api_key=victim["api_key"]
        )
        keys = A.http("GET", "/.well-known/trust-keys.json")
        good_path = os.path.join(WORK, "good.json")
        keys_path = os.path.join(WORK, "keys.json")
        with open(good_path, "w") as fh:
            json.dump(bundle["json"], fh)
        with open(keys_path, "w") as fh:
            json.dump(keys["json"], fh)
        good = verify(good_path, keys_path)
        signing_input = bundle["json"].get("signing_input", "")
        tampers = A.signed_receipt_tamper_cases(
            bundle["json"], fallback_wallet_id=wallet
        )
        tamper_results = {}
        for name, (old, new) in tampers.items():
            if old not in signing_input:
                tamper_results[name] = {"skipped": True}
                continue
            forged = dict(bundle["json"])
            forged["signing_input"] = signing_input.replace(old, new, 1)
            fp = os.path.join(WORK, f"forged_{name}.json")
            with open(fp, "w") as fh:
                json.dump(forged, fh)
            tamper_results[name] = verify(fp, keys_path)
        # unknown key
        kdoc = json.loads(open(keys_path).read())
        mutated = json.loads(json.dumps(kdoc))
        for k in mutated.get("keys") or mutated.get("trust_keys") or []:
            if "kid" in k:
                k["kid"] = k["kid"] + "-ROTATED-AWAY"
        mkeys = os.path.join(WORK, "keys_unknown.json")
        with open(mkeys, "w") as fh:
            json.dump(mutated, fh)
        unknown = verify(good_path, mkeys)
        forgery = {
            "ran": True,
            "receipt_id": rid,
            "receipt_source": receipt_source,
            "genuine_verifies": good["exit"] == 0,
            "genuine_detail": good,
            "signing_input_sample": signing_input[:240],
            "all_tampers_invalid": (
                len(tamper_results) == len(tampers)
                and all(v.get("exit") == 1 for v in tamper_results.values())
            ),
            "unknown_key_undetermined": unknown["exit"] == 2,
            "tamper_results": tamper_results,
        }

    # ---- invariants ----
    live_scope_success = [
        r
        for r in live["scope"]
        if r["outcome"] == "success" or r["has_receipt"] and r["ledger_entry_id"]
    ]
    live_cred_success = [
        r
        for r in live["cred"]
        if r["outcome"] == "success" or (r["has_receipt"] and r["ledger_entry_id"])
    ]
    budget_row = rec["budget_permit_row"]
    # A missing P_budget row would make the cap check pass vacuously — require it.
    budget_row_present = budget_row is not None
    budget_spent = float((budget_row or {}).get("spent_credits", 0))
    budget_max = float((budget_row or {}).get("max_credits", BUDGET_CAP))
    xtenant_created = [r for r in live["xtenant"] if r["permit_created"]]
    all_storm_buckets_observed = all(live[bucket] for bucket in live)

    checks = {
        "crash_happened": killed["at_debits"] is not None,
        "old_process_stopped": old_process_stopped,
        "all_required_storm_vectors_started": all_required_storm_vectors_started(
            started_by_vector
        ),
        "all_storm_buckets_observed": all_storm_buckets_observed,
        "no_worker_errors": not worker_errors,
        "revocation_succeeded": revocation_succeeded,
        "no_double_charge_total": rec["num_debits"] <= rec["distinct_attempted_keys"],
        "no_double_charge_per_key": not rec["double_charged_keys"],
        "no_receipt_without_charge": rec["receipt_without_charge_count"] == 0,
        "no_silent_orphan_debit": rec["silent_orphan_debit_count"] == 0,
        "budget_row_present": budget_row_present,
        "budget_spent_within_cap": budget_row_present and budget_spent <= budget_max,
        "budget_successes_within_cap": rec["budget_permit_successes"] * COST
        <= BUDGET_CAP,
        "no_scope_success": len(live_scope_success) == 0,
        "no_credential_success": len(live_cred_success) == 0,
        "no_cross_tenant_permit_created": len(xtenant_created) == 0,
        # persisted-state checks: catch a denied vector that committed but whose
        # response was lost to the crash
        "no_cross_tenant_permit_persisted": len(xtenant_permits_persisted) == 0,
        "no_intruder_success_persisted": len(intruder_success) == 0,
        "no_success_on_other_permits": rec["other_permit_successes"] == 0,
        "forgery_ran": forgery.get("ran", False),
        "forgery_genuine_verifies": forgery.get("genuine_verifies", False),
        "forgery_all_tampers_invalid": forgery.get("all_tampers_invalid", False),
        "forgery_unknown_key_undetermined": forgery.get(
            "unknown_key_undetermined", False
        ),
    }
    verdict = "HELD" if all(checks.values()) else "BROKE"

    ev = {
        "attack": "7 - shared five-vector crash storm plus offline forgery",
        "verdict": verdict,
        "engine": "sqlite (controlled boot)",
        "params": {
            "load_keys": LOAD_KEYS,
            "budget_keys": BUDGET_KEYS,
            "budget_cap": BUDGET_CAP,
            "cost": COST,
            "dedup_keys": DEDUP_KEYS,
            "dedup_fanout": DEDUP_FANOUT,
            "kill_threshold_debits": THRESHOLD,
            "storm_jobs_enqueued": n,
            "storm_jobs_started": storm_jobs_started,
            "storm_jobs_started_by_vector": dict(started_by_vector),
            "worker_errors_by_vector": dict(worker_errors),
        },
        "crash": {
            "killed_at_committed_debits": killed["at_debits"],
            "debits_visible_after_crash": crashed_debits,
        },
        "replay_outcome_histogram": dict(replay_hist),
        "reconciliation": rec,
        "persisted_denial_check": {
            "cross_tenant_permits_persisted": len(xtenant_permits_persisted),
            "intruder_success_receipts": len(intruder_success),
        },
        "forgery": forgery,
        "live_outcome_counts": {
            k: Counter(
                r.get("outcome") or r.get("reason") or f"http{r.get('status')}"
                for r in v
            )
            for k, v in live.items()
        },
        "checks": checks,
        "victim_wallet": wallet,
    }
    print(json.dumps(ev, indent=2, default=str))
    with open("evidence_attack_combined.json", "w") as fh:
        json.dump(ev, fh, indent=2, default=str)
    return 0 if verdict == "HELD" else 1


if __name__ == "__main__":
    sys.exit(main())
