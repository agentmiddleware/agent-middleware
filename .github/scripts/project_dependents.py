#!/usr/bin/env python3
"""Expand a pull-request project set with the projects that depend on it.

detect_projects.py lists directories that exist. On pull_request the workflow
keeps only projects that own a changed path, then calls expand_with_dependents
so a change is also tested by its consumers (gateway installs ../sdk/python;
each integration pins that path; gateway tests import or load the wrappers).

Edges come from the tree, not a hand-maintained matrix:

- manifest path refs (pyproject.toml, requirements*.txt, setup.cfg, setup.py,
  package.json) and Dockerfile COPY/ADD sources
- the gateway install in .github/workflows/ci.yml (`-e "../sdk/python..."`)
- imports of another project's package from that project's source and tests
- an f-string load of ``integrations/{framework}-agent-middleware``

FALLBACK_DEPENDS_ON is only the floor for edges those scans can miss. A name
that is not a discovered project is ignored.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "fixtures",
    "testdata",
}
SOURCE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".mjs", ".cjs"}
MANIFEST_NAMES = ("pyproject.toml", "setup.cfg", "setup.py", "package.json")

# Floor for edges the scanners are not guaranteed to see. Gateway tests load
# langchain/crewai/autogen by building
# ``integrations/{framework}-agent-middleware`` (see
# gateway/tests/test_wrapper_permit_retry_contract.py), which is not a static
# path or an import name. sdk/python and openai are also listed so a pythonpath
# or uv.sources edit cannot drop the jobs the review requires. Dynamic scans
# still add any other edge (a future package.json ``file:`` dep, for example).
FALLBACK_DEPENDS_ON = {
    "gateway": (
        "sdk/python",
        "integrations/openai-agent-middleware",
        "integrations/langchain-agent-middleware",
        "integrations/crewai-agent-middleware",
        "integrations/autogen-agent-middleware",
    ),
    "integrations/openai-agent-middleware": ("sdk/python",),
    "integrations/langchain-agent-middleware": ("sdk/python",),
    "integrations/crewai-agent-middleware": ("sdk/python",),
    "integrations/autogen-agent-middleware": ("sdk/python",),
}

_REL_PATH = re.compile(r"(?:\.\./|\./)[A-Za-z0-9_./-]+")
_COPY = re.compile(r"(?m)^\s*(?:COPY|ADD)\s+(?:--\S+\s+)*(\S+)\s+\S+")
_PY_FROM = re.compile(r"(?m)^\s*from\s+([A-Za-z_][\w.]*)\s+import\b")
_PY_IMPORT = re.compile(r"(?m)^\s*import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)")
_JS_SPEC = re.compile(
    r"""(?:from|import)\s*(?:\(\s*)?["']([^"']+)["']|require\(\s*["']([^"']+)["']\s*\)"""
)
_HATCH_PACKAGES = re.compile(r"(?m)^packages\s*=\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"""["']([^"']+)["']""")
_CONSTRUCTED_INTEGRATION = re.compile(r"integrations/\{[^}\n]+\}-agent-middleware")
_INTEGRATION_PROJECT = re.compile(r"integrations/.+-agent-middleware\Z")
_WORKFLOW_SDK_INSTALL = re.compile(
    r"""-e\s+["']\.\./sdk/python(?:\[[^\]]+\])?["']"""
)


def is_shared(path: str, projects: list[str]) -> bool:
    if path in (".github", "spec") or path.startswith((".github/", "spec/")):
        return True
    if "/" not in path:
        return True
    return not any(path == proj or path.startswith(proj + "/") for proj in projects)


def matched(path: str, projects: list[str]) -> list[str]:
    return [proj for proj in projects if path == proj or path.startswith(proj + "/")]


def _project_owning(rel: str, projects: list[str]) -> str | None:
    owners = [proj for proj in projects if rel == proj or rel.startswith(proj + "/")]
    if not owners:
        return None
    return max(owners, key=len)


def _rel_under_root(root: Path, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    text = rel.as_posix()
    if text in ("", "."):
        return None
    return text


def _manifest_files(project_dir: Path) -> list[Path]:
    found = []
    for name in MANIFEST_NAMES:
        candidate = project_dir / name
        if candidate.is_file():
            found.append(candidate)
    found.extend(sorted(project_dir.glob("requirements*.txt")))
    for candidate in sorted(project_dir.glob("Dockerfile*")):
        if candidate.is_file():
            found.append(candidate)
    return found


def _path_refs(text: str, *, dockerfile: bool) -> list[tuple[str, str]]:
    """Return (kind, raw) path tokens. kind is 'project' or 'repo'."""
    refs = [("project", match.group(0)) for match in _REL_PATH.finditer(text)]
    if dockerfile:
        for match in _COPY.finditer(text):
            source = match.group(1)
            if source in (".", "..") or source.startswith(("--", "http://", "https://")):
                continue
            if "*" in source:
                continue
            refs.append(("repo" if not source.startswith(".") else "project", source))
    return refs


def _resolve_ref(root: Path, project_dir: Path, kind: str, raw: str) -> str | None:
    token = raw.strip().strip("\"'")
    if token.startswith("file:"):
        token = token[5:]
    if token.startswith("link:"):
        token = token[5:]
    token = re.sub(r"\[.*\]\Z", "", token).rstrip("/")
    if not token or "://" in token or token in (".", ".."):
        return None
    base = root if kind == "repo" else project_dir
    return _rel_under_root(root, base / token)


def _import_names(project_dir: Path) -> set[str]:
    names: set[str] = set()
    src = project_dir / "src"
    if src.is_dir():
        for child in src.iterdir():
            if child.name.startswith(".") or child.name in SKIP_DIRS:
                continue
            if child.is_dir() and (child / "__init__.py").is_file():
                names.add(child.name)
            elif child.suffix == ".py" and child.name != "__init__.py":
                names.add(child.stem)
    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        for block in _HATCH_PACKAGES.finditer(text):
            for quoted in _QUOTED.findall(block.group(1)):
                leaf = Path(quoted).name
                if leaf and leaf not in {".", ".."}:
                    names.add(leaf)
    package_json = project_dir / "package.json"
    if package_json.is_file():
        try:
            meta = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None
        if isinstance(meta, dict) and isinstance(meta.get("name"), str) and meta["name"]:
            names.add(meta["name"])
    return names


def _iter_source_files(project_dir: Path):
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".")
        ]
        for name in filenames:
            if Path(name).suffix in SOURCE_SUFFIXES:
                yield Path(dirpath) / name


