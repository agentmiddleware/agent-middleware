"""Exercise the read-only checker against the repository's real migrations."""

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


GATEWAY = Path(__file__).resolve().parents[1]
VERSIONS = GATEWAY / "migrations" / "versions"
SCRIPT = GATEWAY / "scripts" / "check_alembic_chain.py"


def _metadata(path):
    tree = ast.parse(path.read_bytes())
    values = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in {"revision", "down_revision"}:
                values[node.target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    values[target.id] = ast.literal_eval(node.value)
    return values


def _files():
    return sorted(VERSIONS.glob("[0-9]*_*.py"))


def _copy(tmp_path):
    destination = tmp_path / "versions"
    shutil.copytree(VERSIONS, destination)
    return destination


def _run(*args):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, json.loads(completed.stdout)


def _rewrite_down(path, replacement):
    source = path.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "down_revision"
                for target in targets
            ):
                lines = source.splitlines(keepends=True)
                line = lines[node.lineno - 1]
                lines[node.lineno - 1] = re.sub(
                    r"(=\s*)(?:\"[^\"]+\"|'[^']+'|None|\([^)]*\))",
                    lambda match: match.group(1) + repr(replacement),
                    line,
                    count=1,
                )
                path.write_text("".join(lines))
                return
    raise AssertionError(f"no down_revision in {path}")


def test_real_chain_has_one_highest_numbered_head(capsys):
    spec = importlib.util.spec_from_file_location("check_alembic_chain", SCRIPT)
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    result = checker.main(["--json"])
    output = json.loads(capsys.readouterr().out)
    highest = max(_files(), key=lambda path: int(path.stem.split("_", 1)[0]))
    assert result == 0
    assert output["findings"] == []
    assert output["head"] == _metadata(highest)["revision"]


