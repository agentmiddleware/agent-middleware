"""The downloadable evidence bundle: what a stranger needs to re-check a run.

A run report is an assertion. A bundle is the working: the ordered event log,
the downstream system of record's own rows, the gateway's own account of
itself *labelled as such*, the exported receipts, an independent verifier's
three-claim output for each of them, and a sha256 index so a reader can prove
the copy they hold is the copy that was written.

Four rules shape this module, all of them consequences of the lab's only
selling point -- that its numbers are observations rather than claims:

* **Provenance is written next to every number.** ``downstream-effects.json``
  says it came from the tool's own ledger; ``gateway-events.json`` says in its
  first key that it is the gateway's self-report. A reader who skips the prose
  still cannot mistake one for the other.
* **Redaction happens before bytes hit the disk**, over the whole document
  tree, keyed on both the field name and the value's shape -- and then a
  self-check re-reads every written byte looking for the run's actual secrets.
  The self-check is the part that matters: the pattern rules are a filter, the
  scan is the proof. A leak deletes the staged bundle and raises.
* **The manifest does not hash itself.** No index can. ``manifest.json`` says
  so in its ``integrity`` section rather than implying a completeness it does
  not have; an out-of-band hash of ``manifest.json`` is the only thing that
  makes the rest tamper-evident, and the bundle says that too.
* **Absence is recorded, not filled in.** A run that exported no portable
  receipts gets an empty ``receipts/`` and a manifest note saying so, never a
  reconstruction from the gateway's own receipt rows.

Layout (exactly these paths)::

    manifest.json            index of every other file: sha256 + byte size
    environment.json         software versions, timestamps, configuration
    test-definition.json     every scenario's definition() + definition_hash
    event-log.jsonl          the ordered EventLog entries, one object per line
    downstream-effects.json  the independent effect ledger's rows
    gateway-events.json      gateway-reported state, labelled as such
    receipts/<id>.json       each portable receipt bundle as exported
    trust-keys.json          optional issuer public keys; not an issuer trust pin
    verification-results.json   the independent verifier's three claims
    summary.html             failure_lab.report.render_run_html(...)
    report.txt               failure_lab.report.render_run_text(...)
    results.json             the raw ScenarioResult documents

Module-level imports are standard library only. :mod:`failure_lab.report` and
:mod:`failure_lab.scenarios.base` are imported inside the functions that need
them, so :func:`redact` and :func:`verify_bundle_integrity` -- the two things a
CI check calls on an already-written bundle -- stay usable without importing
the scenario suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from failure_lab import TEST_DEFINITION_VERSION
from failure_lab import __version__ as FAILURE_LAB_VERSION

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph light
    from failure_lab.scenarios.base import ScenarioResult

BUNDLE_SCHEMA_VERSION = "failure-lab-evidence/1"

MANIFEST_NAME = "manifest.json"
RECEIPTS_DIRECTORY = "receipts"

#: Every path the bundle writes, with the one line the manifest carries about
#: it. A reader should be able to tell from this alone which files are
#: observations and which are the gateway talking about itself.
FILE_DESCRIPTIONS: dict[str, str] = {
    "manifest.json": "Index of every other file with its sha256 and byte size. Not self-hashed.",
    "environment.json": "Software versions, timestamps and the run's configuration.",
    "test-definition.json": "Each scenario's definition document and its definition_hash.",
    "event-log.jsonl": "The ordered EventLog entries, one JSON object per line.",
    "downstream-effects.json": (
        "Rows from the simulated tool's own SQLite ledger, which the gateway "
        "cannot reach. One row is one execution; duplicates are rows."
    ),
    "gateway-events.json": (
        "GATEWAY-REPORTED. The gateway's own account of its attempts, debits, "
        "refunds, receipts, idempotency records and permits. Not an "
        "independent observation of anything."
    ),
    "verification-results.json": (
        "The independent verifier's three separate claims per receipt: "
        "SIGNATURE_VALID, ISSUER_TRUST_ESTABLISHED, "
        "DOWNSTREAM_EXECUTION_ESTABLISHED."
    ),
    "summary.html": "failure_lab.report.render_run_html of this run.",
    "report.txt": "failure_lab.report.render_run_text of this run.",
    "results.json": "The raw ScenarioResult documents, redacted.",
    "trust-keys.json": (
        "Issuer public keys for offline signature checks. These keys travelled "
        "with the receipts and do not establish issuer trust."
    ),
}

# --------------------------------------------------------------------------- #
# Redaction                                                                     #
# --------------------------------------------------------------------------- #

#: Field names whose value is assumed to be credential material wherever it
#: appears in the tree. Matched as a substring of the lowercased key, so
#: ``X-API-Key``, ``downstream_bearer_token`` and ``TRUST_SIGNING_PRIVATE_KEY_B64``
#: are all caught.
REDACTED_KEY_PATTERN = re.compile(
    r"api[_-]?key|apikey|token|secret|seed|password|authorization|bearer|private[_-]?key",
    re.IGNORECASE,
)

#: An ``Authorization`` (or ``X-API-Key``) header serialised into a string
#: blob, where the key-name rule cannot see it. The optional scheme word is
#: part of the match on purpose: ``Authorization: Bearer <token>`` otherwise
#: ends at the space after ``Bearer`` and publishes the token.
_AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|x-control-token)\b"
    r"\s*[:=]\s*[\"']?(?:(?:bearer|basic|digest|token)\s+)?\S+"
)
_BEARER_VALUE_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
#: ``amw_``/``sk_``-style credentials, the SDK's ``b2a_`` keys, and the
#: ``lab-admin-`` key :func:`failure_lab.gateway.boot_standalone_environment`
#: mints. Shape-based, so an unnamed credential in free text is still caught.
_CREDENTIAL_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:amw|b2a|sk|pk|rk|lab-admin)[_-][A-Za-z0-9_-]{12,}"
)

#: The one exemption to the key rule. The PRD requires the random seed in the
#: manifest, and an integer cannot carry Ed25519 key material. A *string* under
#: these keys is still redacted -- that is the shape a signing seed has.
_PUBLIC_INTEGER_SEED_KEYS = frozenset({"seed", "random_seed"})

#: Values shorter than this are not scanned for: a six-character "secret"
#: matches ordinary prose and would make the self-check useless noise.
MIN_SCANNABLE_SECRET_LENGTH = 8


class SecretLeakError(RuntimeError):
    """A known secret value survived redaction and reached the written bytes.

    The message names the file and the length of the offending value, never
    the value: an exception string ends up in CI logs.
    """

    def __init__(self, leaks: Sequence[Mapping[str, Any]]) -> None:
        self.leaks = [dict(leak) for leak in leaks]
        detail = "; ".join(
            f"{leak['path']}: value #{leak['value_index']} "
            f"(length {leak['value_length']}) appears {leak['occurrences']}x"
            for leak in self.leaks
        )
        super().__init__(f"evidence bundle leaked known secret material -- {detail}")


def _marker(matched: str) -> str:
    """A short, stable, non-reversible marker naming the rule that fired."""
    return f"<redacted:{matched.lower().replace('-', '_')}>"


def _is_public_integer_seed(key: str, value: Any) -> bool:
    return (
        key.strip().lower() in _PUBLIC_INTEGER_SEED_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
    )


def redact_text(value: str) -> str:
    """Apply the value-shape rules to one string.

    Used on free text -- observations, rendered HTML, tracebacks -- where a
    credential has no field name to be caught by.

    The bearer rule runs **first**. The header rule's ``\\S+`` stops at the
    first space, so running it first on ``Authorization: Bearer <token>``
    consumes the word ``Bearer``, leaves the token, and destroys the only
    marker the bearer rule had to find it by. The downstream bearer token is
    ``secrets.token_urlsafe(24)`` -- shapeless -- so that ordering is the
    difference between a redacted string and a published credential.
    """
    result = _BEARER_VALUE_PATTERN.sub(_marker("bearer"), value)
    result = _AUTHORIZATION_HEADER_PATTERN.sub(
        lambda m: f"{m.group(1)}: {_marker(m.group(1))}", result
    )
    return _CREDENTIAL_VALUE_PATTERN.sub(_marker("credential"), result)


def redact(document: Any) -> Any:
    """Walk a whole document tree and remove credential material.

    Two rules, applied everywhere:

    * a value whose **key** matches :data:`REDACTED_KEY_PATTERN` is replaced
      wholesale -- including a dict or list value, so nothing survives by
      being nested one level deeper;
    * a **string** value that has the shape of a bearer token, a serialised
      ``Authorization`` header or an ``amw_``/``sk_``-style credential is
      rewritten in place.

    Identifiers that are not credentials are deliberately left alone:
    ``idempotency_key``, ``operation_id``, ``receipt_id``, ``kid`` and the
    receipt's ``signature`` all survive, because redacting them would destroy
    the reader's ability to re-verify anything.

    Two shapes that a walker keyed on values alone would miss are covered
    here. A **key** can itself be the credential -- a gateway snapshot keyed
    by api key writes the key into the file name-side, where no value rule
    looks -- so key text goes through the shape rules too. And a value that is
    neither a mapping, a sequence nor a JSON scalar (``bytes``, a ``set``, any
    object) is stringified here rather than by ``json.dumps(default=str)``
    after redaction has finished, which is what let one through.
    """
    if isinstance(document, Mapping):
        redacted: dict[str, Any] = {}
        for key, value in document.items():
            name = redact_text(str(key))
            match = REDACTED_KEY_PATTERN.search(name)
            if match is not None and not _is_public_integer_seed(name, value):
                replacement: Any = _marker(match.group(0))
            else:
                replacement = redact(value)
            while name in redacted:
                # A key whose credential was rewritten can collide with
                # another. Dropping one silently would lose a row.
                name = f"{name}#{len(redacted)}"
            redacted[name] = replacement
        return redacted
    if isinstance(document, (list, tuple)):
        return [redact(item) for item in document]
    if isinstance(document, str):
        return redact_text(document)
    if document is None or isinstance(document, (bool, int, float)):
        return document
    return redact_text(str(document))


def environment_secret_values() -> list[str]:
    """Secret-shaped values sitting in this process's environment.

    The caller passes the run's admin key, bearer token and control token.
    This adds whatever else the sandbox exported under a sensitive name --
    notably ``TRUST_SIGNING_PRIVATE_KEY_B64``, which no caller holds a handle
    to but which would be catastrophic to publish. Harvesting by variable name
    keeps this cheap and keeps it from ever reading a value it does not
    already treat as secret.
    """
    values: list[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < MIN_SCANNABLE_SECRET_LENGTH:
            continue
        if REDACTED_KEY_PATTERN.search(name):
            values.append(value)
    return values


def find_leaked_secrets(
    paths: Iterable[Path], secret_values: Iterable[str]
) -> list[dict[str, Any]]:
    """Read every file and report which known secret values appear in it.

    Returns records, never the offending value. Values shorter than
    :data:`MIN_SCANNABLE_SECRET_LENGTH` are skipped and reported separately by
    the caller, because scanning for them produces only false positives. A
    file that cannot be read raises rather than being skipped: "I could not
    look" and "there is nothing there" are different answers.
    """
    candidates = [
        value
        for value in dict.fromkeys(str(v) for v in secret_values if v)
        if len(value) >= MIN_SCANNABLE_SECRET_LENGTH
    ]
    if not candidates:
        return []
    leaks: list[dict[str, Any]] = []
    for path in paths:
        # A file that cannot be read is not a file that is clean. Swallowing
        # the error here would let an unreadable file pass the backstop and
        # count towards "scanned".
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for index, value in enumerate(candidates):
            occurrences = text.count(value)
            if occurrences:
                leaks.append(
                    {
                        "path": path.name,
                        "value_index": index,
                        "value_length": len(value),
                        "occurrences": occurrences,
                    }
                )
    return leaks


def assert_no_secret_leak(directory: Path | str, secret_values: Iterable[str]) -> int:
    """Raise :class:`SecretLeakError` if any known secret is in these bytes.

    Returns the number of files scanned. Call it on a bundle that already
    exists; :func:`build_evidence_bundle` calls it before the bundle is moved
    into place.
    """
    root = Path(directory)
    files = sorted(p for p in root.rglob("*") if p.is_file())
    leaks = find_leaked_secrets(files, secret_values)
    if leaks:
        raise SecretLeakError(leaks)
    return len(files)


# --------------------------------------------------------------------------- #
# Results in, documents out                                                     #
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _document(result: Any) -> dict[str, Any]:
    """Normalise a ScenarioResult (or an already-serialised one) to a dict."""
    if isinstance(result, Mapping):
        return dict(result)
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    raise TypeError(
        f"expected a ScenarioResult or its as_dict() document, got {type(result).__name__}"
    )


def _scenario_result_from_document(document: Mapping[str, Any]) -> ScenarioResult:
    """Rebuild a :class:`ScenarioResult` from a redacted document.

    The report renderers take objects, not documents. Rebuilding from the
    *redacted* document rather than from the live result is what makes
    ``summary.html`` and ``report.txt`` redacted by construction instead of by
    a scrub pass over rendered HTML.
    """
    from failure_lab.scenarios.base import (
        ConfigurationResult,
        Counters,
        ScenarioResult as _ScenarioResult,
        Verdict,
    )

    counter_names = {field.name for field in dataclass_fields(Counters)}

    def verdict_of(value: Any) -> Verdict:
        try:
            return Verdict(str(value))
        except ValueError:
            return Verdict.ERROR

    configurations = []
    for entry in document.get("configurations") or []:
        raw_counters = entry.get("counters") or {}
        counters = Counters(
            **{k: v for k, v in raw_counters.items() if k in counter_names}
        )
        configurations.append(
            ConfigurationResult(
                configuration=str(entry.get("configuration", "")),
                label=str(entry.get("label", "")),
                verdict=verdict_of(entry.get("verdict")),
                expectation=str(entry.get("expectation", "")),
                observation=str(entry.get("observation", "")),
                counters=counters,
                attempts=list(entry.get("attempts") or []),
                downstream_effects=list(entry.get("downstream_effects") or []),
                crossings=list(entry.get("crossings") or []),
                gateway=entry.get("gateway"),
                receipts=list(entry.get("receipts") or []),
                remaining_risks=list(entry.get("remaining_risks") or []),
                extra=dict(entry.get("extra") or {}),
                error=entry.get("error"),
            )
        )
    return _ScenarioResult(
        test_id=str(document.get("test_id", "")),
        title=str(document.get("title", "")),
        claim=str(document.get("claim", "")),
        definition_version=str(document.get("definition_version", "")),
        definition_hash=str(document.get("definition_hash", "")),
        started_at=str(document.get("started_at", "")),
        finished_at=str(document.get("finished_at", "")),
        configurations=configurations,
        limitations=list(document.get("limitations") or []),
        events=list(document.get("events") or []),
        expected=dict(document.get("expected") or {}),
    )


_TRACKED_DISTRIBUTIONS = (
    "fastapi",
    "starlette",
    "httpx",
    "pydantic",
    "sqlmodel",
    "sqlalchemy",
    "cryptography",
    "mcp",
    "uvicorn",
    "aiosqlite",
)


def _software_versions() -> dict[str, str]:
    from importlib import metadata

    versions: dict[str, str] = {
        "failure_lab": FAILURE_LAB_VERSION,
        "test_definitions": TEST_DEFINITION_VERSION,
        "bundle_schema": BUNDLE_SCHEMA_VERSION,
    }
    for name in _TRACKED_DISTRIBUTIONS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


def _runtime_facts() -> dict[str, Any]:
    return {
        "python_version": sys.version.split()[0],
        "python_full_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


_FAULT_KEYS_IN_EXTRA = (
    "fault_plan",
    "fault_plans",
    "crash_boundary",
    "boundary",
    "injected",
)


def _fault_injection_points(
    documents: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Every injected failure the instruments actually recorded.

    Derived from the fault layer's own crossings (``fault`` is the mode it
    applied) plus whatever a scenario recorded in ``extra`` about a boundary it
    crashed at. ``normal`` crossings are not injection points and are omitted.
    """
    points: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for document in documents:
        test_id = str(document.get("test_id", ""))
        for entry in document.get("configurations") or []:
            configuration = str(entry.get("configuration", ""))
            for crossing in entry.get("crossings") or []:
                fault = str(crossing.get("fault") or "")
                if not fault or fault == "normal":
                    continue
                key = (test_id, configuration, "fault_layer", fault)
                point = points.setdefault(
                    key,
                    {
                        "test_id": test_id,
                        "configuration": configuration,
                        "layer": "fault_layer",
                        "point": fault,
                        "observed_crossings": 0,
                    },
                )
                point["observed_crossings"] += 1
            extra = entry.get("extra") or {}
            if isinstance(extra, Mapping):
                for name in _FAULT_KEYS_IN_EXTRA:
                    if name not in extra or extra[name] in (None, "", [], {}):
                        continue
                    key = (test_id, configuration, "scenario_extra", name)
                    points.setdefault(
                        key,
                        {
                            "test_id": test_id,
                            "configuration": configuration,
                            "layer": "scenario_extra",
                            "point": name,
                            "detail": extra[name],
                        },
                    )
    return [points[key] for key in sorted(points)]


