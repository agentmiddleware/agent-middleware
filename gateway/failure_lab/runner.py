"""One command that turns a scenario suite into an evidence bundle.

Everything here exists because the individual pieces are each only half of an
argument. A ``ScenarioResult`` is a measurement nobody can re-check; a
``Comparison`` is a reading of that measurement; a claims manifest is a list of
sentences someone is allowed to say; a bundle is the bytes a stranger needs to
disagree. :func:`run_lab` is the thing that produces all four from the same
run, so they cannot drift apart between a marketing page and the code.

Four decisions in here are load-bearing, and all four are about not lying:

**The sandbox is pinned before the application is imported.**
:func:`failure_lab.gateway.boot_standalone_environment` writes the sandbox
posture into ``os.environ`` and refuses to run once ``app.main`` is in
``sys.modules``, because the application caches its settings at import. Every
``app`` import in this module is therefore inside :func:`_execute`, after the
boot call. A module-level ``import app`` would silently produce a run against
whatever posture the ambient environment happened to have -- which is the one
failure this harness could never detect from its own output.

**A scenario that raises does not take the run with it.** It is recorded as an
``ERROR`` verdict and the suite continues. The synthetic result marks the
configurations the scenario would have exercised ``NOT_RUN`` and puts the
traceback on a separate ``harness`` row, rather than stamping ``ERROR`` on the
configuration columns:
:func:`failure_lab.report.build_comparison` treats an ``ERROR`` column as a
column that *ran*, and a crashed T03 whose naive baseline shows zero duplicate
effects would be reported as "your existing integration handled this test
correctly". An error must read as an error, never as a pass.

**An undocumented improvement fails the release too.** ``exit_status`` is
non-zero when any observed verdict differs from the documented expectation in
either direction. A P0 invariant regressing and a scenario quietly starting to
pass mean the same thing -- the documentation and the tests disagree -- and
only one of them is tempting to ignore.

**The run directory is removed when the run made it.** Cleanup runs in a
``finally``, so a failure does not leave a sandbox database behind. It removes
only a directory this function created; a caller-supplied ``run_dir`` is the
caller's, and ``rmtree`` on a path someone else named is not recoverable. That
is also why ``--output`` exists: it is how the evidence bundle outlives the
run.

The bundle lands in ``<run_dir>/evidence`` rather than in ``run_dir`` itself,
because :func:`failure_lab.evidence.verify_bundle_integrity` reports any file
in the bundle tree that the manifest does not list -- correctly, an index that
ignores extra files is not an index. The telemetry log, the claims manifest and
``run.json`` are therefore siblings of the bundle, not contents of it, and the
sandbox's own state (the gateway database and the effect ledgers) goes in
``<run_dir>/sandbox``, which is emptied at the start of every run. See
:func:`_prepare_sandbox` for why sharing it between runs breaks every
signature.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import random
import shutil
import subprocess
import tempfile
import traceback
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from failure_lab import TEST_DEFINITION_VERSION
from failure_lab import __version__ as LAB_VERSION
from failure_lab.claims import (
    ClaimsManifest,
    build_claims_manifest,
    write_claims_manifest,
)
from failure_lab.configurations import (
    ALL_CONFIGURATIONS,
    CONFIGURATION_LABELS,
    Configuration,
)
from failure_lab.evidence import (
    BundleResult,
    SecretLeakError,
    build_evidence_bundle,
    environment_secret_values,
    find_leaked_secrets,
    redact,
)
from failure_lab.report import (
    CONCLUSION_GLOSS,
    INTENDED_EXECUTIONS,
    Comparison,
    build_comparison,
)
from failure_lab.scenarios import select_scenarios
from failure_lab.scenarios.base import (
    ConfigurationResult,
    Counters,
    EventLog,
    Scenario,
    ScenarioResult,
    Verdict,
)
from failure_lab.telemetry import (
    EventName,
    TelemetryClient,
    TelemetryRejected,
    TrafficSource,
    pseudonymous_subject,
)

#: The run agreed with every documented expectation and nothing errored.
EXIT_OK = 0
#: At least one observed verdict differs from what the suite documents --
#: a regression or an unannounced improvement. Both are release blockers.
EXIT_EXPECTATION_DIVERGED = 1
#: At least one scenario raised, or a configuration errored inside one.
EXIT_SCENARIO_ERROR = 2

#: Never ``human_customer``. A person has to type that; see
#: :func:`failure_lab.telemetry.detect_traffic_source` for the same rule on the
#: detection side.
DEFAULT_TRAFFIC_SOURCE = TrafficSource.INTERNAL_TEST

BUNDLE_DIRECTORY_NAME = "evidence"
CLAIMS_FILENAME = "claims.json"
RUN_DOCUMENT_FILENAME = "run.json"

#: The sandbox's own state -- the gateway's SQLite database and the downstream
#: effect ledgers -- lives in its own subdirectory, and is deleted and remade
#: on every run. Sharing it between runs is not merely untidy: every boot mints
#: a fresh Ed25519 seed under the same fixed key id, so a second run against a
#: first run's database fails every signature with
#: ``signing_key_id_public_key_mismatch``. Measured, not theorised.
SANDBOX_DIRECTORY_NAME = "sandbox"

#: The column a scenario-level crash is recorded against. Deliberately not one
#: of the four :class:`~failure_lab.configurations.Configuration` values: no
#: configuration ran, and naming one would attribute the crash to a workload
#: that never started.
HARNESS_CONFIGURATION = "harness"

#: Versions worth recording because a change in any of them can change what
#: the gateway does under failure: the web stack that serves it, the MCP
#: client that talks to it, the transport the lab observes through, the
#: signature implementation, and the ORM that owns its durable boundaries.
TRACKED_PACKAGES = ("fastapi", "mcp", "httpx", "cryptography", "sqlmodel")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# Environment                                                                   #
# --------------------------------------------------------------------------- #


def git_commit(root: Path | str = REPOSITORY_ROOT) -> dict[str, Any]:
    """The commit the gateway was read from, or a recorded failure to find it.

    A missing git binary, a tarball checkout and a detached worktree are all
    ordinary; none of them is a reason to fail a run. What is not acceptable
    is recording ``"unknown"`` in a way that reads like a commit, so the
    failure reason travels with the field.

    The same rule applies one level down. ``git rev-parse`` can succeed while
    ``git status`` does not, and a ``dirty`` of ``None`` with an empty note
    renders exactly like a clean tree everywhere it is read. A failure to look
    gets its own sentence, so nothing downstream can present it as an
    observation that the tree was clean.
    """

    def run(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *arguments],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    commit = run("rev-parse", "HEAD")
    if commit is None:
        return {
            "commit": "unknown",
            "dirty": None,
            "note": "git rev-parse HEAD did not succeed here; the working tree "
            "the gateway was read from cannot be identified from this bundle",
        }
    status = run("status", "--porcelain")
    if status is None:
        return {
            "commit": commit,
            "dirty": None,
            "note": "git status --porcelain did not succeed here, so whether "
            "the working tree carried uncommitted changes is unknown. This is "
            "not a report that it was clean",
        }
    return {
        "commit": commit,
        "dirty": bool(status),
        "note": (
            "uncommitted changes were present, so this commit does not fully "
            "describe the code under test"
            if status
            else ""
        ),
    }


def package_versions(names: Sequence[str] = TRACKED_PACKAGES) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def collect_environment(
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    seed: int,
    traffic_source: TrafficSource,
    reproduction_command: str,
    gateway_version: str,
    selection: Mapping[str, Any],
    run_directory: Path,
) -> dict[str, Any]:
    """The facts a reader needs to decide whether this run describes their build."""
    commit = git_commit()
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "test_definition_version": TEST_DEFINITION_VERSION,
        "lab_version": LAB_VERSION,
        "gateway_version": gateway_version,
        "gateway_commit": commit["commit"],
        "gateway_commit_dirty": commit["dirty"],
        "gateway_commit_note": commit["note"],
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "packages": package_versions(),
        "seed": seed,
        # NOT ``seed_note``. ``REDACTED_KEY_PATTERN`` matches ``seed`` as a
        # substring of the key, and the integer exemption in
        # :func:`failure_lab.evidence.redact` covers the exact keys ``seed``
        # and ``random_seed`` only -- so a key named ``seed_note`` is replaced
        # wholesale with ``<redacted:seed>`` in run.json, in ``--json`` and in
        # the bundle's environment.json. The caveat that a seeded run is not a
        # reproducible run would then be missing from every artifact that
        # quotes the seed, which is the one place it has to appear. Measured on
        # a real run before this name changed.
        "reproducibility_note": (
            "the process-wide random module was seeded with this value. "
            "Identifiers minted with uuid4 and secrets are not seeded and "
            "differ per run, so this does not make a run byte-identical"
        ),
        "traffic_source": traffic_source.value,
        "counts_toward_conversion": traffic_source == TrafficSource.HUMAN_CUSTOMER,
        "reproduction_command": reproduction_command,
        "selection": dict(selection),
        "run_directory": str(run_directory),
    }


# --------------------------------------------------------------------------- #
# Results                                                                       #
# --------------------------------------------------------------------------- #


def error_result(
    scenario: Scenario, exc: BaseException, *, started_at: str, finished_at: str
) -> ScenarioResult:
    """Turn a scenario that raised out of ``run()`` into a reportable ERROR.

    The configuration rows are reconstructed exactly as
    :meth:`Scenario.run` would have built them -- ``NOT_APPLICABLE`` for the
    configurations this scenario does not exercise, ``NOT_RUN`` for the ones it
    does -- so the divergence report names only the columns that genuinely lost
    a measurement, rather than reporting a configuration the scenario never
    claimed as missing. ``NOT_RUN`` also keeps
    :func:`failure_lab.report.build_comparison` from reading empty counters as
    a clean baseline. The traceback goes on a separate ``harness`` row whose
    verdict is ``ERROR``, which is what makes ``ScenarioResult.verdict`` report
    ``ERROR`` for the scenario as a whole.
    """
    message = f"{type(exc).__name__}: {exc}"
    trace = "".join(traceback.format_exception(exc))[-4000:]
    try:
        definition_hash = scenario.definition_hash()
    except Exception:  # noqa: BLE001 - a broken definition is part of the finding
        definition_hash = ""

    configurations = [
        ConfigurationResult(
            configuration=configuration.value,
            label=CONFIGURATION_LABELS[configuration],
            verdict=(
                Verdict.NOT_RUN
                if configuration in scenario.configurations
                else Verdict.NOT_APPLICABLE
            ),
            expectation=scenario.expected.get(configuration.value, ""),
            observation=(
                (
                    "no measurements: the scenario raised before this "
                    f"configuration produced a result ({message})"
                )
                if configuration in scenario.configurations
                else scenario.inapplicable_reason
            ),
            counters=Counters(),
        )
        for configuration in ALL_CONFIGURATIONS
    ]
    configurations.append(
        ConfigurationResult(
            configuration=HARNESS_CONFIGURATION,
            label="Harness (no configuration ran)",
            verdict=Verdict.ERROR,
            expectation="the scenario runs to completion and returns a result",
            observation=f"{scenario.test_id} raised out of Scenario.run: {message}",
            counters=Counters(),
            error=trace,
        )
    )
    return ScenarioResult(
        test_id=scenario.test_id,
        title=scenario.title,
        claim=scenario.claim,
        definition_version=TEST_DEFINITION_VERSION,
        definition_hash=definition_hash,
        started_at=started_at,
        finished_at=finished_at,
        configurations=configurations,
        limitations=[
            *scenario.limitations,
            "This scenario did not run to completion. Nothing below is a "
            "measurement of the gateway; it is a record of a harness failure.",
        ],
        events=[],
        expected=dict(scenario.expected),
    )


#: Keys a scenario may set on ``ConfigurationResult.extra`` to hand the runner
#: material it cannot obtain itself. The runner closes every target before it
#: builds the bundle, so anything that needs a live gateway -- a portable
#: receipt export, the issuer's key document, a verifier report -- has to be
#: collected by the scenario while it still holds one.
PORTABLE_RECEIPTS_KEY = "portable_receipts"
TRUST_KEYS_KEY = "trust_keys"
VERIFICATION_RESULTS_KEY = "verification_results"

TRUST_KEYS_FILENAME = "trust-keys.json"


def harvest_evidence(
    results: Sequence[ScenarioResult],
    notes: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """Pull exported receipts, verifier reports and the key document out of results.

    Returns ``(receipts_by_id, verification_results, key_document)``. When
    ``notes`` is given, anything a scenario supplied under one of the harvest
    keys but this function could not read is appended to it.

    Nothing here invents evidence. When no scenario exported a portable
    receipt the bundle gets an empty ``receipts/`` and
    :func:`~failure_lab.evidence.build_evidence_bundle` writes the note saying
    the gateway's own receipt rows are not a substitute for one. That is the
    correct output for a run that produced no independently checkable
    artifact, and it is why this function does not fall back to the
    gateway-reported receipt rows that every result already carries.

    What it must not do is drop material silently. A scenario that sets
    ``portable_receipts`` to a shape this function does not understand would
    otherwise produce a bundle carrying evidence.py's note that *no receipts
    were supplied* -- a statement about the scenario that is false, and one
    nobody could tell apart from the true case. The same goes for a second,
    different ``trust_keys`` document: first-found wins, so the receipts signed
    under the other one would fail verification with nothing in the bundle to
    explain why. Both are recorded.
    """
    dropped: list[str] = []
    receipts: dict[str, Any] = {}
    verifications: list[dict[str, Any]] = []
    key_document: dict[str, Any] | None = None
    for result in results:
        for entry in result.configurations:
            origin = f"{result.test_id}/{entry.configuration}"
            extra = entry.extra or {}
            exported = extra.get(PORTABLE_RECEIPTS_KEY)
            if isinstance(exported, Mapping):
                receipts.update({str(k): v for k, v in exported.items()})
            elif isinstance(exported, Sequence) and not isinstance(
                exported, (str, bytes)
            ):
                for index, bundle in enumerate(exported):
                    identifier = (
                        str(bundle.get("receipt_id"))
                        if isinstance(bundle, Mapping) and bundle.get("receipt_id")
                        else f"{result.test_id}-{entry.configuration}-{index:03d}"
                    )
                    receipts[identifier] = bundle
            elif exported is not None:
                dropped.append(
                    f"{origin} set {PORTABLE_RECEIPTS_KEY!r} to a "
                    f"{type(exported).__name__}, which is neither a mapping nor "
                    "a list of receipt bundles; it was not written to the bundle"
                )
            reports = extra.get(VERIFICATION_RESULTS_KEY)
            if isinstance(reports, Sequence) and not isinstance(reports, (str, bytes)):
                usable = [report for report in reports if isinstance(report, Mapping)]
                if len(usable) != len(list(reports)):
                    dropped.append(
                        f"{origin} supplied {len(list(reports)) - len(usable)} "
                        f"{VERIFICATION_RESULTS_KEY!r} entries that are not "
                        "mappings; they were not written to the bundle"
                    )
                verifications.extend(
                    {
                        "test_id": result.test_id,
                        "configuration": entry.configuration,
                        **dict(report),
                    }
                    for report in usable
                )
            elif reports is not None:
                dropped.append(
                    f"{origin} set {VERIFICATION_RESULTS_KEY!r} to a "
                    f"{type(reports).__name__} rather than a list of mappings; "
                    "it was not written to the bundle"
                )
            keys = extra.get(TRUST_KEYS_KEY)
            if isinstance(keys, Mapping):
                if key_document is None:
                    key_document = dict(keys)
                elif dict(keys) != key_document:
                    dropped.append(
                        f"{origin} supplied a {TRUST_KEYS_KEY!r} document that "
                        "differs from the one already harvested. The first is "
                        "the one written beside the bundle, so a receipt signed "
                        "under the other will not verify against it"
                    )
            elif keys is not None:
                dropped.append(
                    f"{origin} set {TRUST_KEYS_KEY!r} to a "
                    f"{type(keys).__name__} rather than a mapping; no key "
                    "document was written from it"
                )
    if notes is not None:
        notes.extend(dropped)
    return receipts, verifications, key_document


def exit_status_for(results: Sequence[ScenarioResult]) -> int:
    """ERROR outranks divergence; both outrank a clean run.

    An empty ``results`` returns :data:`EXIT_OK`, because every statement this
    function makes is a statement about results it was given and there are
    none. That is *not* a run that passed, and a caller must not present it as
    one: ``python -m failure_lab run`` refuses a selection that matches no
    scenario before it starts, rather than letting a mistyped ``--tier`` exit
    zero having measured nothing.
    """
    if any(result.verdict == Verdict.ERROR for result in results):
        return EXIT_SCENARIO_ERROR
    if any(not result.matches_expectation for result in results):
        return EXIT_EXPECTATION_DIVERGED
    return EXIT_OK


@dataclass
class LabRun:
    """Everything one invocation produced, and where it put it."""

    run_id: str
    started_at: str
    finished_at: str
    environment: dict[str, Any]
    results: list[ScenarioResult]
    comparisons: list[Comparison]
    claims: ClaimsManifest
    bundle: BundleResult | None
    exit_status: int
    run_dir: Path
    run_directory_removed: bool
    claims_path: Path | None = None
    telemetry_path: Path | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def diverged(self) -> list[ScenarioResult]:
        return [result for result in self.results if not result.matches_expectation]

    @property
    def errored(self) -> list[ScenarioResult]:
        return [result for result in self.results if result.verdict == Verdict.ERROR]

    def verdict_rows(self) -> list[dict[str, Any]]:
        """One row per scenario, in the order they ran."""
        by_test: dict[str, Comparison] = {c.test_id: c for c in self.comparisons}
        rows = []
        for result in self.results:
            comparison = by_test.get(result.test_id)
            rows.append(
                {
                    "test_id": result.test_id,
                    "title": result.title,
                    "verdict": result.verdict.value,
                    "matches_expectation": result.matches_expectation,
                    "mismatches": [
                        {
                            "configuration": cfg,
                            "expected": expected,
                            "observed": observed,
                        }
                        for cfg, expected, observed in result.mismatches()
                    ],
                    "conclusion_kind": (
                        comparison.conclusion.kind.value
                        if comparison
                        else "inconclusive"
                    ),
                }
            )
        return rows

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_status": self.exit_status,
            "environment": self.environment,
            "scenarios": self.verdict_rows(),
            "comparisons": [comparison.as_dict() for comparison in self.comparisons],
            "results": [result.as_dict() for result in self.results],
            "claims": self.claims.as_dict(),
            "evidence_bundle": self.bundle.as_dict() if self.bundle else None,
            "claims_manifest_path": str(self.claims_path) if self.claims_path else None,
            "telemetry_path": str(self.telemetry_path) if self.telemetry_path else None,
            "run_directory": str(self.run_dir),
            "run_directory_removed": self.run_directory_removed,
            "errors": self.errors,
            "notes": self.notes,
        }

    def redacted_document(self) -> dict[str, Any]:
        """The run document with credential material removed.

        Anything this object hands to a file, a terminal or an HTTP response
        goes through here first. :func:`failure_lab.evidence.redact` is the
        same pass the bundle uses, so the two cannot diverge.
        """
        return redact(self.as_dict())


# --------------------------------------------------------------------------- #
# Running                                                                       #
# --------------------------------------------------------------------------- #


def _reproduction_command(
    *,
    test_ids: Sequence[str] | None,
    tier: str | None,
    seed: int,
    source: TrafficSource,
) -> str:
    parts = ["python -m failure_lab run"]
    if tier:
        parts.append(f"--tier {tier}")
    for test_id in test_ids or ():
        parts.append(f"--test {test_id}")
    parts.append(f"--seed {seed}")
    parts.append(f"--source {source.value}")
    return " ".join(parts)


class _Telemetry:
    """Records funnel events without being able to lose the run.

    :mod:`failure_lab.telemetry` raises on a property it does not recognise,
    deliberately: a silently dropped field is a leak nobody learns about. That
    is the right trade for the collector and the wrong one here -- discarding a
    completed diagnostic because an analytics dimension was malformed would
    destroy the measurement to protect the metric about the measurement. So a
    rejection is caught, recorded in the run's notes and surfaced, rather than
    swallowed or fatal.
    """

    def __init__(self, client: TelemetryClient | None, notes: list[str]) -> None:
        self.client = client
        self.notes = notes

    def record(self, name: EventName, **properties: Any) -> None:
        if self.client is None:
            return
        try:
            self.client.record(name, **properties)
        except TelemetryRejected as exc:
            self.notes.append(
                f"telemetry event {name.value!r} was refused by the collector "
                f"and not recorded: {type(exc).__name__}: {exc}"
            )


async def run_lab(
    *,
    test_ids: Sequence[str] | None = None,
    tier: str | None = None,
    run_dir: Path | str | None = None,
    keep: bool = False,
    seed: int | None = None,
    traffic_source: TrafficSource | str = DEFAULT_TRAFFIC_SOURCE,
    archive: bool = False,
    scenario_options: Mapping[str, Any] | None = None,
    reproduction_command: str | None = None,
    telemetry: bool = True,
) -> LabRun:
    """Run the selected scenarios once and write everything they produced.

    ``test_ids`` and ``tier`` compose: passing both keeps the named scenarios
    that are also in that tier. Passing neither runs the whole suite.

    ``run_dir`` is created if it does not exist and is **never removed** by
    this function -- it belongs to the caller. When it is omitted a temporary
    directory is used instead and removed on the way out unless ``keep`` is
    true, on every path including an exception.

    Raises :class:`~failure_lab.evidence.SecretLeakError` if the bundle would
    have written a credential. That one is not caught anywhere: a run whose
    evidence leaks is not a run with a caveat.
    """
    source = TrafficSource(traffic_source)
    if seed is None:
        seed = random.randrange(2**31)
    random.seed(seed)

    options = dict(scenario_options or {})
    scenarios = select_scenarios(
        list(test_ids) if test_ids else None, tier=tier, **options
    )
    selected_ids = [scenario.test_id for scenario in scenarios]
    command = reproduction_command or _reproduction_command(
        test_ids=selected_ids, tier=tier, seed=seed, source=source
    )

    created_directory = run_dir is None
    directory = (
        Path(tempfile.mkdtemp(prefix="failure-lab-run-"))
        if run_dir is None
        else Path(run_dir)
    )
    directory.mkdir(parents=True, exist_ok=True)

    try:
        return await _execute(
            scenarios=scenarios,
            requested_ids=list(test_ids or ()),
            tier=tier,
            options=options,
            directory=directory,
            seed=seed,
            source=source,
            archive=archive,
            command=command,
            telemetry_enabled=telemetry,
            created_directory=created_directory,
            keep=keep,
        )
    finally:
        if created_directory and not keep:
            shutil.rmtree(directory, ignore_errors=True)


def _prepare_sandbox(directory: Path) -> Path:
    """Give this run an empty sandbox directory inside the run directory.

    Re-running into the same ``--output DIR`` must not inherit the previous
    run's gateway database: :func:`boot_standalone_environment` mints a new
    signing seed under the same key id every time, so the stored public key no
    longer matches the private one and every receipt signature fails with
    ``signing_key_id_public_key_mismatch``.

    Only a directory that is empty or that holds a sandbox database is
    removed. Anything else is refused by name rather than deleted -- a caller
    who points ``--output`` at a directory that already means something to
    them should get an error, not an ``rmtree``.
    """
    sandbox = directory / SANDBOX_DIRECTORY_NAME
    if sandbox.exists():
        contents = list(sandbox.iterdir())
        if contents and not (sandbox / "gateway.db").exists():
            raise RuntimeError(
                f"{sandbox} already exists and does not look like a lab sandbox "
                "(no gateway.db). Refusing to delete it; choose another --output "
                "directory."
            )
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    return sandbox


async def _execute(
    *,
    scenarios: list[Scenario],
    requested_ids: list[str],
    tier: str | None,
    options: dict[str, Any],
    directory: Path,
    seed: int,
    source: TrafficSource,
    archive: bool,
    command: str,
    telemetry_enabled: bool,
    created_directory: bool,
    keep: bool,
) -> LabRun:
    # Order matters: the sandbox posture has to be in os.environ before
    # anything under app/ is imported, because the application caches its
    # settings at import time.
    from failure_lab.gateway import boot_standalone_environment

    run_id = uuid.uuid4().hex
    started_at = _now()
    sandbox = _prepare_sandbox(directory)
    admin_api_key = boot_standalone_environment(sandbox)

    from app.db.database import close_db, init_db
    from app.main import app

    from failure_lab.configurations import LabEnvironment

    gateway_version = str(getattr(app, "version", "unknown"))
    notes: list[str] = []
    errors: list[dict[str, str]] = []

    client: TelemetryClient | None = None
    if telemetry_enabled:
        client = TelemetryClient(
            directory,
            source,
            # One run is one subject. The lab has no notion of a returning
            # person, and inventing a stable identity for one would fabricate
            # retention out of repeated CI jobs.
            subject=pseudonymous_subject(run_id),
        )
    events = _Telemetry(client, notes)

    events.record(
        EventName.DIAGNOSTIC_STARTED,
        scenario_count=len(scenarios),
        tier=tier,
        definition_version=TEST_DEFINITION_VERSION,
        lab_version=LAB_VERSION,
        gateway_version=gateway_version,
        surface="cli",
    )

    if not scenarios:
        notes.append(
            "no scenarios were selected, so this run establishes nothing about "
            "the gateway. The bundle it wrote records an empty suite."
        )

    results: list[ScenarioResult] = []
    comparisons: list[Comparison] = []
    log = EventLog()

    await init_db()
    try:
        environment = LabEnvironment(
            run_dir=sandbox, app=app, admin_api_key=admin_api_key
        )
        for scenario in scenarios:
            scenario_started = _now()
            events.record(
                EventName.GATEWAY_COMPARISON_STARTED,
                test_id=scenario.test_id,
                tier=scenario.tier,
            )
            try:
                # Sequential on purpose: every scenario mounts its own gateway
                # over the same FastAPI application and the same SQLite file.
                result = await scenario.run(environment, log=log)
            except Exception as exc:  # noqa: BLE001 - a harness failure is a result
                result = error_result(
                    scenario, exc, started_at=scenario_started, finished_at=_now()
                )
                errors.append(
                    {
                        "test_id": scenario.test_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            results.append(result)
            comparison = build_comparison(result)
            comparisons.append(comparison)
            _record_scenario_events(events, result, comparison, scenario_started)
    finally:
        await close_db()

    finished_at = _now()
    environment_document = collect_environment(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        seed=seed,
        traffic_source=source,
        reproduction_command=command,
        gateway_version=gateway_version,
        selection={
            "requested_test_ids": requested_ids,
            "tier": tier,
            "selected_test_ids": [scenario.test_id for scenario in scenarios],
            "scenario_options": options,
        },
        run_directory=directory,
    )

    receipts, verifications, key_document = harvest_evidence(results, notes)

    bundle = build_evidence_bundle(
        directory / BUNDLE_DIRECTORY_NAME,
        results,
        environment=environment_document,
        receipts=receipts,
        trust_keys=key_document,
        verification_results=verifications,
        secret_values=[
            admin_api_key,
            environment.downstream_bearer_token,
            environment.control_token,
        ],
        scenarios=scenarios,
        event_log=log,
        random_seed=seed,
        test_configuration={
            "requested_test_ids": requested_ids,
            "tier": tier,
            "selected_test_ids": [scenario.test_id for scenario in scenarios],
            "scenario_options": options,
            "credits_per_call": environment.credits_per_call,
            "call_timeout_seconds": environment.call_timeout_seconds,
            "permit_max_credits": environment.permit_max_credits,
        },
        reproduction_command=command,
        archive=archive,
    )

    # Preserve the sibling for callers using its original location. The bundle
    # now carries its own manifest-covered, leak-scanned copy for offline use.
    # Write this only after the bundle's secret scan has succeeded.
    if key_document is not None:
        (directory / TRUST_KEYS_FILENAME).write_text(
            _json_text(redact(key_document)), encoding="utf-8"
        )

    claims = build_claims_manifest(
        results,
        environment=environment_document,
        version=gateway_version,
        tested_at=finished_at,
        evidence={
            "bundle_directory": str(bundle.directory),
            "manifest_sha256": bundle.manifest_sha256,
            "archive": str(bundle.archive_path) if bundle.archive_path else None,
        },
    )
    claims_path = write_claims_manifest(directory / CLAIMS_FILENAME, claims)

    status = exit_status_for(results)
    run = LabRun(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        environment=environment_document,
        results=results,
        comparisons=comparisons,
        claims=claims,
        bundle=bundle,
        exit_status=status,
        run_dir=directory,
        run_directory_removed=created_directory and not keep,
        claims_path=claims_path,
        telemetry_path=client.local_sink.path if client else None,
        errors=errors,
        notes=notes,
    )

    events.record(
        EventName.DIAGNOSTIC_COMPLETED,
        scenario_count=len(results),
        outcome="success" if status == EXIT_OK else "failure",
        definition_version=TEST_DEFINITION_VERSION,
        gateway_version=gateway_version,
        lab_version=LAB_VERSION,
        tier=tier,
    )

    run_document_path = directory / RUN_DOCUMENT_FILENAME
    run_document_path.write_text(_json_text(run.redacted_document()), encoding="utf-8")

    _assert_siblings_are_clean(
        [
            run_document_path,
            directory / TRUST_KEYS_FILENAME,
            claims_path,
            claims_path.with_suffix(".md"),
            run.telemetry_path,
        ],
        [
            admin_api_key,
            environment.downstream_bearer_token,
            environment.control_token,
            *environment_secret_values(),
        ],
    )
    return run


def _assert_siblings_are_clean(
    paths: Sequence[Path | None], secret_values: Sequence[str]
) -> None:
    """Scan the files written *beside* the bundle for the run's own secrets.

    :func:`~failure_lab.evidence.build_evidence_bundle` scans its own bytes
    and refuses to leave a leaking bundle on disk. Nothing scanned the four
    files this module writes next to it, and they are not covered by that
    check by construction: ``trust-keys.json`` holds a document that never
    enters the bundle at all, and ``claims.json``/``claims.md`` are generated
    after it. Both go through :func:`~failure_lab.evidence.redact`, which
    catches a credential by key name or by shape -- and the downstream bearer
    token and the control token are ``secrets.token_urlsafe(24)``, which has
    no shape. A scenario putting one of them somewhere ``redact`` does not
    look was demonstrated to land it in ``trust-keys.json`` verbatim while the
    run still exited zero.

    A leaking file is deleted before the raise, for the same reason the bundle
    is: the failure has to be recoverable by re-running, not by remembering to
    delete something. The sandbox is deliberately not scanned -- it is the
    gateway's live database, not published evidence, and it is where the
    credentials the run minted are supposed to be.
    """
    present = [path for path in paths if path is not None and path.is_file()]
    leaks = find_leaked_secrets(present, secret_values)
    if not leaks:
        return
    leaked_names = {str(leak["path"]) for leak in leaks}
    for path in present:
        if path.name in leaked_names:
            path.unlink(missing_ok=True)
    raise SecretLeakError(leaks)


def _json_text(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, default=str) + "\n"


def _record_scenario_events(
    events: _Telemetry,
    result: ScenarioResult,
    comparison: Comparison,
    started_at: str,
) -> None:
    """Emit the funnel events one scenario produced.

    ``baseline_passed`` / ``baseline_failed`` are taken from the
    ``A_direct_naive`` column, because that column *is* "the caller's own
    integration under test" in this harness -- a simulated one, recorded under
    a non-customer traffic source so it can never reach a conversion
    numerator.

    The two events are not a partition of "duplicates or not". A baseline that
    produced *fewer* downstream effects than the one the operation intended
    did not survive the injected failure either -- the business action simply
    never happened -- and ``baseline_passed`` describes it as "the caller's
    own integration survived". So a short baseline emits neither event and is
    recorded as a note instead. Emitting ``baseline_failed`` for it would be
    the flattering reading, since every ``baseline_failed`` is a reason to buy
    the product; emitting ``baseline_passed`` would be the wrong one. The
    honest answer is that the run did not measure what either event claims.
    """
    duration_ms = _elapsed_ms(started_at, result.finished_at)
    events.record(
        EventName.GATEWAY_COMPARISON_COMPLETED,
        test_id=result.test_id,
        verdict=result.verdict.value,
        matches_expectation=result.matches_expectation,
        conclusion_kind=comparison.conclusion.kind.value,
        duration_ms=duration_ms,
    )
    existing = next(
        (
            column
            for column in comparison.columns
            if column.configuration == Configuration.DIRECT_NAIVE.value and column.ran
        ),
        None,
    )
    if existing is None:
        return
    if existing.duplicate_effects:
        name = EventName.BASELINE_FAILED
    elif existing.downstream_effects >= INTENDED_EXECUTIONS:
        name = EventName.BASELINE_PASSED
    else:
        events.notes.append(
            f"{result.test_id}: the {existing.configuration} baseline recorded "
            f"{existing.downstream_effects} downstream effect(s) where "
            f"{INTENDED_EXECUTIONS} was intended, and no duplicate. Neither "
            "baseline_passed nor baseline_failed was emitted: a baseline that "
            "never executed the operation did not survive the failure, and it "
            "did not duplicate either"
        )
        return
    events.record(
        name,
        test_id=result.test_id,
        configuration=existing.configuration,
        duplicate_effects=existing.duplicate_effects,
        downstream_executions=existing.downstream_effects,
        unresolved_operations=existing.unresolved_operations,
    )


def _elapsed_ms(started_at: str, finished_at: str) -> float | None:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return round((end - start).total_seconds() * 1000, 3)


def summary_lines(run: LabRun) -> list[str]:
    """The short, factual stdout summary: what ran, what it concluded, where it went."""
    lines: list[str] = []
    rows = run.verdict_rows()
    lines.append(
        f"run {run.run_id}  definitions {TEST_DEFINITION_VERSION}  seed {run.environment['seed']}"
    )
    # Tri-state on purpose. ``dirty`` is None when git status could not be
    # run, and printing nothing for that case renders a tree nobody inspected
    # exactly like a clean one.
    dirty = run.environment.get("gateway_commit_dirty")
    tree = (
        "  (working tree dirty)"
        if dirty
        else ("" if dirty is False else "  (working tree state unknown)")
    )
    lines.append(
        f"gateway {run.environment['gateway_version']} "
        f"@ {str(run.environment['gateway_commit'])[:12]}{tree}"
    )
    lines.append(
        f"traffic source {run.environment['traffic_source']} (not counted as a customer)"
        if run.environment["traffic_source"] != TrafficSource.HUMAN_CUSTOMER.value
        else "traffic source human_customer (declared explicitly by the caller)"
    )
    lines.append("")
    if not run.results:
        lines.append("NO SCENARIOS WERE SELECTED. This run establishes nothing.")
    else:
        lines.append(f"{'TEST':<6}{'VERDICT':<16}{'CONCLUSION':<34}TITLE")
        for row in rows:
            flag = (
                ""
                if row["matches_expectation"]
                else "  <- differs from documented expectation"
            )
            lines.append(
                f"{row['test_id']:<6}{row['verdict']:<16}{row['conclusion_kind']:<34}"
                f"{row['title']}{flag}"
            )
        # The conclusion column is a slug, and two of its values -- "the
        # existing integration handled it" and "correct native controls
        # handled it" -- are the ones that say the product was not needed for
        # that test. Printed bare next to a PASS they read as wins. The gloss
        # is report.py's own sentence for each kind, so the terminal cannot
        # drift from the bundle.
        kinds = sorted({row["conclusion_kind"] for row in rows})
        for kind in kinds:
            lines.append(f"  {kind:<34}{CONCLUSION_GLOSS.get(kind, '')}")
    lines.append("")
    for row in rows:
        for mismatch in row["mismatches"]:
            lines.append(
                f"  {row['test_id']} {mismatch['configuration']}: documented "
                f"{mismatch['expected']}, observed {mismatch['observed']}"
            )
    for error in run.errors:
        lines.append(f"  {error['test_id']} raised: {error['error']}")
    if run.errors or any(row["mismatches"] for row in rows):
        lines.append("")
    if run.bundle is not None:
        lines.append(f"evidence bundle:  {run.bundle.directory}")
        lines.append(f"manifest sha256:  {run.bundle.manifest_sha256}")
        if run.bundle.archive_path:
            lines.append(f"archive:          {run.bundle.archive_path}")
    if run.claims_path is not None:
        lines.append(f"claims manifest:  {run.claims_path}")
    if run.telemetry_path is not None:
        lines.append(f"telemetry:        {run.telemetry_path}")
    if run.run_directory_removed:
        lines.append("")
        lines.append(
            "The run directory was temporary and has been removed, taking the "
            "bundle with it. Pass --output DIR or --keep to retain it."
        )
    for note in run.notes:
        lines.append(f"note: {note}")
    lines.append("")
    lines.append(
        "Verdicts are compared against the documented expectation, so an "
        "unannounced improvement fails this run for the same reason a "
        "regression does. No risk score is computed anywhere."
    )
    return lines


__all__ = [
    "BUNDLE_DIRECTORY_NAME",
    "CLAIMS_FILENAME",
    "DEFAULT_TRAFFIC_SOURCE",
    "EXIT_EXPECTATION_DIVERGED",
    "EXIT_OK",
    "EXIT_SCENARIO_ERROR",
    "HARNESS_CONFIGURATION",
    "PORTABLE_RECEIPTS_KEY",
    "RUN_DOCUMENT_FILENAME",
    "SANDBOX_DIRECTORY_NAME",
    "TRACKED_PACKAGES",
    "TRUST_KEYS_FILENAME",
    "TRUST_KEYS_KEY",
    "VERIFICATION_RESULTS_KEY",
    "LabRun",
    "collect_environment",
    "error_result",
    "exit_status_for",
    "git_commit",
    "harvest_evidence",
    "package_versions",
    "run_lab",
    "summary_lines",
]
