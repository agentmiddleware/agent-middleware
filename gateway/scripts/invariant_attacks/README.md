# Invariant attack harness

Adversarial, concurrency-aware tests that try to make the trust plane **violate
its own promises** — double-charge, overspend a permit, escape a permit's scope,
pass off a forged receipt, lose accounting integrity across a crash, or act
outside a credential's authority. Companion to
[`../../docs/invariant-attack-report.md`](../../docs/invariant-attack-report.md),
which records the exact requests, observed responses, and HELD/BROKE/PARTIAL
verdict for each.

Stdlib only (`urllib` + `threading` + `sqlite3`) — no test dependencies,
except that the receipt-forgery attack and the combined crash storm invoke
the shipped offline SDK verifier (`b2a_sdk.verify_cli` via `sys.executable`),
which needs `cryptography` importable by the interpreter running the harness. The
isolated concurrency attacks release threads from a `threading.Barrier`; the
combined crash storm uses an interleaved worker queue and records which variants
actually started. DB reconciliation for the crash test reads the SQLite file
**read-only** so it never perturbs the state it is measuring.

## Run against a stock quickstart

```bash
# terminal 1 — boot the real local trust plane (SQLite, self-serve keys, one tool)
make quickstart

# terminal 2 — from the repo root
cd scripts/invariant_attacks
export API_URL=http://127.0.0.1:8000
export TP_DB_PATH=../../data/quickstart/api.db          # crash reconciliation reads this
export TP_NOTES_PATH=../../data/dogfood_partner_notes.jsonl

python attack1_double_charge.py     # double charging          -> HELD
python attack2_budget.py            # budget overspend         -> HELD on current SQLite
python attack2_mechanism_sqlite.py  #   ^ no lost update / no overspend -> HELD; exits 1 on BROKE
python attack3_scope.py             # scope escape             -> HELD
python attack4_forgery.py           # forged receipts          -> HELD
python attack6_key_misuse.py        # credential misuse        -> HELD
```

Each script provisions its own wallet(s) via `POST /v1/dev-keys/self-provision`,
prints a JSON evidence blob, writes `evidence_attack*.json`, and never prints a
full API key (keys are redacted to an 8-char prefix).

## Attack 2 history: SQLite race fixed, both engines hold

The original campaign reproduced a SQLite overspend because its
`authorize_and_reserve` implementation relied on `SELECT ... FOR UPDATE`, which
SQLAlchemy silently drops on SQLite. That historical finding was fixed by making
the reservation a single guarded atomic `UPDATE`; the current live SQLite race
holds the cap and is gated in CI. The Postgres comparison remains useful because
it independently exercises the production row-lock path:

```bash
# start a throwaway Postgres and a Postgres-backed instance on :8001, then:
API_URL=http://127.0.0.1:8001 python attack2_budget_postgres.py   # -> HELD; exits 1 on BROKE, writes evidence_attack2_*.json
```

## Attack 5 crash consistency

The authoritative proof already lives in the repo and kills workers at **exact
commit boundaries** (not a random `kill -9`):

```bash
make prove-crash-recovery      # 2 fault-boundary deaths + 1 serialization test
```

`attack5_crash_sqlite.py` is the complementary live-instance test: it needs the
server booted under a controllable pid via `boot_controlled.sh` (so it can
`kill -9` and restart it), driven by env vars:

```bash
export TP_WORKDIR=/tmp/tp-crash TP_STATE_DIR=/tmp/tp-crash/state
export TP_PIDFILE=$TP_WORKDIR/server.pid TP_BOOT=$PWD/boot_controlled.sh
mkdir -p "$TP_STATE_DIR"
# boot once so the pid file exists, then run the test (it kills + restarts):
TP_PORT=8000 "$TP_BOOT" & echo $! > "$TP_PIDFILE"
TP_DB_PATH=$TP_STATE_DIR/api.db python attack5_crash_sqlite.py
```

`reconcile_probe.py` forces `reconcile_stuck_records(idle_seconds=0)` against the
live SQLite DB to show crash-orphaned records are repaired or flagged
`needs_review`, never silently dropped.

The live SQLite verdict is fail-closed: hard corruption (double charge,
receipt-without-debit, silent orphan, or a crash that never happened) is `BROKE`;
otherwise unresolved proof-pending records produce `PARTIAL`, and only a fully
resolved run produces `HELD`. The deterministic Postgres boundary suite remains
the authoritative crash-integrity proof.

## Shared crash-storm attack

`attack_combined.py` schedules five operational attack families against shared
victim + attacker state, then kills the server mid-storm: budget overspend,
double-charge/dedup, scope escape, credential misuse, and crash consistency.
The worker queue is interleaved and a `HELD` verdict requires every required live
variant to have started, every broad result bucket to be observed, and no harness
worker error. This proves non-vacuous cross-vector contention; it does not claim
every variant overlapped at the exact kill instant. After restart every
load/budget key is replayed and reconciled, then receipt forgery is checked
offline using a storm-minted receipt when available or a pre-storm control
receipt otherwise.

It boots via `boot_controlled_uv.sh` (uv-based, so it runs on a fresh clone
without an activated venv). Because `uv run` keeps uvicorn as a child, the
launcher must be a **session leader** so the killer can `killpg` the whole group
— hence the `start_new_session` launch below (`setsid` does not exist on macOS).
The rate limiter is raised so the invariant under test — not the throttle — is
what the storm exercises:

```bash
export TP_STATE_DIR=/tmp/tp-combined TP_PIDFILE=$TP_STATE_DIR/server.pid
export TP_BOOT=$PWD/boot_controlled_uv.sh TP_DB_PATH=$TP_STATE_DIR/api.db
export TP_PORT=8000 RATE_LIMIT_PER_MINUTE=1000000
mkdir -p "$TP_STATE_DIR"
# boot once as a session leader, writing the group-leader PID to the pidfile:
python3 -c 'import subprocess,os; p=subprocess.Popen(["bash",os.environ["TP_BOOT"]],\
  start_new_session=True, stdout=open(os.environ["TP_STATE_DIR"]+"/server.log","ab"),\
  stderr=subprocess.STDOUT); open(os.environ["TP_PIDFILE"],"w").write(str(p.pid))'
# wait for http://127.0.0.1:8000/health to be 200, then:
python attack_combined.py     # -> verdict HELD, crash_happened true
```

Verdict is `HELD` only if the crash actually fired, every required live variant
started and produced evidence without a harness error, and every asserted
invariant held: no double charge, budget contained at the cap, charge⇔receipt
paired, no scope/credential escape produced a success, and the selected genuine
receipt verifies while all representative tampering is rejected.

CI runs the controlled crash storm in an isolated SQLite state directory,
redacts credentials and tenant wallet identifiers, and publishes only
`evidence_attack_combined.redacted.json` as a 14-day artifact. Server logs,
databases, raw evidence, and the signing seed are never uploaded.