def _imported_modules(path: Path, text: str) -> set[str]:
    found: set[str] = set()
    if path.suffix in {".py", ".pyi"}:
        for match in _PY_FROM.finditer(text):
            found.add(match.group(1).split(".", 1)[0])
        for match in _PY_IMPORT.finditer(text):
            for part in match.group(1).split(","):
                name = part.strip().split(".", 1)[0]
                if name:
                    found.add(name)
        return found
    for match in _JS_SPEC.finditer(text):
        spec = match.group(1) or match.group(2)
        if spec and not spec.startswith("."):
            found.add(spec)
    return found


def dependency_edges(root: Path, projects: list[str]) -> list[tuple[str, str]]:
    """Return sorted (dependent, dependency) pairs among discovered projects."""
    root = root.resolve()
    project_set = list(dict.fromkeys(projects))
    depends: dict[str, set[str]] = {proj: set() for proj in project_set}

    def add(dependent: str, dependency: str | None) -> None:
        if dependency and dependency != dependent and dependency in depends:
            depends[dependent].add(dependency)

    for proj in project_set:
        project_dir = root / proj
        if not project_dir.is_dir():
            continue
        for manifest in _manifest_files(project_dir):
            text = manifest.read_text(encoding="utf-8", errors="ignore")
            dockerfile = manifest.name.startswith("Dockerfile")
            for kind, raw in _path_refs(text, dockerfile=dockerfile):
                add(proj, _project_owning(_resolve_ref(root, project_dir, kind, raw) or "", project_set))

    name_owners: dict[str, list[str]] = {}
    for proj in project_set:
        for name in _import_names(root / proj):
            name_owners.setdefault(name, []).append(proj)
    unique_names = {
        name: owners[0] for name, owners in name_owners.items() if len(owners) == 1
    }

    for proj in project_set:
        project_dir = root / proj
        if not project_dir.is_dir():
            continue
        saw_constructed = False
        for source in _iter_source_files(project_dir):
            text = source.read_text(encoding="utf-8", errors="ignore")
            for module in _imported_modules(source, text):
                add(proj, unique_names.get(module))
            if _CONSTRUCTED_INTEGRATION.search(text):
                saw_constructed = True
        if saw_constructed:
            for other in project_set:
                if _INTEGRATION_PROJECT.fullmatch(other):
                    add(proj, other)

    workflow = root / ".github" / "workflows" / "ci.yml"
    if workflow.is_file() and _WORKFLOW_SDK_INSTALL.search(
        workflow.read_text(encoding="utf-8", errors="ignore")
    ):
        add("gateway", "sdk/python")

    for dependent, dependencies in FALLBACK_DEPENDS_ON.items():
        if dependent not in depends:
            continue
        for dependency in dependencies:
            add(dependent, dependency)

    edges = [
        (dependent, dependency)
        for dependent, deps in depends.items()
        for dependency in deps
    ]
    return sorted(edges)


