"""Machine-readable traceability from a public claim back to a run.

A sentence on a website is an assertion until something can be pointed at. The
manifest this module emits is that something: one record per scenario, naming
the claim, the test that exercised it, the exact definition the test ran, the
environment, the timestamp, the **observed** verdict, and what the test still
does not cover.

Three rules, and the third is the whole point:

* ``status`` is the observed verdict. It is copied from the result. Nothing in
  this module can turn a FAIL into a PASS, and there is no parameter for it.
* A scenario whose observed verdict differs from its **documented**
  expectation carries ``matches_documented_expectation: false`` and the
  divergence spelled out in ``limitations``. That is true whether the
  surprise is bad (a documented guarantee did not hold) or good (something
  documented as failing now passes) -- an unexpected improvement means the
  documentation and the expectation must be updated together, deliberately,
  not absorbed silently by a manifest.
* :func:`assert_claim_is_supported` refuses a claim unless a record both
  observes PASS *and* matches its documented expectation. A docs or CI check
  calls it and fails the build; that is what stops a claim drifting away from
  the test that was supposed to justify it.

The PRD's record shape is a subset of what is emitted here, and stays one:
``claim``, ``test_id``, ``version``, ``status``, ``environment``,
``tested_at`` and ``limitations`` are always present with those names.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from failure_lab import TEST_DEFINITION_VERSION
from failure_lab import __version__ as FAILURE_LAB_VERSION

CLAIMS_SCHEMA_VERSION = "failure-lab-claims/1"

#: The only status that supports a public claim.
SUPPORTING_STATUS = "PASS"


class UnsupportedClaimError(AssertionError):
    """A public claim has no PASSing, expectation-matching test behind it.

    Subclasses :class:`AssertionError` so a docs check or a CI gate that
    already treats assertion failures as build failures needs no special case.
    """

    def __init__(
        self, claim_text: str, reason: str, *, candidates: Sequence[str] = ()
    ) -> None:
        self.claim_text = claim_text
        self.reason = reason
        self.candidates = list(candidates)
        message = f"unsupported claim {claim_text!r}: {reason}"
        if self.candidates:
            shown = "; ".join(self.candidates[:5])
            message = f"{message}. Claims that are tested: {shown}"
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_claim(text: str) -> str:
    """Fold a claim to the form used for matching.

    Case, surrounding whitespace, internal run-length whitespace and a
    trailing period are not part of a claim's identity. Anything else is: two
    claims that differ in a word are two claims, and the stricter reading is
    the safe one for a check whose job is to refuse.
    """
    collapsed = " ".join(str(text).split())
    return collapsed.rstrip(".").casefold()


def _observed_status(status: str, observed: Mapping[str, str]) -> tuple[str, list[str]]:
    """A PASS summary cannot outrank the negative configuration rows it reports."""
    worst_row = next((v for v in ("ERROR", "FAIL") if v in observed.values()), "")
    if status == SUPPORTING_STATUS and worst_row:
        return worst_row, [
            f"The result document reported {SUPPORTING_STATUS} while a "
            f"configuration row observed {worst_row}; the row is used as the "
            "status, because a scenario verdict is the worst of its rows."
        ]
    return status, []


@dataclass
class ClaimRecord:
    """One claim, one test, one observed result."""

    claim: str
    test_id: str
    version: str
    status: str
    environment: str
    tested_at: str
    limitations: list[str] = field(default_factory=list)
    definition_hash: str = ""
    definition_version: str = TEST_DEFINITION_VERSION
    #: The observed verdict per configuration, so a reader can see *where* the
    #: claim holds -- "the gateway prevented it" and "the naive baseline
    #: already prevented it" are different facts.
    configurations: dict[str, str] = field(default_factory=dict)
    matches_documented_expectation: bool = True
    #: The documented expectation, kept beside the observation so a divergence
    #: can be read without fetching the scenario source.
    documented_expectation: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] | None = None
    title: str = ""

    @property
    def supports_a_public_claim(self) -> bool:
        return self.status == SUPPORTING_STATUS and self.matches_documented_expectation

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "test_id": self.test_id,
            "version": self.version,
            "status": self.status,
            "environment": self.environment,
            "tested_at": self.tested_at,
            "limitations": list(self.limitations),
            "title": self.title,
            "definition_hash": self.definition_hash,
            "definition_version": self.definition_version,
            "configurations": dict(self.configurations),
            "documented_expectation": dict(self.documented_expectation),
            "matches_documented_expectation": self.matches_documented_expectation,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ClaimRecord:
        """Read a record back, without letting absence read as agreement.

        ``as_dict`` always writes ``matches_documented_expectation``, so a
        round trip is unaffected. A record that arrives without the field --
        an older manifest, a hand-written one, a truncated one -- is *not*
        assumed to match: a gate that grants support for data it does not have
        grants it exactly when the data is missing. And where the field says
        true while the rows beside it diverge, the rows win, as they do when
        the record is built.
        """
        expected = {
            str(k): str(v)
            for k, v in (document.get("documented_expectation") or {}).items()
        }
        observed = {
            str(k): str(v) for k, v in (document.get("configurations") or {}).items()
        }
        rows_diverge = bool(_divergence_limitations(expected, observed))
        reported = document.get("matches_documented_expectation")
        if reported is None:
            matches = bool(expected) and not rows_diverge
        else:
            matches = bool(reported) and not rows_diverge
        status, status_lines = _observed_status(
            str(document.get("status", "")), observed
        )
        return cls(
            claim=str(document.get("claim", "")),
            test_id=str(document.get("test_id", "")),
            version=str(document.get("version", "")),
            status=status,
            environment=str(document.get("environment", "")),
            tested_at=str(document.get("tested_at", "")),
            limitations=list(document.get("limitations") or []) + status_lines,
            definition_hash=str(document.get("definition_hash", "")),
            definition_version=str(
                document.get("definition_version", TEST_DEFINITION_VERSION)
            ),
            configurations=observed,
            matches_documented_expectation=matches,
            documented_expectation=expected,
            evidence=document.get("evidence"),
            title=str(document.get("title", "")),
        )


@dataclass
class ClaimsManifest:
    records: list[ClaimRecord]
    generated_at: str
    environment: str
    version: str
    definition_version: str = TEST_DEFINITION_VERSION
    schema_version: str = CLAIMS_SCHEMA_VERSION

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def by_test_id(self, test_id: str) -> ClaimRecord | None:
        wanted = str(test_id).upper()
        for record in self.records:
            if record.test_id.upper() == wanted:
                return record
        return None

    def supported(self) -> list[ClaimRecord]:
        """Records that may back a public claim."""
        return [record for record in self.records if record.supports_a_public_claim]

    def divergences(self) -> list[ClaimRecord]:
        """Records whose observation disagrees with the documentation."""
        return [
            record
            for record in self.records
            if not record.matches_documented_expectation
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "environment": self.environment,
            "version": self.version,
            "definition_version": self.definition_version,
            "claims": [record.as_dict() for record in self.records],
        }

    def to_json(self) -> str:
        return (
            json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        )

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> ClaimsManifest:
        return cls(
            records=[
                ClaimRecord.from_dict(entry) for entry in document.get("claims") or []
            ],
            generated_at=str(document.get("generated_at", "")),
            environment=str(document.get("environment", "")),
            version=str(document.get("version", "")),
            definition_version=str(
                document.get("definition_version", TEST_DEFINITION_VERSION)
            ),
            schema_version=str(document.get("schema_version", CLAIMS_SCHEMA_VERSION)),
        )


def _result_document(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    as_dict = getattr(result, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    raise TypeError(
        f"expected a ScenarioResult or its as_dict() document, got {type(result).__name__}"
    )


def _divergence_limitations(
    expected: Mapping[str, str], observed: Mapping[str, str]
) -> list[str]:
    """One plain sentence per configuration where the run surprised the docs."""
    lines: list[str] = []
    for configuration, documented in expected.items():
        seen = observed.get(configuration, "not run")
        if seen == documented:
            continue
        if documented == SUPPORTING_STATUS and seen != SUPPORTING_STATUS:
            direction = "the documented guarantee did not hold"
        elif seen == SUPPORTING_STATUS:
            direction = (
                "this passed where the documentation says it does not; the "
                "documentation and the expectation must be updated together "
                "before the claim is made"
            )
        else:
            direction = "the run disagrees with the documented expectation"
        lines.append(
            f"DIVERGENCE in {configuration}: documented {documented}, observed "
            f"{seen} -- {direction}."
        )
    return lines


def build_claims_manifest(
    results: Sequence[Any],
    *,
    environment: str | Mapping[str, Any] = "unknown",
    version: str | None = None,
    tested_at: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    evidence_by_test_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> ClaimsManifest:
    """Emit one record per scenario result.

    ``environment`` may be a label or the run's environment mapping, in which
    case a label is composed from what the run recorded. ``version`` defaults
    to the gateway version the run reported, falling back to the lab's own.
    ``evidence`` is the bundle this run produced -- its path and the sha256 of
    its manifest -- attached to every record; ``evidence_by_test_id`` overrides
    it per scenario when bundles are written per test.

    There is deliberately no argument that sets ``status``.
    """
    environment_map: Mapping[str, Any] = (
        environment if isinstance(environment, Mapping) else {}
    )
    if isinstance(environment, str):
        environment_label = environment
    else:
        environment_label = str(
            environment_map.get("environment")
            or environment_map.get("label")
            or "; ".join(
                f"{name}={environment_map[name]}"
                for name in ("run_id", "gateway_version", "python_version", "platform")
                if environment_map.get(name)
            )
            or "unknown"
        )
    resolved_version = str(
        version
        or environment_map.get("gateway_version")
        or environment_map.get("version")
        or FAILURE_LAB_VERSION
    )
    generated_at = tested_at or _now()

    records: list[ClaimRecord] = []
    for result in results:
        document = _result_document(result)
        test_id = str(document.get("test_id", ""))
        expected = {str(k): str(v) for k, v in (document.get("expected") or {}).items()}
        observed = {
            str(entry.get("configuration", "")): str(entry.get("verdict", ""))
            for entry in document.get("configurations") or []
        }
        divergence_lines = _divergence_limitations(expected, observed)
        matches = bool(document.get("matches_expectation", not divergence_lines))
        if divergence_lines:
            # The document's own flag and the per-configuration rows must agree;
            # if they do not, the rows win. A manifest that trusted a summary
            # flag over the measurements would be exactly the failure mode this
            # module exists to prevent.
            matches = False

        # The observed verdict, copied -- but never a summary that outranks the
        # rows it summarises. ScenarioResult.verdict is the worst row, so a
        # document reading PASS over a FAIL row did not come from a run.
        status, status_lines = _observed_status(
            str(document.get("verdict", "")), observed
        )

        limitations = list(document.get("limitations") or [])
        limitations.extend(status_lines)
        limitations.extend(divergence_lines)
        risks = [
            risk
            for entry in document.get("configurations") or []
            for risk in entry.get("remaining_risks") or []
        ]
        for risk in risks:
            line = f"Remaining risk: {risk}"
            if line not in limitations:
                limitations.append(line)

        record_evidence: dict[str, Any] | None = None
        if evidence_by_test_id and test_id in evidence_by_test_id:
            record_evidence = dict(evidence_by_test_id[test_id])
        elif evidence:
            record_evidence = dict(evidence)

        records.append(
            ClaimRecord(
                claim=str(document.get("claim", "")),
                test_id=test_id,
                version=resolved_version,
                # The observed verdict. Never the desired one.
                status=status,
                environment=environment_label,
                tested_at=str(document.get("finished_at") or generated_at),
                limitations=limitations,
                definition_hash=str(document.get("definition_hash", "")),
                definition_version=str(
                    document.get("definition_version", TEST_DEFINITION_VERSION)
                ),
                configurations=observed,
                matches_documented_expectation=matches,
                documented_expectation=expected,
                evidence=record_evidence,
                title=str(document.get("title", "")),
            )
        )

    return ClaimsManifest(
        records=records,
        generated_at=generated_at,
        environment=environment_label,
        version=resolved_version,
    )


def _as_manifest(manifest: ClaimsManifest | Mapping[str, Any]) -> ClaimsManifest:
    return (
        manifest
        if isinstance(manifest, ClaimsManifest)
        else ClaimsManifest.from_dict(manifest)
    )


def find_claim(
    manifest: ClaimsManifest | Mapping[str, Any], claim_text: str
) -> ClaimRecord | None:
    """The record whose claim matches, ignoring case, spacing and a full stop."""
    wanted = normalize_claim(claim_text)
    for record in _as_manifest(manifest).records:
        if normalize_claim(record.claim) == wanted:
            return record
    return None


def supported_claims(manifest: ClaimsManifest | Mapping[str, Any]) -> list[str]:
    """Exactly the claim sentences a public surface is allowed to make."""
    return [record.claim for record in _as_manifest(manifest).supported()]


def assert_claim_is_supported(
    manifest: ClaimsManifest | Mapping[str, Any],
    claim_text: str,
    *,
    allow_divergence: bool = False,
) -> ClaimRecord:
    """Refuse a public claim that no PASSing test stands behind.

    Raises :class:`UnsupportedClaimError` when the claim is not in the
    manifest, when its test did not observe PASS, or when its observation
    diverged from the documented expectation. The last case is refused even
    though the status may read PASS: a scenario that surprised its own
    documentation has not yet been re-documented, and shipping the claim in
    that window is how a manifest stops meaning anything.

    ``allow_divergence=True`` exists for the deliberate case where a reviewer
    has read the divergence and still wants the claim. It has to be typed.
    """
    resolved = _as_manifest(manifest)
    record = find_claim(resolved, claim_text)
    if record is None:
        raise UnsupportedClaimError(
            claim_text,
            "no test in the claims manifest makes this claim",
            candidates=supported_claims(resolved),
        )
    if record.status != SUPPORTING_STATUS:
        raise UnsupportedClaimError(
            claim_text,
            f"{record.test_id} observed {record.status or 'no verdict'}, not "
            f"{SUPPORTING_STATUS}",
        )
    if not record.matches_documented_expectation and not allow_divergence:
        if not record.documented_expectation:
            raise UnsupportedClaimError(
                claim_text,
                f"{record.test_id} observed PASS but the record carries no "
                "documented expectation to have matched, so nothing here says the "
                "run behaved as documented. Rebuild the manifest from the run",
            )
        divergences = [
            line for line in record.limitations if line.startswith("DIVERGENCE")
        ]
        detail = (
            " ".join(divergences)
            or "the observed verdicts differ from the documented ones"
        )
        raise UnsupportedClaimError(
            claim_text,
            f"{record.test_id} observed PASS but diverged from its documented "
            f"expectation, so the claim is not yet re-documented -- {detail}",
        )
    return record


def render_markdown(manifest: ClaimsManifest | Mapping[str, Any]) -> str:
    """A traceability table a human reviewer can read in one pass.

    Divergences are pulled out above the table rather than buried in a cell,
    because a divergence is the only row that needs a decision.
    """
    resolved = _as_manifest(manifest)
    lines: list[str] = []
    lines.append("# Claims traceability")
    lines.append("")
    lines.append(f"Generated: {resolved.generated_at}")
    lines.append(f"Version: {resolved.version}")
    lines.append(f"Test definitions: {resolved.definition_version}")
    lines.append(f"Environment: {resolved.environment}")
    lines.append("")
    lines.append(
        "`status` is the verdict the run observed, not the verdict the claim "
        "wants. A row with `Matches docs` = no is not usable as evidence for a "
        "public claim until the documentation and the scenario's expectation "
        "are updated together."
    )
    lines.append("")

    divergences = resolved.divergences()
    if divergences:
        lines.append("## Divergences from documented expectations")
        lines.append("")
        for record in divergences:
            lines.append(
                f"- **{record.test_id}** ({record.status}) — {record.title or record.claim}"
            )
            for limitation in record.limitations:
                if limitation.startswith("DIVERGENCE"):
                    lines.append(f"  - {limitation}")
        lines.append("")

    lines.append("## Claims")
    lines.append("")
    lines.append(
        "| Test | Claim | Status | Matches docs | Version | Tested at | Definition | Evidence |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for record in resolved.records:
        evidence = "—"
        if record.evidence:
            path = (
                record.evidence.get("path")
                or record.evidence.get("directory")
                or "bundle"
            )
            digest = str(record.evidence.get("manifest_sha256") or "")
            evidence = f"`{path}`" + (f" ({digest[:12]}…)" if digest else "")
        lines.append(
            "| {test} | {claim} | {status} | {matches} | {version} | {tested} | `{digest}` | {evidence} |".format(
                test=record.test_id,
                claim=_cell(record.claim),
                status=record.status,
                matches="yes" if record.matches_documented_expectation else "**no**",
                version=record.version,
                tested=record.tested_at,
                digest=(record.definition_hash or "unknown")[:12],
                evidence=evidence,
            )
        )
    lines.append("")

    lines.append("## Limitations")
    lines.append("")
    for record in resolved.records:
        if not record.limitations:
            continue
        lines.append(f"### {record.test_id} — {record.title or record.claim}")
        lines.append("")
        for limitation in record.limitations:
            lines.append(f"- {_cell(limitation)}")
        lines.append("")
    if not any(record.limitations for record in resolved.records):
        lines.append(
            "No scenario recorded a limitation. That is itself worth checking."
        )
        lines.append("")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def write_claims_manifest(
    path: Path | str, manifest: ClaimsManifest | Mapping[str, Any]
) -> Path:
    """Write the manifest as JSON, and the Markdown table beside it.

    Both files go through :func:`failure_lab.evidence.redact` on the way out.
    A claims manifest is a published artifact, and the free text it carries --
    limitations, remaining risks, an environment label the caller composed --
    comes from the same run as the evidence bundle, which redacts everything
    it writes. Nothing in the record shape matches the key rule, so redaction
    here can only rewrite a value that is shaped like a credential.
    """
    from failure_lab.evidence import redact, redact_text

    resolved = _as_manifest(manifest)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = redact(resolved.as_dict())
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    target.with_suffix(".md").write_text(
        redact_text(render_markdown(resolved)), encoding="utf-8"
    )
    return target


def load_claims_manifest(path: Path | str) -> ClaimsManifest:
    """Read a manifest a previous run wrote, for a docs or CI check."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return ClaimsManifest.from_dict(document)


def assert_claims_are_supported(
    manifest: ClaimsManifest | Mapping[str, Any], claim_texts: Iterable[str]
) -> list[ClaimRecord]:
    """Check a whole page's worth of claims, reporting every failure at once."""
    resolved = _as_manifest(manifest)
    records: list[ClaimRecord] = []
    failures: list[str] = []
    for claim_text in claim_texts:
        try:
            records.append(assert_claim_is_supported(resolved, claim_text))
        except UnsupportedClaimError as exc:
            failures.append(str(exc))
    if failures:
        raise UnsupportedClaimError(
            f"{len(failures)} claim(s)",
            "the following claims are not supported: " + " || ".join(failures),
        )
    return records


__all__ = [
    "CLAIMS_SCHEMA_VERSION",
    "ClaimRecord",
    "ClaimsManifest",
    "SUPPORTING_STATUS",
    "UnsupportedClaimError",
    "assert_claim_is_supported",
    "assert_claims_are_supported",
    "build_claims_manifest",
    "find_claim",
    "load_claims_manifest",
    "normalize_claim",
    "render_markdown",
    "supported_claims",
    "write_claims_manifest",
]
