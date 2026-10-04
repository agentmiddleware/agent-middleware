"""``python -m failure_lab`` -- the whole lab behind one command.

Six subcommands, each of which either measures something or re-checks
something someone else measured:

``list``      what the suite contains, with the verdict each scenario documents
``run``       run a selection, write the bundle, print what happened
``verify``    re-hash an existing bundle and re-run the receipt verification
``claims``    show which public claims a run's measurements support
``explore``   property-based exploration of the lifecycle (``failure_lab.stateful``)
``serve``     the diagnostic server (``failure_lab.diagnostic``)

Three rules are enforced here rather than left to whoever is calling.

**The default traffic source is never ``human_customer``.** ``--source``
defaults to ``internal_test`` and ``human_customer`` has to be typed by a
person, because a CLI that can label its own traffic as a customer is a CLI
that will eventually inflate a conversion rate from a cron job. The same rule
is enforced a second time in :mod:`failure_lab.telemetry`, where
``detect_traffic_source`` cannot return it either; defence in depth on a
number that gets quoted to investors is cheap.

**Stdout carries the result, stderr carries the narration.** The gateway, the
MCP SDK and httpx all write progress lines, and a few arrive on stdout rather
than through ``logging``. ``--json`` would be unusable if any of them landed
in the document, so the run executes under ``redirect_stdout(sys.stderr)`` and
the summary is printed afterwards.

**Nothing prints that has not been through redaction.** Everything this module
writes to a terminal comes from :meth:`~failure_lab.runner.LabRun.redacted_document`
or from a bundle that was already redacted before it hit the disk. A terminal
is a place secrets get pasted into issue trackers from.

:mod:`failure_lab.diagnostic` is imported lazily inside ``serve`` and
:mod:`failure_lab.stateful` inside ``explore``, so that the rest of the CLI
keeps working while those modules are still being written -- and so that
``list`` does not pay for importing a scenario driver it never uses.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from failure_lab import TEST_DEFINITION_VERSION
from failure_lab import __version__ as LAB_VERSION
from failure_lab.evidence import SecretLeakError
from failure_lab.runner import (
    BUNDLE_DIRECTORY_NAME,
    CLAIMS_FILENAME,
    DEFAULT_TRAFFIC_SOURCE,
    LabRun,
    run_lab,
    summary_lines,
)
from failure_lab.telemetry import TrafficSource

PROGRAM = "python -m failure_lab"

#: Exit code for a CLI-level problem -- a name that does not resolve, a
#: selection that matches nothing, a bundle that is not there, a subcommand
#: whose module has not landed. It is 2 because that is what ``argparse``
#: already exits with on a bad command line, and a second code for the same
#: class of mistake helps nobody. It therefore **collides** with
#: :data:`~failure_lab.runner.EXIT_SCENARIO_ERROR`: a caller that needs to
#: tell "a scenario raised" from "the lab could not be asked" has to read
#: stderr, which carries a message in the second case and a verdict table in
#: the first.
EXIT_USAGE = 2

#: A written artifact contained a credential this run minted. Its own code
#: because it is not a finding about the gateway and not a usage error, and
#: because a CI job must be able to fail differently on it. The leaking files
#: are deleted before this is returned; see
#: :func:`failure_lab.runner._assert_siblings_are_clean`.
EXIT_EVIDENCE_LEAK = 3

#: Addresses ``serve`` will bind. Duplicated from
#: :data:`failure_lab.diagnostic.server.LOOPBACK_HOSTS` rather than imported,
#: because this check has to hold on the path where that module is absent or
#: exposes only an application object, and importing it here would undo the
#: lazy import that keeps the rest of the CLI working without it.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _print(lines: list[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")


def _dump(document: Any) -> None:
    sys.stdout.write(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n")


# --------------------------------------------------------------------------- #
# Argument parsing                                                              #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Agent Gateway Failure Lab: drive the gateway under injected "
            "failures and measure what actually happened."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"failure_lab {LAB_VERSION} (test definitions {TEST_DEFINITION_VERSION})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="List the scenarios and what each documents.")
    listing.add_argument("--tier", choices=("fast", "slow"), default=None)
    listing.add_argument("--json", action="store_true", dest="as_json")

    run = sub.add_parser("run", help="Run scenarios and write an evidence bundle.")
    run.add_argument("--tier", choices=("fast", "slow"), default=None)
    run.add_argument(
        "--test",
        action="append",
        default=[],
        dest="tests",
        metavar="ID",
        help="Scenario id, e.g. --test T03 (repeatable).",
    )
    run.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="Run directory to keep. Without it a temporary one is used and removed.",
    )
    run.add_argument(
        "--source",
        choices=tuple(source.value for source in TrafficSource),
        default=DEFAULT_TRAFFIC_SOURCE.value,
        help=(
            "Traffic source recorded on every telemetry event "
            f"(default: {DEFAULT_TRAFFIC_SOURCE.value}). 'human_customer' is "
            "never inferred and has to be typed."
        ),
    )
    run.add_argument("--seed", type=int, default=None, help="Seed for the process RNG.")
    run.add_argument(
        "--keep", action="store_true", help="Keep a temporary run directory."
    )
    run.add_argument(
        "--json", action="store_true", dest="as_json", help="Print the run document."
    )
    run.add_argument(
        "--archive", action="store_true", help="Also write the bundle as a .zip."
    )
    run.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Scenario option passed to every selected scenario (repeatable).",
    )
    run.add_argument(
        "--no-telemetry",
        action="store_true",
        help="Do not write the local telemetry log for this run.",
    )
    run.add_argument(
        "--verbose", action="store_true", help="Leave application logging switched on."
    )

    verify = sub.add_parser(
        "verify",
        help="Re-hash a bundle and re-run the independent receipt verification.",
    )
    verify.add_argument("--bundle", type=Path, required=True, metavar="DIR")
    verify.add_argument(
        "--keys",
        type=Path,
        default=None,
        metavar="PATH",
        help="A trust-keys document to verify receipts against, if the bundle carries none.",
    )
    verify.add_argument("--json", action="store_true", dest="as_json")

    claims = sub.add_parser("claims", help="Show which public claims a run supports.")
    claims.add_argument("--bundle", type=Path, default=None, metavar="DIR")
    claims.add_argument("--markdown", action="store_true")
    claims.add_argument("--json", action="store_true", dest="as_json")

    explore = sub.add_parser(
        "explore", help="Property-based exploration of the governed-call lifecycle."
    )
    explore.add_argument("--seed", type=int, required=True)
    explore.add_argument("--sequences", type=int, default=None)
    explore.add_argument("--max-length", type=int, default=None)
    explore.add_argument("--shrink-budget", type=int, default=None)
    explore.add_argument("--full", action="store_true")
    explore.add_argument("--keep", action="store_true")
    explore.add_argument("--verbose", action="store_true")

    serve = sub.add_parser("serve", help="Run the diagnostic server.")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback addresses only; anything else is refused.",
    )

    return parser


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _options(pairs: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for pair in pairs:
        name, _, value = pair.partition("=")
        options[name.strip()] = _coerce(value)
    return options


# --------------------------------------------------------------------------- #
# list                                                                          #
# --------------------------------------------------------------------------- #


def command_list(args: argparse.Namespace) -> int:
    from failure_lab.scenarios import SCENARIO_CLASSES

    chosen = [
        scenario
        for scenario in SCENARIO_CLASSES
        if args.tier is None or scenario.tier == args.tier
    ]
    if args.as_json:
        _dump(
            {
                "test_definition_version": TEST_DEFINITION_VERSION,
                "lab_version": LAB_VERSION,
                "scenarios": [
                    {
                        "test_id": scenario.test_id,
                        "title": scenario.title,
                        "claim": scenario.claim,
                        "tier": scenario.tier,
                        "configurations": [c.value for c in scenario.configurations],
                        "expected": dict(scenario.expected),
                        "limitations": list(scenario.limitations),
                    }
                    for scenario in chosen
                ],
            }
        )
        return 0

    lines = [
        f"Agent Gateway Failure Lab — test definitions {TEST_DEFINITION_VERSION}",
        "",
        f"{'TEST':<6}{'TIER':<7}TITLE",
    ]
    for scenario in chosen:
        lines.append(f"{scenario.test_id:<6}{scenario.tier:<7}{scenario.title}")
        lines.append(f"      claim:    {scenario.claim}")
        documented = ", ".join(
            f"{configuration}={verdict}"
            for configuration, verdict in sorted(scenario.expected.items())
        )
        lines.append(f"      expected: {documented or 'none documented'}")
    lines.append("")
    lines.append(
        "'expected' is the verdict the product currently documents, not a "
        "target. A run that disagrees with it is a finding in either "
        "direction."
    )
    _print(lines)
    return 0


# --------------------------------------------------------------------------- #
# run                                                                           #
# --------------------------------------------------------------------------- #


def command_run(args: argparse.Namespace) -> int:
    # Lazy, like every other scenario import here: `list` must not pay for a
    # driver it never uses, and nothing under app/ may be imported before
    # run_lab has pinned the sandbox posture into os.environ.
    from failure_lab.scenarios import select_scenarios

    if not args.verbose:
        # The gateway, the MCP SDK and httpx all narrate at INFO. The result is
        # the output; the narration would bury it.
        logging.disable(logging.CRITICAL - 1)

    # Resolved before the gateway boots. An empty selection is a mistyped
    # command line, not a result: run_lab would happily write a well-formed
    # bundle recording an empty suite and exit zero, and a CI job that typed
    # `--tier fase` would go green having measured nothing. exit_status_for
    # is right to say nothing about results it was not given; refusing the
    # question is this layer's job.
    try:
        selected = select_scenarios(
            args.tests or None, tier=args.tier, **_options(args.option)
        )
    except KeyError as exc:
        sys.stderr.write(f"{exc.args[0] if exc.args else exc}\n")
        return EXIT_USAGE
    if not selected:
        filters = ", ".join(
            part
            for part in (
                f"--tier {args.tier}" if args.tier else "",
                " ".join(f"--test {test}" for test in args.tests),
            )
            if part
        )
        sys.stderr.write(
            f"no scenario matches {filters or 'this selection'}; nothing would "
            "run and the result would establish nothing about the gateway. "
            f"Run '{PROGRAM} list' to see which tier each scenario is in.\n"
        )
        return EXIT_USAGE

    source = TrafficSource(args.source)
    if source == TrafficSource.HUMAN_CUSTOMER:
        sys.stderr.write(
            "recording this run as human_customer traffic because --source said "
            "so. Nothing in the environment can establish that; it is the "
            "caller's assertion.\n"
        )

    run: LabRun
    try:
        # Startup lines from the application and the MCP SDK reach stdout
        # directly. Stdout is the machine-readable surface, so the run happens
        # with stdout pointed at stderr and the summary is printed after.
        with contextlib.redirect_stdout(sys.stderr):
            run = asyncio.run(
                run_lab(
                    test_ids=args.tests or None,
                    tier=args.tier,
                    run_dir=args.output,
                    # A caller-named directory is never removed by run_lab, so
                    # --output already implies retention; --keep is what
                    # retains a temporary one.
                    keep=bool(args.keep),
                    seed=args.seed,
                    traffic_source=source,
                    archive=bool(args.archive),
                    scenario_options=_options(args.option),
                    telemetry=not args.no_telemetry,
                )
            )
    except KeyError as exc:
        # get_scenario raises KeyError with the known ids in its message.
        sys.stderr.write(f"{exc.args[0] if exc.args else exc}\n")
        return EXIT_USAGE
    except SecretLeakError as exc:
        # Caught before RuntimeError, which it subclasses. Without this branch
        # a run whose evidence leaked a credential exited with the same code
        # and the same one-line shape as "choose another --output directory",
        # which is the one outcome that must not read like a setup problem.
        # The message names the file and the length of the value, never the
        # value.
        sys.stderr.write(
            "EVIDENCE LEAK: this run wrote a credential it minted into an "
            "artifact.\n"
            f"{exc}\n"
            "The offending files have been deleted. Nothing about the "
            "gateway was established by this run; fix the leak and run it "
            "again.\n"
        )
        return EXIT_EVIDENCE_LEAK
    except RuntimeError as exc:
        # The run never started: a sandbox directory that is not ours to
        # delete, or an application already imported under another posture.
        # Both are setup problems with a readable message; a traceback here
        # would bury it.
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE

    if args.as_json:
        _dump(run.redacted_document())
    else:
        _print(summary_lines(run))
    return run.exit_status


# --------------------------------------------------------------------------- #
# verify                                                                        #
# --------------------------------------------------------------------------- #


def resolve_bundle(path: Path) -> Path | None:
    """Accept either the bundle directory or the run directory that holds it.

    ``run --output DIR`` produces ``DIR/evidence``, and people pass whichever
    of the two they have in their shell history. Guessing between them is
    safe: the answer is whichever one has a ``manifest.json``.
    """
    if (path / "manifest.json").is_file():
        return path
    nested = path / BUNDLE_DIRECTORY_NAME
    if (nested / "manifest.json").is_file():
        return nested
    return None


def _load_keys(
    bundle: Path, override: Path | None
) -> tuple[dict[str, bytes], str, str]:
    """Find a trust-keys document for re-verification, and say what happened.

    Returns ``(keys, path, problem)``. Exactly one of ``path`` and ``problem``
    is set when a candidate file existed, and both are empty when none did.
    The three cases have to stay apart: a document that is there and unusable
    is a failure to look, and reporting it as "no trust-keys document was
    found" tells a reader the bundle carries no key material when it carries
    key material nobody could parse.
    """
    from failure_lab.verifier import parse_key_document

    candidates = [override] if override else []
    candidates += [
        bundle / "trust-keys.json",
        bundle / "keys.json",
        bundle.parent / "trust-keys.json",
    ]
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except ValueError as exc:
            return {}, "", f"{candidate} is not valid JSON: {exc}"
        try:
            return parse_key_document(document), str(candidate), ""
        except Exception as exc:  # noqa: BLE001 - a bad key document is a finding
            return {}, "", f"{candidate} is not a usable key document: {exc}"
    return {}, "", ""


def command_verify(args: argparse.Namespace) -> int:
    from failure_lab.evidence import verify_bundle_integrity
    from failure_lab.verifier import KeySource, verify

    bundle = resolve_bundle(args.bundle)
    if bundle is None:
        sys.stderr.write(
            f"no bundle at {args.bundle}: expected a manifest.json there or in "
            f"{args.bundle / BUNDLE_DIRECTORY_NAME}\n"
        )
        return EXIT_USAGE

    integrity = verify_bundle_integrity(bundle)

    receipt_paths = sorted((bundle / "receipts").glob("*.json"))
    keys, key_origin, key_problem = _load_keys(bundle, args.keys)
    verifications: list[dict[str, Any]] = []
    for path in receipt_paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            verifications.append(
                {"receipt_file": path.name, "error": f"not valid JSON: {exc}"}
            )
            continue
        report = verify(
            document,
            keys,
            # The bundle is being re-checked offline, so whatever key document
            # travelled with it came from the same place the receipts did.
            # Saying ISSUER_ORIGIN is the honest label for that, and it is why
            # issuer trust will come back NOT_ESTABLISHED.
            key_source=KeySource.ISSUER_ORIGIN,
        )
        verifications.append({"receipt_file": path.name, **report.as_dict()})

    valid = sum(
        1
        for entry in verifications
        if any(
            claim.get("claim") == "SIGNATURE_VALID"
            and claim.get("status") == "ESTABLISHED"
            for claim in entry.get("claims", [])
        )
    )
    document = {
        "bundle": str(bundle),
        "integrity": integrity.as_dict(),
        "receipts_found": len(receipt_paths),
        "key_document": key_origin or None,
        "key_document_problem": key_problem or None,
        "verification": verifications,
        "signatures_valid": valid,
        "notes": _verify_notes(receipt_paths, keys, key_origin, key_problem),
    }

    if args.as_json:
        _dump(document)
    else:
        _print(_verify_lines(document, integrity))
    ok = integrity.ok and (not receipt_paths or valid == len(receipt_paths))
    return 0 if ok else 1


def _verify_notes(
    receipt_paths: list[Path],
    keys: dict[str, bytes],
    key_origin: str,
    key_problem: str = "",
) -> list[str]:
    notes: list[str] = []
    if not receipt_paths:
        notes.append(
            "this bundle carries no portable receipts, so no signature was "
            "checked here. That is an absence in the bundle, not a verdict "
            "about the gateway's receipts."
        )
    elif key_problem:
        notes.append(
            f"a trust-keys document was found but could not be used: "
            f"{key_problem}. No signature was checked. This is a failure to "
            "read the keys, not a bundle without keys and not a signature "
            "that failed; pass --keys PATH to supply a usable one."
        )
    elif not keys:
        notes.append(
            "no trust-keys document was found beside the receipts, so no "
            "signature could be checked. Pass --keys PATH to supply one."
        )
    if key_origin:
        notes.append(
            f"keys were read from {key_origin}, which travelled with the "
            "receipts. A key served by the same origin as the receipt cannot "
            "establish issuer trust; only an out-of-band pin can, which is why "
            "ISSUER_TRUST_ESTABLISHED does not come back established here."
        )
    notes.append(
        "integrity proves the bundle matches its own manifest. It cannot "
        "prove the manifest was not edited; only an out-of-band hash of "
        "manifest.json does that."
    )
    notes.append(
        "no signature establishes that a business action occurred. "
        "DOWNSTREAM_EXECUTION_ESTABLISHED is reported separately and requires "
        "an independent observation of the downstream system."
    )
    return notes


def _verify_lines(document: dict[str, Any], integrity: Any) -> list[str]:
    lines = [f"bundle: {document['bundle']}", ""]
    lines.append(
        f"integrity: {'OK' if integrity.ok else 'FAILED'}  "
        f"({integrity.checked} file(s) re-hashed)"
    )
    for name in integrity.changed:
        lines.append(f"  changed:    {name}")
    for name in integrity.missing:
        lines.append(f"  missing:    {name}")
    for name in integrity.unexpected:
        lines.append(f"  unlisted:   {name}")
    lines.append("")
    lines.append(
        f"portable receipts: {document['receipts_found']}  "
        f"signatures verified: {document['signatures_valid']}"
    )
    for entry in document["verification"]:
        if "error" in entry:
            lines.append(f"  {entry['receipt_file']}: {entry['error']}")
            continue
        for claim in entry.get("claims", []):
            lines.append(
                f"  {entry['receipt_file']}  {claim['claim']:<34}{claim['status']}"
            )
            if claim.get("reason"):
                lines.append(f"      {claim['reason']}")
    lines.append("")
    for note in document["notes"]:
        lines.append(f"note: {note}")
    return lines


# --------------------------------------------------------------------------- #
# claims                                                                        #
# --------------------------------------------------------------------------- #


def resolve_claims(path: Path | None) -> Path | None:
    """Find the claims manifest a run wrote.

    ``run`` writes it beside the bundle rather than inside it, because a file
    the bundle's manifest does not list would make
    :func:`~failure_lab.evidence.verify_bundle_integrity` report the bundle as
    tampered with. So ``--bundle`` may name either directory and this looks in
    both.
    """
    if path is None:
        return None
    if path.is_file():
        return path
    for candidate in (path / CLAIMS_FILENAME, path.parent / CLAIMS_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def command_claims(args: argparse.Namespace) -> int:
    from failure_lab.claims import load_claims_manifest, render_markdown

    if args.bundle is None:
        sys.stderr.write(
            "claims needs --bundle DIR: the run directory or evidence bundle a "
            "previous run wrote. The manifest is generated from measurements, "
            "never written by hand.\n"
        )
        return EXIT_USAGE

    path = resolve_claims(args.bundle)
    if path is None:
        sys.stderr.write(
            f"no {CLAIMS_FILENAME} found at {args.bundle} or beside it. "
            "Re-run with --output DIR to keep one.\n"
        )
        return EXIT_USAGE

    manifest = load_claims_manifest(path)
    if args.as_json:
        _dump(manifest.as_dict())
        return 0
    if args.markdown:
        sys.stdout.write(render_markdown(manifest))
        return 0

    divergences = manifest.divergences()
    lines = [
        f"claims manifest: {path}",
        f"generated {manifest.generated_at}  version {manifest.version}  "
        f"definitions {manifest.definition_version}",
        f"environment: {manifest.environment}",
        "",
        f"{'TEST':<6}{'STATUS':<10}{'SUPPORTS':<10}CLAIM",
    ]
    for record in manifest.records:
        supports = "yes" if record.supports_a_public_claim else "no"
        lines.append(
            f"{record.test_id:<6}{record.status:<10}{supports:<10}{record.claim}"
        )
    lines.append("")
    lines.append(
        f"{len(manifest.supported())} of {len(manifest)} claim(s) are supported "
        f"by this run; {len(divergences)} diverged from the documented expectation."
    )
    for record in divergences:
        lines.append(f"  {record.test_id}: {record.claim}")
        for limitation in record.limitations:
            lines.append(f"      {limitation}")
    lines.append("")
    lines.append(
        "A claim is supported only when the scenario observed PASS and the "
        "observation matched what the product documents. A divergence is not "
        "a claim with a caveat; it is a claim that cannot be made until the "
        "documentation and the expectation are updated together."
    )
    _print(lines)
    return 0


# --------------------------------------------------------------------------- #
# explore                                                                       #
# --------------------------------------------------------------------------- #


def command_explore(args: argparse.Namespace) -> int:
    from failure_lab import stateful

    forwarded = ["--seed", str(args.seed)]
    if args.sequences is not None:
        forwarded += ["--sequences", str(args.sequences)]
    if args.max_length is not None:
        forwarded += ["--max-length", str(args.max_length)]
    if args.shrink_budget is not None:
        forwarded += ["--shrink-budget", str(args.shrink_budget)]
    if args.full:
        forwarded.append("--full")
    if args.keep:
        forwarded.append("--keep")
    if args.verbose:
        forwarded.append("--verbose")
    return stateful.main(forwarded)


# --------------------------------------------------------------------------- #
# serve                                                                         #
# --------------------------------------------------------------------------- #


def command_serve(args: argparse.Namespace) -> int:
    """Start the diagnostic server, if it has been written yet.

    The module is imported here and nowhere else, and every entry point it
    might expose is tried by name rather than assumed, so this subcommand does
    not have to be rewritten when ``failure_lab.diagnostic`` lands.

    The loopback check is repeated here rather than left to
    :func:`failure_lab.diagnostic.server.serve`, which enforces it for the
    ``main()`` and ``serve()`` entry points. The third fallback below runs an
    ``app``/``create_app()`` under uvicorn directly, and that path has no such
    check -- so a diagnostic that exposed only an application object could be
    published on ``0.0.0.0`` by this CLI, serving a page that boots a gateway
    and renders evidence. The invariant belongs to whoever calls ``bind``.
    """
    if args.host not in LOOPBACK_HOSTS:
        sys.stderr.write(
            f"refusing to bind {args.host!r}: the diagnostic boots a gateway "
            "with an admin credential in this process and renders evidence "
            "from it, so it is served on the loopback only. Allowed: "
            + ", ".join(sorted(LOOPBACK_HOSTS))
            + "\n"
        )
        return EXIT_USAGE

    try:
        from failure_lab import diagnostic
    except ImportError as exc:
        sys.stderr.write(
            "the diagnostic server is not available in this checkout: "
            f"{type(exc).__name__}: {exc}\n"
            "failure_lab/diagnostic/ has not landed yet. Everything else in "
            f"{PROGRAM} works without it.\n"
        )
        return EXIT_USAGE

    main = getattr(diagnostic, "main", None)
    if callable(main):
        return int(main(["--host", args.host, "--port", str(args.port)]) or 0)

    serve = getattr(diagnostic, "serve", None)
    if callable(serve):
        result = serve(host=args.host, port=args.port)
        return int(result or 0)

    factory = getattr(diagnostic, "create_app", None)
    application = factory() if callable(factory) else getattr(diagnostic, "app", None)
    if application is None:
        sys.stderr.write(
            "failure_lab.diagnostic is importable but exposes none of main(), "
            "serve() or app/create_app(); this CLI cannot tell how to start it.\n"
        )
        return EXIT_USAGE

    import uvicorn

    uvicorn.run(application, host=args.host, port=args.port, log_level="info")
    return 0


# --------------------------------------------------------------------------- #
# Entry point                                                                   #
# --------------------------------------------------------------------------- #

COMMANDS = {
    "list": command_list,
    "run": command_run,
    "verify": command_verify,
    "claims": command_claims,
    "explore": command_explore,
    "serve": command_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    raise SystemExit(main())
