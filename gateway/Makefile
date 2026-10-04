.PHONY: quickstart quickstart-check live-loop-proof demo-ambiguous-retry demo-ambiguous-retry-check failure-lab failure-lab-check test test-all test-proof coverage prove-trust-plane prove-trust-plane-postgres prove-crash-recovery demo-trust-plane demo-trust-plane-check dogfood-trust-plane dogfood-trust-plane-check red-team-trust-plane red-team-trust-plane-check agent-ops-war-room agent-ops-war-room-check check-doc-references trust-coverage-gate trust-release-gate trust-conformance-live adversarial-battery-live failure-lab-suite failure-lab-all failure-lab-evidence failure-lab-list failure-lab-explore failure-lab-integration-check failure-lab-diagnostic failure-lab-verify-bundle



# The 15-minute golden path: boot a real local trust plane on loopback with
# self-serve key minting and one invokable governed tool, then follow
# docs/quickstart.md from `git clone` to an offline-verified signed receipt.
# State persists in data/quickstart/; `--reset` via QUICKSTART_ARGS wipes it.
quickstart:
	uv run --with-requirements requirements.txt python scripts/quickstart.py $(QUICKSTART_ARGS)

# CI guard for the documented golden path: boots the quickstart server in a
# throwaway state dir and drives every step of docs/quickstart.md over real
# HTTP, including offline verification and the tamper check.
quickstart-check:
	uv run --with-requirements requirements.txt pytest tests/test_quickstart_path.py -v

# One-command live proof against an already-running quickstart server
# (terminal 1: `make quickstart`). Drives discover -> authenticate ->
# authorize -> invoke -> meter -> receipt -> replay -> audit -> govern over
# real HTTP as a self-provisioned non-admin caller, verifies both the
# success and denial receipts offline, and writes a partner handoff bundle
# to data/live-loop-proof/.
live-loop-proof:
	uv run --with-requirements requirements.txt python scripts/live_loop_proof.py $(LIVE_LOOP_PROOF_ARGS)

# Fast inner loop: trust-plane (product) tests only. Proof-surface workloads
# are skipped here — run them with `make test-all` (what CI runs) or `make test-proof`.
# `--with-requirements` makes these self-contained: uv installs the runtime +
# test deps for the run, so `make test` works on a fresh checkout without a
# separate `pip install -r requirements.txt` (deps live in requirements.txt,
# not pyproject [project.dependencies]).
test:
	uv run --with-requirements requirements.txt pytest tests/ -q -m "not proof"

test-all:
	uv run --with-requirements requirements.txt pytest tests/ -q

test-proof:
	uv run --with-requirements requirements.txt pytest tests/ -q -m proof

# Reproducible whole-application coverage baseline. Production-posture tests run
# in their dedicated CI job because they require a different environment.
coverage:
	uv run --with-requirements requirements.txt pytest tests/ -q -m "not production_trust" --cov=app --cov-report=term-missing

prove-trust-plane:
	uv run --with-requirements requirements.txt python scripts/demo_trust_plane.py --assert

# Use the real PostgreSQL process/crash proof, including its isolation guards.
# The in-process demo deliberately owns a throwaway SQLite database.
prove-trust-plane-postgres: prove-crash-recovery

# Two-process crash-consistency proof. Starts independent Uvicorn workers
# against one shared PostgreSQL database and kills a worker at durable commit
# boundaries, proving one side effect / debit / receipt, receipt-commit
# recovery, and fail-closed manual review after an ambiguous side effect
# (never an automatic redispatch).
#
# Requires DATABASE_URL=postgresql+asyncpg://... pointing at a DEDICATED,
# EMPTY database already migrated to head, STATE_BACKEND=postgres, a local/test
# ENVIRONMENT, and explicit MCP_STRESS_DB_ISOLATED=1 acknowledgment. Provision
# and migrate only the disposable database before invoking this target; this
# target never migrates or overrides the caller's isolation settings.
# The harness refuses to run otherwise: it fails closed on a
# non-PostgreSQL URL, a production-like ENVIRONMENT, a stale Alembic revision,
# or any application table that already holds rows, and it takes an advisory
# lock so two runs cannot overlap. This is the same proof CI runs.
prove-crash-recovery:
	RUN_MCP_MULTIPROCESS_TESTS=1 \
	uv run --with-requirements requirements.txt \
	  pytest tests/test_mcp_postgres_multiprocess.py -v --tb=short

# The headline demo: one consequential payout, one lost response, one retry.
# Without the boundary the vendor is paid twice; with it the retry moves no
# money, debits nothing, and returns the confirmation the agent lost.
demo-ambiguous-retry:
	uv run --with-requirements requirements.txt python scripts/demo_ambiguous_retry.py

demo-ambiguous-retry-check:
	uv run --with-requirements requirements.txt python scripts/demo_ambiguous_retry.py --assert --json

# The failure laboratory: one workflow (a vendor payout), one injected fault
# (the downstream executes, then its response is lost), measured across the
# existing integration, a correctly used native idempotency baseline, and the
# gateway. Downstream effects are counted by a record the gateway cannot
# reach; every expectation is asserted; the report is written to
# data/failure-lab/<run>/ and must say when the native baseline already
# handles the fault. See docs/failure-lab.md.
failure-lab:
	uv run --with-requirements requirements.txt python scripts/failure_lab.py $(FAILURE_LAB_ARGS)