def test_missing_middle_migration_reports_broken_link(tmp_path):
    directory = _copy(tmp_path)
    files = _files()
    middle = files[len(files) // 2]
    (directory / middle.name).unlink()
    status, output = _run(directory)
    assert status == 1
    assert any(
        "down_revision" in finding and "missing" in finding
        for finding in output["findings"]
    )


def test_duplicate_revision(tmp_path):
    directory = _copy(tmp_path)
    original = _files()[0]
    duplicate = directory / f"{original.stem}_copy.py"
    shutil.copyfile(directory / original.name, duplicate)
    status, output = _run(directory)
    assert status == 1
    assert any("duplicate revision" in finding for finding in output["findings"])


def test_multiple_heads_and_branchpoint(tmp_path):
    directory = _copy(tmp_path)
    files = _files()
    victim = files[len(files) // 2]
    earlier = files[len(files) // 4]
    _rewrite_down(directory / victim.name, _metadata(earlier)["revision"])
    status, output = _run(directory)
    assert status == 1
    assert any("expected one head" in finding for finding in output["findings"])
    assert any("branchpoint" in finding for finding in output["findings"])


def test_single_parent_tuple_is_parsed(tmp_path):
    directory = _copy(tmp_path)
    path = _files()[-1]
    parent = _metadata(path)["down_revision"]
    _rewrite_down(directory / path.name, (parent,))
    status, output = _run(directory)
    assert status == 0
    assert output["findings"] == []


def test_numeric_prefix_gap(tmp_path):
    directory = _copy(tmp_path)
    files = _files()
    counts = {}
    for path in files:
        prefix = path.stem.split("_", 1)[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    middle = next(
        path
        for path in files[len(files) // 4 : -len(files) // 4]
        if counts[path.stem.split("_", 1)[0]] == 1
    )
    (directory / middle.name).unlink()
    status, output = _run(directory)
    assert status == 1
    assert any("numeric prefix gap" in finding for finding in output["findings"])


def test_identical_copy_compares_cleanly(tmp_path):
    directory = _copy(tmp_path)
    status, output = _run(VERSIONS, "--compare", directory)
    assert status == 0
    assert output["findings"] == []


def test_same_revision_with_changed_content_fails_comparison(tmp_path):
    directory = _copy(tmp_path)
    path = directory / _files()[-1].name
    path.write_bytes(path.read_bytes() + b"\n# comparison change\n")
    status, output = _run(VERSIONS, "--compare", directory)
    assert status == 1
    assert any("different content" in finding for finding in output["findings"])


def test_same_prefix_with_different_real_revision_fails_comparison(tmp_path):
    directory = _copy(tmp_path)
    earlier, later = _files()[-2:]
    shutil.copyfile(directory / earlier.name, directory / later.name)
    status, output = _run(VERSIONS, "--compare", directory)
    assert status == 1
    assert any(
        "maps to different revisions" in finding for finding in output["findings"]
    )


def test_comparison_normalizes_line_endings(tmp_path):
    directory = _copy(tmp_path)
    path = directory / _files()[-1].name
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    status, output = _run(VERSIONS, "--compare", directory)
    assert status == 0
    assert output["findings"] == []


def test_missing_prefix_is_reported_in_comparison(tmp_path):
    directory = _copy(tmp_path)
    files = _files()
    counts = {}
    for path in files:
        prefix = path.stem.split("_", 1)[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    missing = next(
        path for path in reversed(files) if counts[path.stem.split("_", 1)[0]] == 1
    )
    (directory / missing.name).unlink()
    status, output = _run(VERSIONS, "--compare", directory)
    assert status == 1
    prefix = missing.stem.split("_", 1)[0]
    assert any(
        f"numeric prefix {prefix} only in first chain" in finding
        for finding in output["findings"]
    )


@pytest.mark.parametrize("arguments", [["--unknown"], ["missing-versions-directory"]])
def test_usage_errors_exit_two(arguments):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2


def _numbered(directory):
    return sorted(
        path
        for path in directory.glob("[0-9]*_*.py")
        if path.stem == _metadata(path)["revision"]
    )


def _rewrite_line(path, name, replacement):
    lines = path.read_text().splitlines(keepends=True)
    for number, line in enumerate(lines):
        if re.match(rf"{name}\b\s*[:=]", line):
            lines[number] = replacement
            path.write_text("".join(lines))
            return
    raise AssertionError(f"no {name} in {path}")


def test_legacy_duplicate_prefix_allowlist_is_exact(tmp_path):
    directory = _copy(tmp_path)
    source = _numbered(directory)[-1]
    moved = directory / ("025_" + source.name.split("_", 1)[1])
    source.rename(moved)
    status, output = _run(directory)
    assert status == 1
    assert any(
        "duplicate numeric prefix 025:" in finding and moved.name in finding
        for finding in output["findings"]
    )


def test_new_duplicate_prefix_outside_allowlist_fails(tmp_path):
    directory = _copy(tmp_path)
    numbered = _numbered(directory)
    target, source = numbered[-2], numbered[-1]
    prefix = target.name.split("_", 1)[0]
    moved = directory / (prefix + "_" + source.name.split("_", 1)[1])
    source.rename(moved)
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"duplicate numeric prefix {prefix}:" in finding
        for finding in output["findings"]
    )


def test_legacy_stem_mismatch_allowlist_is_keyed_by_exact_filename(tmp_path):
    directory = _copy(tmp_path)
    legacy = directory / "022_refresh_tokens.py"
    renamed = directory / "022_refresh_tokens_renamed.py"
    legacy.rename(renamed)
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"{renamed.name}: filename stem differs" in finding
        for finding in output["findings"]
    )


def test_new_stem_mismatch_fails(tmp_path):
    directory = _copy(tmp_path)
    source = _numbered(directory)[len(_numbered(directory)) // 2]
    renamed = directory / f"{source.stem}_renamed.py"
    source.rename(renamed)
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"{renamed.name}: filename stem differs" in finding
        for finding in output["findings"]
    )


def test_syntax_error_is_reported(tmp_path):
    directory = _copy(tmp_path)
    path = directory / _files()[-1].name
    path.write_text(path.read_text() + "\ndef broken(:\n")
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"{path.name}: cannot parse" in finding for finding in output["findings"]
    )


def test_missing_revision_assignment_is_reported(tmp_path):
    directory = _copy(tmp_path)
    path = directory / _files()[-1].name
    _rewrite_line(path, "revision", "\n")
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"{path.name}: expected one module-level revision assignment" in finding
        for finding in output["findings"]
    )


def test_duplicate_revision_assignment_is_reported(tmp_path):
    directory = _copy(tmp_path)
    path = directory / _files()[-1].name
    path.write_text(
        path.read_text() + f"\nrevision = {_metadata(path)['revision']!r}\n"
    )
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"{path.name}: expected one module-level revision assignment" in finding
        for finding in output["findings"]
    )


def test_non_literal_down_revision_is_reported(tmp_path):
    directory = _copy(tmp_path)
    path = directory / _files()[-1].name
    _rewrite_line(path, "down_revision", "down_revision = revision\n")
    status, output = _run(directory)
    assert status == 1
    assert any(
        f"{path.name}: down_revision must be a literal" in finding
        for finding in output["findings"]
    )


def test_cycle_error_names_every_revision(tmp_path):
    directory = _copy(tmp_path)
    numbered = _numbered(directory)
    first, second = numbered[-2], numbered[-1]
    _rewrite_down(first, _metadata(second)["revision"])
    status, output = _run(directory)
    assert status == 1
    ids = sorted([_metadata(first)["revision"], _metadata(second)["revision"]])
    expected = f"cycle detected: {ids[0]} -> {ids[1]} -> {ids[0]}"
    assert any(finding.endswith(expected) for finding in output["findings"])


def test_unnumbered_revision_only_in_one_chain_is_reported(tmp_path):
    left = _copy(tmp_path / "left")
    right = _copy(tmp_path / "right")
    last = _numbered(left)[-1]
    revision = _metadata(last)["revision"]
    unnumbered = left / last.name.split("_", 1)[1]
    last.rename(unnumbered)
    (right / last.name).unlink()
    status, output = _run(left, "--compare", right)
    assert status == 1
    assert not any("numeric prefix" in f and "only in" in f for f in output["findings"])
    assert (
        f"revision {revision} only in first chain ({unnumbered.name})"
        in output["findings"]
    )
