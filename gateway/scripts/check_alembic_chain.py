"""Check Alembic revision files without importing them or opening a database.

Usage: python scripts/check_alembic_chain.py [VERSIONS_DIR] [--compare OTHER_VERSIONS_DIR] [--json]
The default directory is gateway/migrations/versions, relative to this file.

Only module-level ``revision`` and ``down_revision`` literals are read.
``branch_labels`` and ``depends_on`` are ignored: they are not checked and do
not affect bases, heads, branchpoints, cycles, or --compare results.
In --compare mode every revision id present in only one chain is reported,
whether or not its filename has a numeric prefix.
"""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path


DEFAULT_VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
NUMBERED_NAME = re.compile(r"^(\d+)_.*$")
# Existing, documented naming anomalies. Keep these exact so new ones still fail.
LEGACY_STEM_MISMATCH = {"022_refresh_tokens.py": "3988bd05deca"}
LEGACY_DUPLICATE_PREFIXES = {
    25: frozenset(
        {"025_refresh_token_key_binding.py", "025_remove_plaintext_owner_keys.py"}
    )
}


def _assignment(tree: ast.Module, name: str):
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                values.append(node.value)
    return values


def _read_chain(directory: Path):
    findings = []
    files = {}
    prefixes = {}
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            contents = path.read_bytes()
            tree = ast.parse(contents, filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append(f"{path.name}: cannot parse: {exc}")
            continue

        fields = {}
        for name in ("revision", "down_revision"):
            assignments = _assignment(tree, name)
            if len(assignments) != 1 or assignments[0] is None:
                findings.append(
                    f"{path.name}: expected one module-level {name} assignment"
                )
                continue
            try:
                fields[name] = ast.literal_eval(assignments[0])
            except (ValueError, TypeError, MemoryError, RecursionError, SyntaxError):
                findings.append(f"{path.name}: {name} must be a literal")

        revision = fields.get("revision")
        if not isinstance(revision, str) or not revision:
            findings.append(f"{path.name}: revision must be a nonempty string")
            continue
        down = fields.get("down_revision")
        if "down_revision" not in fields or not (
            down is None
            or isinstance(down, str)
            or isinstance(down, tuple)
            and all(isinstance(item, str) for item in down)
        ):
            findings.append(
                f"{path.name}: down_revision must be a string, tuple of strings, or None"
            )
            continue
        if revision in files:
            findings.append(
                f"duplicate revision {revision}: {files[revision]['path'].name}, {path.name}"
            )
        else:
            files[revision] = {"path": path, "down": down, "contents": contents}

        match = NUMBERED_NAME.fullmatch(path.stem)
        if match:
            prefix = int(match.group(1))
            prefixes.setdefault(prefix, []).append((revision, path))
            if (
                path.stem != revision
                and LEGACY_STEM_MISMATCH.get(path.name) != revision
            ):
                findings.append(
                    f"{path.name}: filename stem differs from revision {revision}"
                )

    if not files:
        findings.append("no revision files found")
    for prefix, entries in sorted(prefixes.items()):
        if len(entries) > 1 and frozenset(path.name for _, path in entries) != (
            LEGACY_DUPLICATE_PREFIXES.get(prefix)
        ):
            findings.append(
                f"duplicate numeric prefix {prefix:03d}: "
                + ", ".join(path.name for _, path in entries)
            )
    if prefixes:
        for prefix in sorted(
            set(range(min(prefixes), max(prefixes) + 1)) - prefixes.keys()
        ):
            findings.append(f"numeric prefix gap: {prefix:03d}")

    children = {revision: [] for revision in files}
    bases = []
    for revision, data in files.items():
        down = data["down"]
        if down is None:
            bases.append(revision)
        for parent in (down,) if isinstance(down, str) else down or ():
            if parent not in files:
                findings.append(f"{revision}: down_revision {parent} is missing")
            else:
                children[parent].append(revision)
    heads = [revision for revision, successors in children.items() if not successors]
    if len(bases) != 1:
        findings.append(
            f"expected one base; found {len(bases)}: {', '.join(sorted(bases))}"
        )
    if len(heads) != 1:
        findings.append(
            f"expected one head; found {len(heads)}: {', '.join(sorted(heads))}"
        )
    for revision, successors in children.items():
        if len(successors) > 1:
            findings.append(f"branchpoint {revision}: {', '.join(sorted(successors))}")
    if any(
        isinstance(data["down"], tuple) and len(data["down"]) > 1
        for data in files.values()
    ):
        findings.append("merge revision found; chain is not linear")
    for cycle in _cycles(files):
        findings.append("cycle detected: " + " -> ".join(cycle + [cycle[0]]))
    if len(bases) == 1:
        visited = set()
        current = bases[0]
        while current in files and current not in visited:
            visited.add(current)
            successors = children[current]
            current = successors[0] if len(successors) == 1 else None
        if len(visited) != len(files):
            findings.append(
                "chain is disconnected or cyclic: "
                + ", ".join(sorted(files.keys() - visited))
            )
    return {
        "directory": str(directory),
        "head": heads[0] if len(heads) == 1 else None,
        "findings": findings,
        "files": files,
        "prefixes": prefixes,
    }


def _cycles(files):
    """Return each down_revision cycle once, as revisions in child-to-parent order."""
    cycles = []
    seen = set()
    for start in sorted(files):
        path = []
        index = {}
        current = start
        while current in files and current not in seen:
            if current in index:
                cycle = path[index[current] :]
                pivot = cycle.index(min(cycle))
                cycles.append(cycle[pivot:] + cycle[:pivot])
                break
            index[current] = len(path)
            path.append(current)
            down = files[current]["down"]
            parents = (down,) if isinstance(down, str) else down or ()
            current = parents[0] if len(parents) == 1 else None
        seen.update(path)
    return cycles


def _digest(contents: bytes) -> str:
    return hashlib.sha256(
        contents.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    ).hexdigest()


def _compare(left, right):
    findings = []
    for prefix in sorted(left["prefixes"].keys() & right["prefixes"].keys()):
        left_ids = {revision for revision, _ in left["prefixes"][prefix]}
        right_ids = {revision for revision, _ in right["prefixes"][prefix]}
        if left_ids != right_ids:
            findings.append(
                f"numeric prefix {prefix:03d} maps to different revisions: "
                f"{', '.join(sorted(left_ids))} vs {', '.join(sorted(right_ids))}"
            )
    for label, only in (
        ("first", left["prefixes"].keys() - right["prefixes"].keys()),
        ("other", right["prefixes"].keys() - left["prefixes"].keys()),
    ):
        for prefix in sorted(only):
            findings.append(f"numeric prefix {prefix:03d} only in {label} chain")
    for label, only in (
        ("first", left["files"].keys() - right["files"].keys()),
        ("other", right["files"].keys() - left["files"].keys()),
    ):
        for revision in sorted(only):
            files = left["files"] if label == "first" else right["files"]
            findings.append(
                f"revision {revision} only in {label} chain "
                f"({files[revision]['path'].name})"
            )
    for revision in sorted(left["files"].keys() & right["files"].keys()):
        if _digest(left["files"][revision]["contents"]) != _digest(
            right["files"][revision]["contents"]
        ):
            findings.append(f"revision {revision} has different content")
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions_dir", nargs="?", type=Path, default=DEFAULT_VERSIONS)
    parser.add_argument("--compare", type=Path, metavar="OTHER_VERSIONS_DIR")
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable results"
    )
    args = parser.parse_args(argv)
    for directory in (args.versions_dir, args.compare):
        if directory is not None and not directory.is_dir():
            parser.error(f"not a versions directory: {directory}")
    left = _read_chain(args.versions_dir)
    right = _read_chain(args.compare) if args.compare else None
    findings = [f"{left['directory']}: {item}" for item in left["findings"]]
    if right:
        findings.extend(f"{right['directory']}: {item}" for item in right["findings"])
        findings.extend(_compare(left, right))
    result = {"head": left["head"], "findings": findings}
    if right:
        result["other_head"] = right["head"]
    if args.json:
        print(json.dumps(result, indent=2))
    elif findings:
        print("Alembic chain findings:")
        for finding in findings:
            print(f"- {finding}")
    else:
        print(f"Alembic chain OK; head: {left['head']}")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
