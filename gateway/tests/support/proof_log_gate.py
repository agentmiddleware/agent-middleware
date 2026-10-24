"""Fail the Postgres proof job if any pytest log contains a skipped proof."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def skipped_proofs(paths: list[Path]) -> list[str]:
    """Return skip summaries after removing pytest's ANSI color codes."""
    skipped = []
    for path in paths:
        for line in path.read_text().splitlines():
            plain = ANSI_ESCAPE.sub("", line)
            if plain.startswith("SKIPPED"):
                skipped.append(f"{path.name}: {plain}")
    return skipped


def main() -> int:
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths or any(not path.is_file() for path in paths):
        print("::error::missing Postgres proof log", file=sys.stderr)
        return 1
    skipped = skipped_proofs(paths)
    if skipped:
        for line in skipped:
            print(line)
        print("::error::a database-gated proof skipped instead of running")
        return 1
    print("no gated skips in proof logs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
