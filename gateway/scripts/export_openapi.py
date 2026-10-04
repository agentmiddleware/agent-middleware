#!/usr/bin/env python3
"""
Export OpenAPI 3 schema from the FastAPI app to docs/openapi.json.

Agents and mirrors may consume the committed copy; the live app serves the same
shape at GET /openapi.json.

Usage:
  python scripts/export_openapi.py              # write docs/openapi.json
  python scripts/export_openapi.py --check      # exit 1 if file is stale (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_exported(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/openapi.json"),
        help="Destination JSON file (default: docs/openapi.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare output to the file without writing; exit 1 if different.",
    )
    args = parser.parse_args(argv)

    out_path: Path = (
        args.output if args.output.is_absolute() else _REPO_ROOT / args.output
    )

    # Import after argparse so --help stays fast.
    from app.main import app

    spec = app.openapi()

    if args.check:
        if not out_path.is_file():
            print(
                f"Missing {out_path}; run: python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        existing = _load_exported(out_path)
        if existing != spec:
            print(
                f"{out_path} is stale or differs from the app schema. "
                "Run: python scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"{out_path} matches app.openapi()")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote {out_path} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
