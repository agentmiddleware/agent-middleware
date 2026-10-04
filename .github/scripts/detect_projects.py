#!/usr/bin/env python3
"""Discover Python and TypeScript projects that actually exist in the repo.

Writes GitHub Actions outputs (to $GITHUB_OUTPUT when set, else stdout):
  python          JSON list of dirs containing pyproject.toml
  typescript      JSON list of dirs containing package.json (workspace members folded into their root)
  python_apache   subset of `python` whose governing license is Apache-2.0
  typescript_apache subset of `typescript` whose governing license is Apache-2.0
  has_python / has_typescript / has_python_apache / has_typescript_apache   "true"/"false"
  codeql_languages JSON list for the CodeQL matrix

Only the AMW layout roots are searched, so stray fixtures elsewhere are ignored.
The governing license of a dir is the nearest LICENSE* file walking up to the repo root;
gateway/ (FSL-1.1-ALv2) is therefore excluded from the Apache-only license gate.
"""
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
SEARCH_ROOTS = ["spec", "verifier", "sdk/python", "sdk/typescript", "integrations", "examples", "gateway"]
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "fixtures", "testdata"}
MAX_DEPTH = 3  # e.g. integrations/<name>/pyproject.toml, examples/<name>/ts/package.json


def walk(base: Path):
    if not base.is_dir():
        return
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        yield d
        if depth >= MAX_DEPTH:
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
                stack.append((child, depth + 1))


def governing_license(d: Path) -> str:
    cur = d
    while True:
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            f = cur / name
            if f.is_file():
                text = f.read_text(errors="ignore")
                if "Functional Source License" in text or "FSL-1.1" in text:
                    return "FSL"
                if "Apache License" in text:
                    return "Apache-2.0"
                return "other"
        if cur == ROOT or cur.parent == cur:
            return "none"
        cur = cur.parent


def rel(d: Path) -> str:
    return d.relative_to(ROOT).as_posix()


py, ts = [], []
for r in SEARCH_ROOTS:
    for d in walk(ROOT / r):
        if (d / "pyproject.toml").is_file():
            py.append(d)
        if (d / "package.json").is_file():
            ts.append(d)

# Fold npm/pnpm/yarn workspace members into the workspace root.
def is_workspace_root(d: Path) -> bool:
    if (d / "pnpm-workspace.yaml").is_file():
        return True
    try:
        return "workspaces" in json.loads((d / "package.json").read_text())
    except Exception:
        return False

roots = [d for d in ts if is_workspace_root(d)]
ts = [d for d in ts if not any(r != d and r in d.parents for r in roots)]

py_s = sorted({rel(d) for d in py})
ts_s = sorted({rel(d) for d in ts})
py_a = [p for p in py_s if governing_license(ROOT / p) == "Apache-2.0"]
ts_a = [p for p in ts_s if governing_license(ROOT / p) == "Apache-2.0"]

codeql = ["actions"]
if py_s or any(ROOT.joinpath(r).is_dir() and any(ROOT.joinpath(r).rglob("*.py")) for r in SEARCH_ROOTS):
    codeql.append("python")
if ts_s:
    codeql.append("javascript-typescript")

out = {
    "python": json.dumps(py_s),
    "typescript": json.dumps(ts_s),
    "python_apache": json.dumps(py_a),
    "typescript_apache": json.dumps(ts_a),
    "has_python": str(bool(py_s)).lower(),
    "has_typescript": str(bool(ts_s)).lower(),
    "has_python_apache": str(bool(py_a)).lower(),
    "has_typescript_apache": str(bool(ts_a)).lower(),
    "codeql_languages": json.dumps(codeql),
}
dest = os.environ.get("GITHUB_OUTPUT")
with (open(dest, "a") if dest else open(os.devnull, "w")) as fh:
    for k, v in out.items():
        fh.write(f"{k}={v}\n")
for k, v in out.items():
    print(f"{k}={v}")