def _definition_entries(
    documents: Sequence[Mapping[str, Any]],
    scenarios: Sequence[Any],
    definitions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Definition documents per scenario, saying where each one came from."""
    notes: list[str] = []
    supplied: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        try:
            definition = dict(scenario.definition())
            supplied[str(definition.get("test_id", ""))] = {
                "test_id": str(definition.get("test_id", "")),
                "definition": definition,
                "definition_hash": str(scenario.definition_hash()),
                "source": "scenario.definition()",
            }
        except Exception as exc:  # noqa: BLE001 - a broken definition is a finding
            notes.append(
                f"could not read a scenario definition: {type(exc).__name__}: {exc}"
            )
    for definition in definitions:
        test_id = str(definition.get("test_id", ""))
        supplied.setdefault(
            test_id,
            {
                "test_id": test_id,
                "definition": dict(definition),
                "definition_hash": str(definition.get("definition_hash", "")),
                "source": "caller-supplied definition document",
            },
        )

    entries: list[dict[str, Any]] = []
    for document in documents:
        test_id = str(document.get("test_id", ""))
        entry = supplied.get(test_id)
        if entry is None:
            entries.append(
                {
                    "test_id": test_id,
                    "definition": None,
                    "definition_hash": str(document.get("definition_hash", "")),
                    "source": "result document only",
                    "note": (
                        "the definition document was not supplied to the bundle "
                        "builder; only the hash the run recorded is present, so "
                        "this hash cannot be recomputed from this bundle"
                    ),
                }
            )
            continue
        recorded = str(document.get("definition_hash", ""))
        if (
            recorded
            and entry["definition_hash"]
            and recorded != entry["definition_hash"]
        ):
            notes.append(
                f"{test_id}: the definition hash recorded in the result "
                f"({recorded[:12]}...) differs from the definition supplied to the "
                f"bundle builder ({entry['definition_hash'][:12]}...)"
            )
            entry = dict(entry, result_definition_hash=recorded)
        entries.append(entry)
    return entries, notes


def _event_order(event: Mapping[str, Any]) -> tuple[str, float]:
    """Order by scenario then sequence, without trusting either's type.

    A malformed ``sequence`` sorts last instead of aborting the bundle: losing
    a whole run's evidence at the write step because one row carried a string
    is a worse failure than an event log in an odd order.
    """
    sequence = event.get("sequence", 0)
    if isinstance(sequence, bool) or not isinstance(sequence, (int, float)):
        try:
            sequence = float(str(sequence))
        except ValueError:
            sequence = float("inf")
    return (str(event.get("scenario", "")), float(sequence))


def _events(
    documents: Sequence[Mapping[str, Any]], event_log: Any
) -> list[dict[str, Any]]:
    if event_log is not None:
        raw = getattr(event_log, "events", event_log)
    else:
        raw = [
            event for document in documents for event in (document.get("events") or [])
        ]
    entries: list[dict[str, Any]] = []
    for event in raw:
        if isinstance(event, Mapping):
            entries.append(dict(event))
        else:
            # Kept, labelled, and not dropped: a row the log could not parse is
            # information about the run.
            entries.append({"malformed_event": event})
    entries.sort(key=_event_order)
    return entries


def _downstream_rows(documents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        test_id = str(document.get("test_id", ""))
        for entry in document.get("configurations") or []:
            configuration = str(entry.get("configuration", ""))
            for effect in entry.get("downstream_effects") or []:
                rows.append(
                    {"test_id": test_id, "configuration": configuration, **effect}
                )
    return rows


def _gateway_rows(documents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        test_id = str(document.get("test_id", ""))
        for entry in document.get("configurations") or []:
            gateway = entry.get("gateway")
            if not gateway:
                continue
            rows.append(
                {
                    "test_id": test_id,
                    "configuration": str(entry.get("configuration", "")),
                    "gateway_reported": gateway,
                }
            )
    return rows


def _receipt_documents(receipts: Any) -> list[tuple[str, Any]]:
    """Normalise the portable receipts to ``(receipt_id, bundle)`` pairs.

    An object that carries ``as_dict()`` is unwrapped here. Anything that is
    still not a mapping afterwards is passed through as it came: the writer
    records it as a receipt that did not export rather than stringifying it
    into a file that would read as an exported receipt.
    """
    if not receipts:
        return []
    if isinstance(receipts, Mapping):
        return [(str(key), value) for key, value in receipts.items()]
    pairs: list[tuple[str, Any]] = []
    for index, bundle in enumerate(receipts):
        if not isinstance(bundle, Mapping):
            as_dict = getattr(bundle, "as_dict", None)
            if callable(as_dict):
                try:
                    bundle = as_dict()
                except Exception:  # noqa: BLE001 - recorded below, never swallowed
                    pass
        receipt_id = ""
        if isinstance(bundle, Mapping):
            receipt_id = str(bundle.get("receipt_id") or "")
        pairs.append((receipt_id or f"receipt-{index:04d}", bundle))
    return pairs


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def _receipt_filename(receipt_id: str, index: int) -> str:
    cleaned = _SAFE_FILENAME.sub("_", receipt_id).strip("._") or f"receipt-{index:04d}"
    return f"{cleaned[:120]}.json"


def _scenario_manifest_entries(
    documents: Sequence[Mapping[str, Any]],
    notes: list[str],
) -> list[dict[str, Any]]:
    """The PRD's per-scenario line: expected invariant, observed outcome.

    Where the result's own ``matches_expectation`` flag disagrees with the
    per-configuration rows, the rows win and the disagreement is recorded.
    A summary flag that outranked the measurements beside it would make this
    entry worth less than the rows it summarises.
    """
    entries: list[dict[str, Any]] = []
    for document in documents:
        expected = dict(document.get("expected") or {})
        observed = {
            str(entry.get("configuration", "")): str(entry.get("verdict", ""))
            for entry in document.get("configurations") or []
        }
        divergences = [
            {
                "configuration": configuration,
                "documented_expectation": verdict,
                "observed": observed.get(configuration, "missing"),
            }
            for configuration, verdict in expected.items()
            if observed.get(configuration) != verdict
        ]
        reported_match = document.get("matches_expectation")
        matches = bool(
            reported_match if reported_match is not None else not divergences
        )
        if divergences and matches:
            notes.append(
                f"{document.get('test_id', '?')}: the result's matches_expectation flag "
                f"says true while {len(divergences)} configuration row(s) diverge from "
                "the documented expectation; the rows are recorded as authoritative"
            )
            matches = False
        entries.append(
            {
                "test_id": str(document.get("test_id", "")),
                "title": str(document.get("title", "")),
                "claim": str(document.get("claim", "")),
                "definition_version": str(document.get("definition_version", "")),
                "definition_hash": str(document.get("definition_hash", "")),
                "started_at": str(document.get("started_at", "")),
                "finished_at": str(document.get("finished_at", "")),
                "expected_invariant": {
                    "claim": str(document.get("claim", "")),
                    "per_configuration": expected,
                    "source": "the scenario's documented `expected` map",
                },
                "observed_outcome": {
                    "verdict": str(document.get("verdict", "")),
                    "per_configuration": observed,
                    "observation": {
                        str(entry.get("configuration", "")): str(
                            entry.get("observation", "")
                        )
                        for entry in document.get("configurations") or []
                    },
                    "downstream_executions": {
                        str(entry.get("configuration", "")): (
                            entry.get("counters") or {}
                        ).get("downstream_executions")
                        for entry in document.get("configurations") or []
                    },
                    "source": (
                        "downstream_executions is counted in the simulated tool's own "
                        "SQLite ledger, which the gateway cannot reach; the verdicts are "
                        "the ones the run recorded. No number here is gateway-reported -- "
                        "those are in gateway-events.json."
                    ),
                },
                "matches_documented_expectation": matches,
                "matches_expectation_reported_by_run": reported_match,
                "divergences": divergences,
                "limitations": list(document.get("limitations") or []),
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# Building                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BundleResult:
    directory: Path
    manifest: dict[str, Any]
    files: list[dict[str, Any]]
    archive_path: Path | None = None

    @property
    def paths(self) -> list[Path]:
        return [self.directory / entry["path"] for entry in self.files]

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "archive_path": str(self.archive_path) if self.archive_path else None,
            "files": self.files,
            "manifest_sha256": self.manifest_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        """The hash a reader must be given out of band to trust the index."""
        return _sha256_bytes(_json_bytes(self.manifest))


def _json_bytes(document: Any) -> bytes:
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_bundle(
    directory: Path | str,
    results: Sequence[Any],
    *,
    environment: Mapping[str, Any] | None = None,
    secret_values: Iterable[str] = (),
    include_environment_secrets: bool = True,
    scenarios: Sequence[Any] = (),
    definitions: Sequence[Mapping[str, Any]] = (),
    event_log: Any = None,
    receipts: Any = None,
    trust_keys: Mapping[str, Any] | None = None,
    verification_results: Sequence[Mapping[str, Any]] = (),
    fault_injection_points: Sequence[Mapping[str, Any]] = (),
    random_seed: int | None = None,
    test_configuration: Mapping[str, Any] | None = None,
    reproduction_command: str | None = None,
    archive: bool = False,
    overwrite: bool = True,
) -> BundleResult:
    """Write the bundle and return where it went.

    ``results`` are :class:`~failure_lab.scenarios.base.ScenarioResult` objects
    or their ``as_dict()`` documents. ``secret_values`` is the set the
    self-check looks for in the written bytes -- the run's admin key, the
    downstream bearer token and the control token, at minimum. With
    ``include_environment_secrets`` left true the sandbox's own secret-named
    environment variables are added to that set, which is how the ephemeral
    signing seed gets covered even though no caller holds it.

    The bundle is assembled in a sibling staging directory and only moved into
    place after the leak scan passes. A leak deletes the staging directory and
    raises :class:`SecretLeakError`: a bundle that leaked is never left on disk
    for someone to pick up.
    """
    target = Path(directory)
    documents = [_document(result) for result in results]
    redacted_documents = [redact(document) for document in documents]

    built_at = _now()
    bundle_id = uuid.uuid4().hex
    caller_environment = dict(environment or {})
    started = [str(d.get("started_at", "")) for d in documents if d.get("started_at")]
    finished = [
        str(d.get("finished_at", "")) for d in documents if d.get("finished_at")
    ]

    notes: list[str] = []
    definition_entries, definition_notes = _definition_entries(
        documents, scenarios, definitions
    )
    notes.extend(definition_notes)

    receipt_pairs = _receipt_documents(receipts)
    if not receipt_pairs:
        notes.append(
            "no portable receipt bundles were supplied, so receipts/ is empty. "
            "The gateway's own receipt rows are in gateway-events.json and are "
            "not a substitute: they are not signed exports and were not "
            "independently verified."
        )
    if not verification_results:
        notes.append(
            "no independent verification results were supplied, so "
            "verification-results.json records none. Nothing in this bundle "
            "establishes that a receipt's signature verifies."
        )

    report_environment = {
        "run_id": caller_environment.get("run_id", bundle_id),
        "started_at": caller_environment.get(
            "started_at", min(started) if started else built_at
        ),
        "finished_at": caller_environment.get(
            "finished_at", max(finished) if finished else built_at
        ),
        "test_definition_version": caller_environment.get(
            "test_definition_version", TEST_DEFINITION_VERSION
        ),
        "gateway_version": caller_environment.get("gateway_version", "unknown"),
        "gateway_commit": caller_environment.get("gateway_commit", "unknown"),
        "python_version": sys.version.split()[0],
        "traffic_source": caller_environment.get("traffic_source", "unknown"),
        "reproduction_command": reproduction_command
        or caller_environment.get("reproduction_command", "not recorded"),
    }
    if random_seed is not None:
        report_environment["seed"] = random_seed
    elif "seed" in caller_environment:
        report_environment["seed"] = caller_environment["seed"]
    redacted_report_environment = redact({**caller_environment, **report_environment})

    environment_document = redact(
        {
            "collected_at": built_at,
            "run": {**caller_environment, **report_environment},
            "software": _software_versions(),
            "runtime": _runtime_facts(),
            "test_configuration": dict(test_configuration or {}),
            "random_seed": random_seed
            if random_seed is not None
            else caller_environment.get("seed"),
        }
    )

    injection_points = [dict(point) for point in fault_injection_points]
    injection_points.extend(_fault_injection_points(documents))

    # -- rendered report ---------------------------------------------------
    from failure_lab.report import build_comparison, render_run_html, render_run_text

    comparisons = []
    for document in redacted_documents:
        rebuilt = _scenario_result_from_document(document)
        if str(document.get("verdict", "")) and rebuilt.verdict.value != str(
            document.get("verdict", "")
        ):
            notes.append(
                f"{document.get('test_id', '?')}: the scenario verdict recomputed from "
                f"the configuration rows ({rebuilt.verdict.value}) differs from the "
                f"verdict the result recorded ({document.get('verdict')})"
            )
        comparisons.append(build_comparison(rebuilt))

    summary_html = redact_text(
        render_run_html(comparisons, environment=redacted_report_environment)
    )
    report_text = redact_text(
        render_run_text(comparisons, environment=redacted_report_environment)
    )

    # -- staging -----------------------------------------------------------
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.partial-{bundle_id[:12]}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    written: list[Path] = []

    def write_bytes(relative: str, payload: bytes) -> None:
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        written.append(path)

    try:
        write_bytes("environment.json", _json_bytes(environment_document))
        if trust_keys is not None:
            write_bytes("trust-keys.json", _json_bytes(redact(dict(trust_keys))))
        write_bytes(
            "test-definition.json",
            _json_bytes(
                redact(
                    {
                        "definition_version": TEST_DEFINITION_VERSION,
                        "scenarios": definition_entries,
                    }
                )
            ),
        )
        event_lines = "".join(
            json.dumps(event, sort_keys=True, ensure_ascii=False, default=str) + "\n"
            for event in redact(_events(documents, event_log))
        )
        write_bytes("event-log.jsonl", event_lines.encode("utf-8"))
        write_bytes(
            "downstream-effects.json",
            _json_bytes(
                redact(
                    {
                        "source": "independent-effect-ledger",
                        "note": (
                            "Rows from the simulated downstream tool's own SQLite "
                            "file. The gateway has no handle to it. One row is one "
                            "execution; a duplicate execution is a second row."
                        ),
                        "rows": _downstream_rows(documents),
                    }
                )
            ),
        )
        write_bytes(
            "gateway-events.json",
            _json_bytes(
                redact(
                    {
                        "source": "gateway-reported",
                        "warning": (
                            "Everything below is the gateway's own account of what "
                            "it did, read from its own tables. It is not an "
                            "independent observation of a downstream effect. "
                            "Compare it against downstream-effects.json."
                        ),
                        "scenarios": _gateway_rows(documents),
                    }
                )
            ),
        )
        receipt_index: list[dict[str, str]] = []
        used_filenames: set[str] = set()
        for index, (receipt_id, bundle) in enumerate(receipt_pairs):
            filename = _receipt_filename(receipt_id, index)
            if filename in used_filenames:
                # Two receipt ids that differ only in characters the filename
                # rules strip would otherwise silently overwrite each other.
                filename = f"{filename[:-5]}-{index:04d}.json"
            used_filenames.add(filename)
            exported = isinstance(bundle, Mapping)
            if exported:
                payload: Any = redact(dict(bundle))
            else:
                # Writing str(bundle) here would put a file in receipts/ that
                # the manifest describes as an exported portable receipt. It
                # is not one, and a reader has no way to tell from the layout.
                payload = {
                    "error": "this is not a portable receipt bundle",
                    "supplied_type": type(bundle).__name__,
                    "value": redact_text(str(bundle)),
                }
                notes.append(
                    f"receipt {receipt_id!r} was supplied as "
                    f"{type(bundle).__name__}, not a portable receipt document; "
                    "its file records that rather than a receipt"
                )
            write_bytes(f"{RECEIPTS_DIRECTORY}/{filename}", _json_bytes(payload))
            receipt_index.append(
                {
                    "receipt_id": receipt_id,
                    "path": f"{RECEIPTS_DIRECTORY}/{filename}",
                    "exported_portable_bundle": exported,
                }
            )
        if not receipt_pairs:
            (staging / RECEIPTS_DIRECTORY).mkdir(exist_ok=True)
        write_bytes(
            "verification-results.json",
            _json_bytes(
                redact(
                    {
                        "verifier": "failure_lab.verifier",
                        "claims": [
                            "SIGNATURE_VALID",
                            "ISSUER_TRUST_ESTABLISHED",
                            "DOWNSTREAM_EXECUTION_ESTABLISHED",
                        ],
                        "note": (
                            "Three separate claims per receipt. A valid signature "
                            "says the holder of that key signed that statement. It "
                            "is not evidence that the business action occurred, and "
                            "a key fetched from the origin being audited does not "
                            "establish issuer trust."
                        ),
                        "results": list(verification_results),
                    }
                )
            ),
        )
        write_bytes("summary.html", summary_html.encode("utf-8"))
        write_bytes("report.txt", report_text.encode("utf-8"))
        write_bytes("results.json", _json_bytes(redacted_documents))

        file_entries = [
            {
                "path": str(path.relative_to(staging)).replace(os.sep, "/"),
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(written)
        ]
        not_exported = {
            entry["path"]
            for entry in receipt_index
            if not entry.get("exported_portable_bundle")
        }
        for entry in file_entries:
            description = FILE_DESCRIPTIONS.get(entry["path"])
            if description is None and entry["path"].startswith(
                f"{RECEIPTS_DIRECTORY}/"
            ):
                description = (
                    "NOT A RECEIPT: what was supplied in this receipt's place, recorded "
                    "as such."
                    if entry["path"] in not_exported
                    else "A portable receipt bundle exactly as the gateway exported it."
                )
            entry["description"] = description or "Bundle file."

        all_secret_values = list(secret_values)
        if include_environment_secrets:
            all_secret_values.extend(environment_secret_values())
        unique_secrets = [
            v for v in dict.fromkeys(str(v) for v in all_secret_values) if v
        ]
        scannable = [v for v in unique_secrets if len(v) >= MIN_SCANNABLE_SECRET_LENGTH]

        manifest = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "built_at": built_at,
            "run": redacted_report_environment,
            "software": _software_versions(),
            "runtime": _runtime_facts(),
            "test_configuration": redact(dict(test_configuration or {})),
            "random_seed": random_seed
            if random_seed is not None
            else caller_environment.get("seed"),
            "fault_injection_points": redact(injection_points),
            "scenarios": redact(_scenario_manifest_entries(documents, notes)),
            "receipts": receipt_index,
            "redaction": {
                "applied": True,
                "key_pattern": REDACTED_KEY_PATTERN.pattern,
                "marker_shape": "<redacted:rule>",
                "rules": [
                    "any value whose field name matches key_pattern, including a "
                    "nested object or list, is replaced wholesale",
                    "any string shaped like a bearer token, a serialised "
                    "Authorization/X-API-Key header, or an amw_/sk_/b2a_/lab-admin- "
                    "credential is rewritten in place",
                    "object keys go through the same shape rules as values, so a "
                    "map keyed by a credential does not publish the key",
                    "a value that is neither an object, an array nor a JSON scalar "
                    "(bytes, a set, any other object) is stringified and redacted "
                    "here rather than by the JSON encoder afterwards",
                    "an integer under `seed` or `random_seed` is kept: the PRD "
                    "requires the random seed and an integer carries no key "
                    "material. A string under those keys is still redacted",
                    "identifiers that are not credentials -- idempotency_key, "
                    "operation_id, receipt_id, kid, signature -- are kept, because "
                    "redacting them would make the bundle unverifiable",
                ],
                "leak_check": {
                    "known_values_supplied": len(unique_secrets),
                    "known_values_scanned": len(scannable),
                    "values_too_short_to_scan": len(unique_secrets) - len(scannable),
                    "minimum_scannable_length": MIN_SCANNABLE_SECRET_LENGTH,
                    # A bundle that leaked is deleted rather than written, so
                    # any manifest a reader holds survived the scan -- but only
                    # if a scan happened. With nothing to scan for, saying
                    # "clean" would claim a check that never ran.
                    "scanned": bool(scannable),
                    "result": (
                        f"no known secret value appears in any written byte "
                        f"({len(scannable)} value(s) searched for across "
                        f"{len(file_entries) + 1} files)"
                        if scannable
                        else (
                            "NOT SCANNED: the caller supplied no secret values and none "
                            "were harvested from the environment, so only the pattern "
                            "rules above were applied. Nothing here establishes that a "
                            "shapeless credential is absent."
                        )
                    ),
                },
            },
            "integrity": {
                "algorithm": "sha256",
                "covers": [entry["path"] for entry in file_entries],
                "not_covered": [MANIFEST_NAME],
                "note": (
                    "manifest.json cannot hash itself. Every other file in this "
                    "bundle is covered by the sha256 recorded here, so an edit to "
                    "any of them is detectable with "
                    "failure_lab.evidence.verify_bundle_integrity. An edit to "
                    "manifest.json itself is not detectable from inside the "
                    "bundle; only a hash of manifest.json obtained out of band "
                    "makes this index tamper-evident."
                ),
            },
            "reproduction": {
                "command": report_environment["reproduction_command"],
                "test_definition_version": TEST_DEFINITION_VERSION,
                "scenario_ids": [str(d.get("test_id", "")) for d in documents],
                "random_seed": random_seed
                if random_seed is not None
                else caller_environment.get("seed"),
                "python_version": sys.version.split()[0],
                "note": (
                    "The command is recorded as the run supplied it. The bundle "
                    "builder does not execute it and cannot attest that it "
                    "reproduces these bytes: scenario timing, wallet ids and "
                    "receipt ids differ per run. What is reproducible is the "
                    "definition_hash of each scenario."
                ),
            },
            "files": file_entries,
            "notes": notes,
        }
        write_bytes(MANIFEST_NAME, _json_bytes(manifest))

        leaks = find_leaked_secrets(
            sorted(p for p in staging.rglob("*") if p.is_file()), unique_secrets
        )
        if leaks:
            raise SecretLeakError(leaks)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Every exit from here also removes the staging directory. A half-moved
    # bundle that survives as a hidden sibling is a bundle someone finds later
    # with no manifest telling them it was never finished.
    try:
        if target.exists() or target.is_symlink():
            if not overwrite:
                raise FileExistsError(f"{target} already exists and overwrite=False")
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    archive_path: Path | None = None
    if archive:
        archive_path = Path(
            shutil.make_archive(
                base_name=str(target),
                format="zip",
                root_dir=str(target.parent),
                base_dir=target.name,
            )
        )

    return BundleResult(
        directory=target,
        manifest=manifest,
        files=file_entries,
        archive_path=archive_path,
    )


# --------------------------------------------------------------------------- #
# Verification                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntegrityReport:
    directory: Path
    ok: bool
    checked: int
    changed: list[str]
    missing: list[str]
    unexpected: list[str]
    problems: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "ok": self.ok,
            "checked": self.checked,
            "changed": self.changed,
            "missing": self.missing,
            "unexpected": self.unexpected,
            "problems": self.problems,
        }


def verify_bundle_integrity(directory: Path | str) -> IntegrityReport:
    """Re-hash every file in a bundle against its manifest.

    Reports three distinct failures rather than one boolean: a file whose
    bytes changed, a file the manifest lists that is gone, and a file on disk
    the manifest never listed. The third matters -- an index that silently
    ignores extra files is not an index.

    This proves the bundle matches *its own* manifest. It says nothing about
    whether the manifest was edited; see the manifest's ``integrity.note``.
    """
    root = Path(directory)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return IntegrityReport(
            directory=root,
            ok=False,
            checked=0,
            changed=[],
            missing=[MANIFEST_NAME],
            unexpected=[],
            problems=[f"{MANIFEST_NAME} is missing; the bundle has no index"],
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return IntegrityReport(
            directory=root,
            ok=False,
            checked=0,
            changed=[],
            missing=[],
            unexpected=[],
            problems=[f"{MANIFEST_NAME} is not valid JSON: {exc}"],
        )

    entries = manifest.get("files") or []
    changed: list[str] = []
    missing: list[str] = []
    problems: list[str] = []
    checked = 0
    listed: set[str] = set()

    for entry in entries:
        relative = str(entry.get("path", ""))
        listed.add(relative)
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        checked += 1
        actual = _sha256_file(path)
        expected = str(entry.get("sha256", ""))
        if actual != expected:
            changed.append(relative)
            problems.append(
                f"{relative}: sha256 is {actual[:16]}..., manifest says {expected[:16]}..."
            )
        size = path.stat().st_size
        if entry.get("bytes") is not None and int(entry["bytes"]) != size:
            problems.append(
                f"{relative}: {size} bytes on disk, manifest says {entry['bytes']}"
            )

    on_disk = {
        str(path.relative_to(root)).replace(os.sep, "/")
        for path in root.rglob("*")
        if path.is_file()
    }
    unexpected = sorted(on_disk - listed - {MANIFEST_NAME})
    problems.extend(
        f"{name}: present on disk but not listed in the manifest" for name in unexpected
    )
    problems.extend(
        f"{name}: listed in the manifest but missing from the bundle"
        for name in missing
    )

    return IntegrityReport(
        directory=root,
        ok=not (changed or missing or unexpected or problems),
        checked=checked,
        changed=changed,
        missing=missing,
        unexpected=unexpected,
        problems=problems,
    )


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleResult",
    "FILE_DESCRIPTIONS",
    "IntegrityReport",
    "MIN_SCANNABLE_SECRET_LENGTH",
    "REDACTED_KEY_PATTERN",
    "SecretLeakError",
    "assert_no_secret_leak",
    "build_evidence_bundle",
    "environment_secret_values",
    "find_leaked_secrets",
    "redact",
    "redact_text",
    "verify_bundle_integrity",
]