def expand_with_dependents(root: Path, projects: list[str], keep: set[str]) -> set[str]:
    """Add every project that depends on a kept project, transitively."""
    reverse: dict[str, set[str]] = {proj: set() for proj in projects}
    for dependent, dependency in dependency_edges(root, projects):
        reverse.setdefault(dependency, set()).add(dependent)
    expanded = set(keep)
    stack = list(keep)
    while stack:
        dependency = stack.pop()
        for dependent in reverse.get(dependency, ()):
            if dependent not in expanded:
                expanded.add(dependent)
                stack.append(dependent)
    return expanded


def limit_matrix(
    root: Path,
    py: list[str],
    ts: list[str],
    changed: list[str],
    *,
    event: str,
    force_full: bool,
) -> tuple[list[str], list[str], bool]:
    """Apply the pull_request filter. Non-PR events and shared paths stay full.

    gateway_postgres is true for a full matrix and whenever gateway is in the
    expanded keep set (or a changed path is already under gateway/).
    """
    if event != "pull_request" or force_full:
        return list(py), list(ts), True

    projects = list(dict.fromkeys([*py, *ts]))
    keep: set[str] = set()
    for path in changed:
        if is_shared(path, projects):
            print(f"shared path {path}; using the full matrix")
            return list(py), list(ts), True
        keep.update(matched(path, projects))

    before = set(keep)
    keep = expand_with_dependents(root, projects, keep)
    added = [proj for proj in projects if proj in keep and proj not in before]
    if added:
        print(f"expanded keep with dependents: {added}")
    py_out = [proj for proj in py if proj in keep]
    ts_out = [proj for proj in ts if proj in keep]
    print(f"pull_request matrix limited to python={py_out} typescript={ts_out}")
    gateway_postgres = "gateway" in keep or any(
        path == "gateway" or path.startswith("gateway/") for path in changed
    )
    return py_out, ts_out, gateway_postgres


def _load_discovered(root: Path) -> tuple[list[str], list[str]]:
    env = os.environ.copy()
    env.pop("GITHUB_OUTPUT", None)
    env["GITHUB_WORKSPACE"] = str(root)
    proc = subprocess.run(
        [sys.executable, str(root / ".github" / "scripts" / "detect_projects.py")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
    )
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key] = value
    return json.loads(found["python"]), json.loads(found["typescript"])


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    py, ts = _load_discovered(root)
    projects = list(dict.fromkeys([*py, *ts]))
    if argv == ["--edges"]:
        for dependent, dependency in dependency_edges(root, projects):
            print(f"{dependent} depends on {dependency}")
        return 0
    if argv != ["--replay"]:
        print(
            "usage: project_dependents.py --replay | --edges",
            file=sys.stderr,
        )
        return 2
    print("edges:")
    for dependent, dependency in dependency_edges(root, projects):
        print(f"  {dependent} depends on {dependency}")
    samples = (
        ("sdk/python only", ["sdk/python/src/b2a_sdk/client.py"]),
        (
            "one integration only",
            ["integrations/openai-agent-middleware/src/openai_b2a/client.py"],
        ),
        ("gateway only", ["gateway/app/main.py"]),
        ("sdk/typescript only", ["sdk/typescript/src/verify.ts"]),
    )
    for label, changed in samples:
        print(f"SAMPLE {label}")
        out_py, out_ts, postgres = limit_matrix(
            root, py, ts, changed, event="pull_request", force_full=False
        )
        print(f"  python={json.dumps(out_py)}")
        print(f"  typescript={json.dumps(out_ts)}")
        print(f"  gateway_postgres={str(postgres).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
