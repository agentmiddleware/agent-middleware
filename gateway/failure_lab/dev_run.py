"""Run one scenario standalone, in its own throwaway sandbox.

    python -m failure_lab.dev_run T03
    python -m failure_lab.dev_run T03 --configuration C_gateway_with_native_idempotency
    python -m failure_lab.dev_run T01 --option concurrency=25 --full

Each invocation creates its own run directory, its own SQLite gateway
database and its own effect ledgers, so several of these can run at once
without colliding. It never touches the repository's pytest database.

This is a development affordance. The product surface is
``python -m failure_lab`` (see :mod:`failure_lab.cli`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("test_id", help="Scenario id, e.g. T03")
    parser.add_argument(
        "--configuration",
        "-c",
        action="append",
        default=None,
        help="Limit to one configuration (repeatable). Default: all four.",
    )
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Scenario option, e.g. --option concurrency=25 (repeatable).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print the whole result document rather than a summary.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the run directory instead of deleting it.",
    )
    return parser.parse_args(argv)


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


async def _run(args: argparse.Namespace, run_dir: Path) -> int:
    from failure_lab.gateway import boot_standalone_environment

    admin_key = boot_standalone_environment(run_dir)

    from app.db.database import close_db, init_db
    from app.main import app

    from failure_lab.configurations import Configuration, LabEnvironment
    from failure_lab.scenarios import get_scenario

    options = dict(_coerce_pair(pair) for pair in args.option)
    scenario = get_scenario(args.test_id, **options)
    selected = (
        tuple(Configuration(name) for name in args.configuration)
        if args.configuration
        else None
    )

    await init_db()
    try:
        env = LabEnvironment(run_dir=run_dir, app=app, admin_api_key=admin_key)
        result = await scenario.run(env, configurations=selected)
    finally:
        await close_db()

    document = result.as_dict()
    if args.full:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                _summarize(document, filtered=bool(args.configuration)),
                indent=2,
                sort_keys=True,
            )
        )
    return 0 if result.verdict.value not in ("ERROR",) else 1


def _coerce_pair(pair: str) -> tuple[str, Any]:
    name, _, value = pair.partition("=")
    return name.strip(), _coerce(value)


def _summarize(document: dict[str, Any], *, filtered: bool = False) -> dict[str, Any]:
    """Condense a result document for reading at a terminal.

    ``matches_expectation`` compares the whole documented expectation map
    against what ran, so filtering to one configuration makes it read false
    for the configurations that were never asked to run. That would look like
    a finding and is not one, so a filtered run reports it as null and says
    why rather than printing a number that means something else.
    """
    summary: dict[str, Any] = {
        "test_id": document["test_id"],
        "title": document["title"],
        "verdict": document["verdict"],
        "matches_expectation": (None if filtered else document["matches_expectation"]),
        "configurations": [
            {
                "configuration": entry["configuration"],
                "verdict": entry["verdict"],
                "expected": document["expected"].get(entry["configuration"]),
                "observation": entry["observation"],
                "counters": {
                    name: value
                    for name, value in entry["counters"].items()
                    if value is not None
                },
                "remaining_risks": entry["remaining_risks"],
                "error": entry["error"],
            }
            for entry in document["configurations"]
        ],
    }
    if filtered:
        summary["matches_expectation_note"] = (
            "not computed: this run was limited to a subset of configurations, "
            "so the documented expectation map cannot be checked in full"
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = Path(tempfile.mkdtemp(prefix=f"failure-lab-{args.test_id.lower()}-"))
    try:
        return asyncio.run(_run(args, run_dir))
    finally:
        if args.keep:
            print(f"run directory kept at {run_dir}", file=sys.stderr)
        else:
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