failure-lab-check:
	uv run --with-requirements requirements.txt python scripts/failure_lab.py --json

demo-trust-plane:
	uv run --with-requirements requirements.txt python scripts/demo_trust_plane.py

demo-trust-plane-check:
	uv run --with-requirements requirements.txt python scripts/demo_trust_plane.py --assert

dogfood-trust-plane:
	uv run --with-requirements requirements.txt python scripts/dogfood_trust_plane.py

dogfood-trust-plane-check:
	uv run --with-requirements requirements.txt python scripts/dogfood_trust_plane.py --assert

red-team-trust-plane:
	uv run --with-requirements requirements.txt python scripts/red_team_trust_plane.py

red-team-trust-plane-check:
	uv run --with-requirements requirements.txt python scripts/red_team_trust_plane.py --assert

agent-ops-war-room:
	uv run --with-requirements requirements.txt python scripts/agent_ops_war_room_demo.py

agent-ops-war-room-check:
	uv run --with-requirements requirements.txt python scripts/agent_ops_war_room_demo.py --assert --json

# Fail if a comment or docstring names a symbol the tree no longer defines.
check-doc-references:
	python scripts/check_doc_references.py


trust-coverage-gate:
	scripts/trust_coverage_gate.sh

trust-release-gate:
	scripts/trust_release_gate.sh

# Live invariant suites target an operator-selected deployment. The conformance
# suite provisions persistent test rows and has no cleanup, so use staging unless
# you intend to retain that data on the selected target.
#
# trust-conformance-live asserts the invariants the product sells against a
# running instance: golden path, sequential replay, 15-way identical concurrent
# admission with safe in-progress responses, post-completion replay,
# idempotency-key conflict on a changed payload, budget denial, expired and
# forged permits, receipt and audit-chain verification, and tenant isolation.
# Requires AGENT_MIDDLEWARE_API_KEY (a bootstrap/admin key) plus either an
# explicit AGENT_MIDDLEWARE_API_URL or `TRUST_CONFORMANCE_ARGS="--api-url ..."`.
# The canonical production origin also requires `--confirm-production` in
# TRUST_CONFORMANCE_ARGS.
trust-conformance-live:
	uv run --with-requirements requirements.txt python scripts/trust_plane_conformance.py $(TRUST_CONFORMANCE_ARGS)

# adversarial-battery-live probes a deployment you operate for wallet
# isolation, invalid keys, forged receipts, permit key binding, expired
# permits, revoked keys, and replay idempotency, then revokes every key it
# minted. Requires BOOTSTRAP_KEY plus either API_URL (no default, by design) or
# `ADVERSARIAL_BATTERY_ARGS="--api-url ..."`. Remote targets must use HTTPS,
# and the canonical production origin also requires `--confirm-production` in
# ADVERSARIAL_BATTERY_ARGS. MCP-invocation checks report SKIP when no
# invokable golden-path-echo tool is exposed; over-spend containment is not
# exercised.
adversarial-battery-live:
	uv run --with-requirements requirements.txt python scripts/adversarial_battery.py $(ADVERSARIAL_BATTERY_ARGS)



# --------------------------------------------------------------------------- #
# Agent Gateway Failure Lab                                                     #
#                                                                               #
# Drives this gateway under injected failures (lost responses, severed          #
# connections, crashes at durable boundaries, concurrent retries, budget and    #
# revocation races) and measures what actually happened against a downstream    #
# ledger the gateway cannot reach. Every scenario also runs against a CORRECT   #
# native baseline, so a result where the gateway adds nothing is reachable and  #
# is reported as exactly that. See docs/failure-lab.md.                         #
# --------------------------------------------------------------------------- #

# Fast tier: cheap enough for every pull request.
failure-lab-suite:
	uv run --with-requirements requirements.txt python -m failure_lab run --tier fast --source ci_run

# Everything, including the concurrency and crash scenarios. Main, nightly, RC.
failure-lab-all:
	uv run --with-requirements requirements.txt python -m failure_lab run --source ci_run

# Full run that keeps its evidence bundle for inspection or publication.
failure-lab-evidence:
	uv run --with-requirements requirements.txt python -m failure_lab run \
	  --source internal_test --output data/failure-lab --keep --archive

# List the scenario suite with its claims, tiers and documented expectations.
failure-lab-list:
	uv run --with-requirements requirements.txt python -m failure_lab list

# Seeded property-based state-machine exploration with minimized repros.
failure-lab-explore:
	uv run --with-requirements requirements.txt python -m failure_lab explore \
	  --seed $(or $(SEED),1) --sequences $(or $(SEQUENCES),8)

# Clean-room integration judge: executable assertions, not an AI's self-report.
failure-lab-integration-check:
	uv run --with-requirements requirements.txt python -m failure_lab.integration_check.judge \
	  --candidate $(or $(CANDIDATE),failure_lab/integration_check/reference_candidate.py)

# The self-serve diagnostic, on loopback only. Refuses production-like boots.
failure-lab-diagnostic:
	uv run --with-requirements requirements.txt python -m failure_lab serve \
	  --port $(or $(PORT),8900)

# Re-check a published evidence bundle: manifest hashes plus an independent
# re-verification of every receipt it carries.
failure-lab-verify-bundle:
	uv run --with-requirements requirements.txt python -m failure_lab verify \
	  --bundle $(or $(BUNDLE),data/failure-lab)
