#!/usr/bin/env python3
"""Fail if any dependency is licensed only under GPL or AGPL.

Usage: check_licenses.py {pip|npm} REPORT.json [--allow FILE]

  pip: output of `pip-licenses --format=json`
  npm: output of `license-checker-rseidelsohn --json`

LGPL is not blocked (it's weak copyleft and fine to depend on from Apache code).
Dual/multi-licensed packages pass if at least one alternative is not GPL/AGPL.
The allowlist file holds one package name per line (# comments ok), for reviewed exceptions.
"""
import json
import re
import sys
from pathlib import Path

STRONG_COPYLEFT = re.compile(
    r"(?<![L\w])A?GPL(?=v?\d|-|\b)|GNU (Affero )?General Public License|\bAffero\b",
    re.IGNORECASE,
)
LESSER = re.compile(r"LGPL|Lesser|Library General Public", re.IGNORECASE)
SPLIT = re.compile(r"\s+OR\s+|\s*;\s*|\s*\|\s*|\s*/\s*", re.IGNORECASE)


def is_blocked(license_expr: str) -> bool:
    alts = [a.strip(" ()*") for a in SPLIT.split(license_expr or "") if a.strip(" ()*")]
    if not alts:
        return False
    def bad(a: str) -> bool:
        return bool(STRONG_COPYLEFT.search(a)) and not LESSER.search(a)
    return all(bad(a) for a in alts)


def main() -> int:
    kind, report = sys.argv[1], Path(sys.argv[2])
    allow = set()
    if "--allow" in sys.argv:
        f = Path(sys.argv[sys.argv.index("--allow") + 1])
        if f.is_file():
            allow = {l.split("#")[0].strip() for l in f.read_text().splitlines() if l.split("#")[0].strip()}
    data = json.loads(report.read_text())
    rows = []
    if kind == "pip":
        for p in data:
            rows.append((p["Name"], p.get("License", "")))
    elif kind == "npm":
        for key, info in data.items():
            at = key.rfind("@")
            name = key[:at] if at > 0 else key
            lic = info.get("licenses", "")
            rows.append((name, " OR ".join(lic) if isinstance(lic, list) else lic))
    else:
        print(f"unknown report kind {kind}", file=sys.stderr)
        return 2
    bad = [(n, l) for n, l in rows if is_blocked(l) and n not in allow]
    for n, l in sorted(rows):
        print(f"{'BLOCKED' if (n, l) in bad else 'ok     '}  {n}: {l}")
    if bad:
        print(f"\n::error::{len(bad)} GPL/AGPL-only dependencies in Apache-2.0 code: "
              + ", ".join(n for n, _ in bad))
        return 1
    print(f"\n{len(rows)} dependencies checked, no GPL/AGPL-only licenses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
