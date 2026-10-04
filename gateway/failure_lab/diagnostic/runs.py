"""Executing one diagnostic run, and destroying everything it touched.

This is deliberately *not* a call to :func:`failure_lab.runner.run_lab`, and
the reason is the whole design of this module.

**The boot happens once per process.**
:func:`~failure_lab.gateway.boot_standalone_environment` refuses to run once
``app.main`` is in ``sys.modules``, because the application caches its settings
at import time. ``run_lab`` boots on every call, which is correct for a CLI
that exits afterwards and fatal for a server that answers a second request. So
the sandbox is booted once, here, behind :func:`sandbox`, and every run after
the first reuses it.

**The step stream has to be live.** ``run_lab`` returns after everything has
finished; a page that wants to show an experiment happening needs the events
while they are being written. :class:`_StreamingLog` is an
:class:`~failure_lab.scenarios.base.EventLog` that calls back on every
``emit``, so the same entries that end up in the evidence bundle are what the
browser reads, in the order the scenario wrote them. Nothing is scripted and
nothing is paced.

**Each run gets its own ledger directory.** The effect ledgers are named after
the configuration and the test id and live in ``LabEnvironment.run_dir``. Two
runs sharing that directory would share ledger files, and the second run would
count the first run's executions as its own -- a silent, plausible,
catastrophic measurement error. A fresh directory per run is what makes the
numbers mean anything.

**The run directory is removed on every path.** It holds a sandbox database, an
effect ledger, and an evidence bundle whose manifest names secret-shaped
environment values it scanned for. The bundle is archived and read into memory
*before* the directory is deleted in a ``finally``, so what the visitor
downloads outlives the run and nothing else does.

**A leak is fatal, not a caveat.**
:func:`~failure_lab.evidence.build_evidence_bundle` raises
:class:`~failure_lab.evidence.SecretLeakError` if a known secret survived
redaction into the written bytes. That exception is not caught here. The run is
reported as failed and no report is served, because the alternative is serving
a report with a credential in it.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import random
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from failure_lab.diagnostic.answer import DiagnosticAnswer, build_answer
from failure_lab.diagnostic.steps import Step, StepStream
from failure_lab.evidence import build_evidence_bundle, redact, redact_text
from failure_lab.report import Comparison, build_comparison, render_run_html
from failure_lab.runner import collect_environment, harvest_evidence
from failure_lab.scenarios import (
    FAST_SCENARIO_IDS,
    EventLog,
    Scenario,
    ScenarioResult,
    Verdict,
    select_scenarios,
)
from failure_lab.telemetry import TrafficSource

logger = logging.getLogger("failure_lab.diagnostic")

#: The environment name this process was started with, captured before anything
#: can overwrite it. :func:`~failure_lab.gateway.boot_standalone_environment`
#: sets ``ENVIRONMENT=local`` unconditionally, so a guard that read the variable
#: after the first run would always find a development posture and always pass.
STARTUP_ENVIRONMENT: str | None = os.environ.get("ENVIRONMENT")

#: Scenarios a web caller may select, narrowed to the fast tier at import. A
#: curated list alone would go stale the day somebody re-tiers a scenario; the
#: intersection cannot.
CURATED_WEB_SCENARIOS: tuple[str, ...] = ("T02", "T03", "T06")
PUBLIC_SCENARIOS: tuple[str, ...] = tuple(
    test_id for test_id in CURATED_WEB_SCENARIOS if test_id in FAST_SCENARIO_IDS
)

#: The default check. T03 is the failure in the PRD's own headline -- the tool
#: executes, the response disappears -- and it is the one where a correct
#: native baseline can beat the product outright.
DEFAULT_SCENARIOS: tuple[str, ...] = ("T03",)

#: The most scenarios one web request may ask for. A cap, not a preference: a
#: public endpoint that can be handed a long list is a public endpoint that can
#: be handed a long list by somebody who means it badly.
MAX_SCENARIOS_PER_RUN = 3

#: Wall clock for one run, after which it is cancelled and reported as failed.
#: A partial run is not a result and is never rendered as one.
RUN_TIMEOUT_SECONDS = 300.0

#: Bundles larger than this are not retained for download. The whole archive
#: lives in memory so the run directory can be deleted; an unbounded one would
#: make the download the resource a visitor exhausts.
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


class ProductionRefused(RuntimeError):
    """This process looks like production, so no sandbox was booted."""


class RunRefused(ValueError):
    """A run request was refused before anything was executed."""

    def __init__(self, reason: str, *, status_code: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def assert_not_production_like(environment: str | None = None) -> None:
    """Refuse to boot a sandbox in anything that looks like production.

    Imported lazily: nothing under ``app/`` may be imported at module scope in
    this package, because the import order is what lets the sandbox pin its
    settings before the application reads them.
    """
    from app.core.trust_mode import is_production_like_environment

    name = STARTUP_ENVIRONMENT if environment is None else environment
    if is_production_like_environment(name):
        raise ProductionRefused(
            f"ENVIRONMENT={name!r} is production-like. The diagnostic boots a "
            "throwaway gateway with an ephemeral signing key, a local SQLite "
            "database and metadata create_all enabled; none of that belongs in "
            "a production process. Run it on a workstation."
        )


def select_web_scenarios(
    requested: Any, *, allowed: tuple[str, ...] = PUBLIC_SCENARIOS
) -> list[str]:
    """Validate a requested selection against the public allowlist."""
    if requested in (None, "", []):
        return _refuse_empty(
            [test_id for test_id in DEFAULT_SCENARIOS if test_id in allowed]
            or list(allowed[:1])
        )
    if isinstance(requested, str):
        requested = [requested]
    if not isinstance(requested, list):
        raise RunRefused("'scenarios' must be a list of test ids")
    chosen: list[str] = []
    for item in requested:
        if not isinstance(item, str):
            raise RunRefused("'scenarios' must contain test ids as strings")
        test_id = item.strip().upper()
        if test_id not in allowed:
            raise RunRefused(
                f"{test_id} is not offered on this surface. This page runs "
                f"{', '.join(allowed)}; the rest of the suite runs from the "
                "command line, where nobody is serving it to strangers."
            )
        if test_id not in chosen:
            chosen.append(test_id)
    if len(chosen) > MAX_SCENARIOS_PER_RUN:
        raise RunRefused(
            f"at most {MAX_SCENARIOS_PER_RUN} scenarios may be requested in one "
            f"run; {len(chosen)} were asked for"
        )
    return _refuse_empty(chosen)


def _refuse_empty(chosen: list[str]) -> list[str]:
    """An empty selection is a refusal, never a run.

    :func:`~failure_lab.scenarios.select_scenarios` treats a falsy selection as
    *every* scenario -- including the slow tier -- so an empty list handed to
    :func:`execute` does not run nothing, it runs all fourteen from one web
    request. The allowlist can legitimately empty out (it is
    ``CURATED_WEB_SCENARIOS`` intersected with ``FAST_SCENARIO_IDS``, so
    re-tiering all three empties it), which is exactly the case where the
    failure has to be loud.
    """
    if not chosen:
        raise RunRefused(
            "no scenario on this surface's allowlist is currently offered, so "
            "there is nothing to run. The allowlist is the curated web set "
            "intersected with the fast tier; if every curated scenario has been "
            "re-tiered as slow it is empty. Run the suite from the command line.",
            status_code=503,
        )
    return chosen


# --------------------------------------------------------------------------- #
# The process-wide sandbox                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class Sandbox:
    """One booted gateway, shared by every run in this process."""

    directory: Path
    app: Any
    admin_api_key: str
    gateway_version: str
    #: Secret-shaped values that must never appear in anything served. The
    #: admin key plus whatever the boot exported under a sensitive name, which
    #: is how the ephemeral signing seed is covered even though no caller here
    #: ever holds it.
    secret_values: tuple[str, ...]
    database_ready: bool = False

    async def ensure_database(self) -> None:
        if self.database_ready:
            return
        from app.db.database import init_db

        await init_db()
        self.database_ready = True

    async def close(self) -> None:
        if not self.database_ready:
            return
        from app.db.database import close_db

        await close_db()
        self.database_ready = False

    def destroy(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


_SANDBOX: Sandbox | None = None
#: Set once :func:`shutdown_sandbox` has run. A retired sandbox cannot be
#: replaced -- see :func:`sandbox` -- and saying so is better than letting the
#: next caller hit ``boot_standalone_environment``'s own refusal, which names
#: an import order rather than the thing that actually happened.
_SANDBOX_RETIRED = False


def _destroy_sandbox_at_exit() -> None:
    """Remove the sandbox directory when the process ends.

    Registered at boot rather than run from an application's shutdown, because
    the directory's lifetime is the process's: it holds the only gateway this
    process will ever be able to boot. See :func:`release_sandbox`.
    """
    box = _SANDBOX
    if box is not None:
        box.destroy()


async def sandbox() -> Sandbox:
    """Boot the sandbox once, then hand back the same one forever.

    The singleton is not a cache. ``boot_standalone_environment`` can only run
    before ``app.main`` is imported, which happens exactly once per process, so
    the second boot is not slow -- it is impossible.
    """
    global _SANDBOX
    if _SANDBOX is None and _SANDBOX_RETIRED:
        raise RuntimeError(
            "this process's sandbox was shut down and cannot be rebuilt: "
            "boot_standalone_environment refuses to run once app.main has been "
            "imported, which it has. Start a new process to run another check."
        )
    if _SANDBOX is None:
        assert_not_production_like()
        from failure_lab.evidence import environment_secret_values
        from failure_lab.gateway import boot_standalone_environment

        directory = Path(tempfile.mkdtemp(prefix="failure-lab-diagnostic-"))
        gateway_directory = directory / "gateway"
        admin_api_key = boot_standalone_environment(gateway_directory)

        from app.main import app

        _SANDBOX = Sandbox(
            directory=directory,
            app=app,
            admin_api_key=admin_api_key,
            gateway_version=str(getattr(app, "version", "unknown")),
            secret_values=tuple({admin_api_key, *environment_secret_values()}),
        )
        atexit.register(_destroy_sandbox_at_exit)
        logger.info(
            "diagnostic sandbox booted in %s (gateway version %s)",
            directory,
            _SANDBOX.gateway_version,
        )
    await _SANDBOX.ensure_database()
    return _SANDBOX


#: Per-run credentials the response guard must also know about. The sandbox's
#: own secrets are booted once and live on :class:`Sandbox`; the downstream
#: bearer token and the control token are minted fresh per
#: :class:`~failure_lab.configurations.LabEnvironment`, are
#: ``secrets.token_urlsafe(24)`` -- shapeless, so no value-shape rule in
#: :func:`~failure_lab.evidence.redact_text` can recognise one in free text --
#: and were being handed to the evidence bundle's leak scan but not to the
#: guard on the HTTP bodies. That held the downloadable artifact to a stricter
#: standard than the page, which is backwards: the page is the thing a visitor
#: sees without asking for it.
#:
#: Bounded, and deliberately larger than :data:`MAX_RETAINED_RUNS` times two,
#: so no retained result can outlive the guard entry for its own run.
RETAINED_RUN_SECRETS = 64
_RUN_SECRETS: deque[str] = deque(maxlen=RETAINED_RUN_SECRETS)


def remember_run_secrets(*values: str) -> None:
    """Register per-run credential values with the response guard."""
    for value in values:
        if value and len(value) >= 8 and value not in _RUN_SECRETS:
            _RUN_SECRETS.append(value)


def known_secret_values() -> tuple[str, ...]:
    """Secret-shaped values this process holds, for the response guard.

    Empty before the first run, which is correct: nothing served before a
    sandbox exists can contain a sandbox credential.
    """
    sandbox_values = _SANDBOX.secret_values if _SANDBOX is not None else ()
    return (*sandbox_values, *_RUN_SECRETS)


async def release_sandbox() -> None:
    """Let go of the sandbox's database without retiring the sandbox.

    What one application's shutdown owns and what the process owns are not the
    same thing, and conflating them is unrecoverable here.
    ``boot_standalone_environment`` refuses to run once ``app.main`` is in
    ``sys.modules``, so a sandbox destroyed on the way down cannot be rebuilt
    on the way up: any process that constructs a second application -- a test
    module, an embedder, a supervisor that restarts the surface in place --
    would get ``boot_standalone_environment must run before app.main is
    imported`` on its first run and stay broken for the life of the process.

    The database engine is the only part that is genuinely reopenable
    (``close_db`` drops it, ``init_db`` builds it again on the next run), so
    that is the only part released here. The directory is removed at process
    exit by :func:`_destroy_sandbox_at_exit`, which is when it is actually
    finished with.
    """
    if _SANDBOX is not None:
        await _SANDBOX.close()


async def shutdown_sandbox() -> None:
    """Close the database, delete the sandbox directory, retire the singleton.

    Final for the life of the process: see :func:`sandbox`. Called by a caller
    that knows nothing else will run here, never from an application lifespan.
    """
    global _SANDBOX, _SANDBOX_RETIRED
    _RUN_SECRETS.clear()
    _SANDBOX_RETIRED = True
    if _SANDBOX is None:
        return
    await _SANDBOX.close()
    _SANDBOX.destroy()
    _SANDBOX = None


# --------------------------------------------------------------------------- #
# One run                                                                       #
# --------------------------------------------------------------------------- #


class _StreamingLog(EventLog):
    """An event log that tells somebody every time a scenario writes to it.

    Subclassed rather than polled: polling a list length between awaits would
    work and would also silently start dropping entries the moment a scenario
    emitted two of them without yielding.
    """

    def __init__(self, on_event: Any) -> None:
        super().__init__()
        self._on_event = on_event

    def emit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        event = super().emit(*args, **kwargs)
        self._on_event(event)
        return event


@dataclass
class RunRecord:
    """Everything one run produced, and everything a page needs to show it."""

    run_id: str
    scenarios: list[str]
    seed: int
    created_at: float
    #: ``running`` until the run ends, then ``complete`` or ``failed``.
    state: str = "running"
    steps: list[Step] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    answer: DiagnosticAnswer | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    document: dict[str, Any] = field(default_factory=dict)
    report_html: str = ""
    archive: bytes | None = None
    archive_name: str = "evidence.zip"
    archive_note: str = ""
    exit_status: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    _waiters: list[asyncio.Future[None]] = field(default_factory=list, repr=False)

    @property
    def finished(self) -> bool:
        return self.state != "running"

    def append(self, steps: list[Step]) -> None:
        if not steps:
            return
        self.steps.extend(steps)
        self.wake()

    def wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait_for_change(self, *, after: int, timeout: float) -> None:
        """Block until there is a step past ``after``, the run ends, or timeout."""
        if len(self.steps) > after or self.finished:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            pass
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def status_document(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "scenarios": list(self.scenarios),
            "step_count": len(self.steps),
            "error": self.error,
            "notes": list(self.notes),
        }


def reproduction_command(scenarios: list[str], seed: int) -> str:
    tests = " ".join(f"--test {test_id}" for test_id in scenarios)
    return f"python -m failure_lab run {tests} --seed {seed}".replace("  ", " ")


async def execute(
    record: RunRecord,
    *,
    state_dir: Path,
    scenario_options: dict[str, Any] | None = None,
) -> RunRecord:
    """Run the selected scenarios in the sandbox and fill in ``record``.

    Never raises for an ordinary failure: a scenario that blows up, a timeout
    and a cancelled run are all reported through ``record.state`` and
    ``record.error``, because a diagnostic that can only report success is not
    a diagnostic. A
    :class:`~failure_lab.evidence.SecretLeakError` is the one exception, and it
    is re-raised after cleanup.
    """
    from failure_lab.configurations import LabEnvironment

    if not record.scenarios:
        # ``select_scenarios`` reads a falsy selection as "every scenario", so
        # an empty list here is not an empty run -- it is the whole suite,
        # slow tier included, off one request. Refused rather than expanded.
        record.state = "failed"
        record.error = (
            "this run was started with no scenarios selected, so nothing ran. "
            "An empty selection is refused rather than expanded into the whole "
            "suite."
        )
        record.wake()
        return record

    box = await sandbox()
    started_at = _now()
    run_dir = Path(tempfile.mkdtemp(prefix=f"run-{record.run_id[:8]}-", dir=state_dir))
    ledger_dir = run_dir / "ledgers"
    ledger_dir.mkdir(parents=True)

    stream = StepStream()
    record.append(
        stream.push(
            {
                "step": "diagnostic.start",
                "message": (
                    "Booting a disposable sandbox: a simulated refund tool with its own "
                    "ledger, a fault-injection layer in front of it, and the gateway in "
                    "front of that."
                ),
                "at": started_at,
            }
        )
    )

    random.seed(record.seed)
    scenarios: list[Scenario] = select_scenarios(
        record.scenarios, **(scenario_options or {})
    )
    log = _StreamingLog(lambda event: record.append(stream.push(event)))
    results: list[ScenarioResult] = []

    try:
        environment = LabEnvironment(
            run_dir=ledger_dir, app=box.app, admin_api_key=box.admin_api_key
        )
        secret_values = [
            *box.secret_values,
            environment.downstream_bearer_token,
            environment.control_token,
        ]
        # Registered before the first scenario event can be streamed, so the
        # guard on every served body knows this run's credentials for as long
        # as its result is addressable -- not just when the bundle is written.
        remember_run_secrets(
            environment.downstream_bearer_token, environment.control_token
        )
        try:
            # Some of the machinery under test writes startup lines straight to
            # stdout. In a server that is the response stream's neighbour, so it
            # goes to stderr with everything else.
            with contextlib.redirect_stdout(sys.stderr):
                await asyncio.wait_for(
                    _run_scenarios(scenarios, environment, log, results),
                    timeout=RUN_TIMEOUT_SECONDS,
                )
        except TimeoutError:
            record.state = "failed"
            record.error = (
                f"the run did not finish within {RUN_TIMEOUT_SECONDS:.0f} seconds "
                "and was abandoned. Nothing is reported: a partial run is not a "
                "result."
            )
            return record
        except asyncio.CancelledError:
            record.state = "failed"
            record.error = "the run was cancelled before it finished."
            raise
        except Exception as exc:  # noqa: BLE001 - a harness failure is a result
            record.state = "failed"
            # Redacted where it is captured, not where it is rendered. An
            # exception message and a traceback are the one part of a run that
            # nobody wrote on purpose: they quote whatever string the failing
            # call had in its hand, which on this harness includes an
            # ``Authorization`` header and a minted admin key. ``record.error``
            # and ``record.notes`` reach the result page, the JSON document and
            # the SSE terminal event, so redacting at any one of those three
            # would leave the other two.
            record.error = redact_text(f"{type(exc).__name__}: {exc}")
            record.notes.append(redact_text(traceback.format_exc()[-2000:]))
            logger.exception("diagnostic run %s failed", record.run_id)
            return record

        record.comparisons = [build_comparison(result) for result in results]
        record.answer = build_answer(record.comparisons)
        record.exit_status = _exit_status(results)

        finished_at = _now()
        command = reproduction_command(record.scenarios, record.seed)
        record.environment = collect_environment(
            run_id=record.run_id,
            started_at=started_at,
            finished_at=finished_at,
            seed=record.seed,
            traffic_source=_traffic_source_placeholder(),
            reproduction_command=command,
            gateway_version=box.gateway_version,
            selection={"test_ids": list(record.scenarios), "surface": "web"},
            run_directory=run_dir,
        )
        redacted_environment = redact(record.environment)
        record.report_html = redact_text(
            render_run_html(record.comparisons, environment=redacted_environment)
        )

        receipts, verifications, key_document = harvest_evidence(results)
        bundle = build_evidence_bundle(
            run_dir / "evidence",
            results,
            environment=record.environment,
            secret_values=secret_values,
            include_environment_secrets=True,
            scenarios=scenarios,
            event_log=log.events,
            receipts=receipts,
            trust_keys=key_document,
            verification_results=verifications,
            random_seed=record.seed,
            test_configuration={"surface": "web", "scenarios": list(record.scenarios)},
            reproduction_command=command,
            archive=True,
        )
        record.archive_name = f"agent-action-safety-check-{record.run_id[:8]}.zip"
        record.archive, record.archive_note = _read_archive(bundle.archive_path)
        record.document = redact(
            {
                "run_id": record.run_id,
                "answer": record.answer.as_dict(),
                "environment": record.environment,
                "comparisons": [c.as_dict() for c in record.comparisons],
                "steps": [step.as_dict() for step in record.steps],
                "evidence_manifest_sha256": bundle.manifest_sha256,
                "exit_status": record.exit_status,
                "notes": record.notes,
            }
        )
        record.state = "complete"
        return record
    finally:
        # Deterministic, on every path: the sandbox database, the effect
        # ledgers and the staged bundle all live under here.
        shutil.rmtree(run_dir, ignore_errors=True)
        record.wake()


async def _run_scenarios(
    scenarios: list[Scenario],
    environment: Any,
    log: EventLog,
    results: list[ScenarioResult],
) -> None:
    """Sequential on purpose: every scenario mounts its own gateway."""
    for scenario in scenarios:
        results.append(await scenario.run(environment, log=log))


def _read_archive(path: Path | None) -> tuple[bytes | None, str]:
    if path is None or not path.exists():
        return None, "no archive was produced for this run."
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        return None, (
            f"the evidence bundle was {size} bytes, over this surface's "
            f"{MAX_ARCHIVE_BYTES} byte retention limit, so it was not kept. "
            "Reproduce the run from the command line to get it."
        )
    return path.read_bytes(), ""


def _exit_status(results: list[ScenarioResult]) -> int:
    if any(result.verdict == Verdict.ERROR for result in results):
        return 2
    if any(not result.matches_expectation for result in results):
        return 1
    return 0


def _traffic_source_placeholder() -> TrafficSource:
    """The source recorded in the run environment document.

    The run itself has no traffic source: the *request* does, and the server
    decides that per request from what the request carries. Recording the
    server's default here would stamp every run with whatever the operator
    configured, including runs that were never a browser session. The
    environment document gets ``unknown`` and the telemetry stream carries the
    real answer, per event, where it can be audited.
    """
    return TrafficSource.UNKNOWN


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return uuid.uuid4().hex


def new_seed() -> int:
    """Draw and record a seed, so the reproduction command is always complete.

    Seeding ``random`` does not make a run byte-identical -- ``uuid4`` and
    ``secrets`` are not seeded -- and
    :func:`~failure_lab.runner.collect_environment` writes that caveat next to
    the value rather than letting the field imply determinism it does not have.
    """
    return random.randrange(2**31)


def elapsed_seconds(record: RunRecord) -> float:
    return time.monotonic() - record.created_at


__all__ = [
    "CURATED_WEB_SCENARIOS",
    "DEFAULT_SCENARIOS",
    "MAX_ARCHIVE_BYTES",
    "MAX_SCENARIOS_PER_RUN",
    "PUBLIC_SCENARIOS",
    "RETAINED_RUN_SECRETS",
    "RUN_TIMEOUT_SECONDS",
    "STARTUP_ENVIRONMENT",
    "ProductionRefused",
    "RunRecord",
    "RunRefused",
    "Sandbox",
    "assert_not_production_like",
    "elapsed_seconds",
    "execute",
    "new_run_id",
    "new_seed",
    "release_sandbox",
    "remember_run_secrets",
    "reproduction_command",
    "sandbox",
    "select_web_scenarios",
    "shutdown_sandbox",
]
