"""Production image stamp: staged file or RELEASE_COMMIT_SHA, never a missing COPY."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.write_build_commit_sha import resolve_stamp

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "write_build_commit_sha.py"
SHA = "abcdef0123456789abcdef0123456789abcdef01"
OTHER = "1234567890abcdef1234567890abcdef12345678"


def _run(
    tmp_path: Path,
    staged: str | None,
    build_arg: str | None,
    nested: str | None = None,
):
    context = tmp_path / "context"
    context.mkdir()
    stamp = context / ".build_commit_sha"
    output = tmp_path / ".build_commit_sha"
    if staged is not None:
        stamp.write_text(staged, encoding="utf-8")
    if nested is not None:
        (context / "gateway").mkdir()
        (context / "gateway" / ".build_commit_sha").write_text(nested, encoding="utf-8")
    env = os.environ.copy()
    env["STAGED_SHA_PATH"] = str(stamp)
    env["BUILD_SHA_PATH"] = str(output)
    if build_arg is None:
        env.pop("RELEASE_COMMIT_SHA", None)
    else:
        env["RELEASE_COMMIT_SHA"] = build_arg
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output


def test_build_arg_writes_lowercase_stamp_with_newline(tmp_path):
    result, output = _run(tmp_path, None, SHA.upper())
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == SHA + "\n"


def test_missing_stamp_and_empty_arg_fails(tmp_path):
    result, output = _run(tmp_path, None, "")
    assert result.returncode != 0
    assert "Release commit stamp missing" in result.stderr
    assert not output.exists()


def test_invalid_build_arg_fails(tmp_path):
    result, output = _run(tmp_path, None, "not-a-sha")
    assert result.returncode != 0
    assert "RELEASE_COMMIT_SHA" in result.stderr
    assert not output.exists()


def test_staged_file_is_authoritative(tmp_path):
    result, output = _run(tmp_path, SHA + "\n", "")
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == SHA + "\n"


def test_staged_and_arg_may_differ_only_by_case(tmp_path):
    result, output = _run(tmp_path, SHA.upper() + "\n", SHA)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == SHA + "\n"


def test_disagreement_fails_and_leaves_staged_file(tmp_path):
    result, output = _run(tmp_path, SHA + "\n", OTHER)
    assert result.returncode != 0
    assert "disagrees" in result.stderr
    assert not output.exists()
    assert (tmp_path / "context" / ".build_commit_sha").read_text() == SHA + "\n"


def test_invalid_staged_file_is_not_replaced_by_build_arg(tmp_path):
    result, output = _run(tmp_path, "not-a-sha\n", SHA)
    assert result.returncode != 0
    assert "Staged .build_commit_sha" in result.stderr
    assert not output.exists()


def test_invalid_arg_does_not_override_valid_staged_file(tmp_path):
    result, output = _run(tmp_path, SHA + "\n", "not-a-sha")
    assert result.returncode != 0
    assert "RELEASE_COMMIT_SHA" in result.stderr
    assert not output.exists()


def test_nested_stamp_alone_cannot_supply_provenance(tmp_path):
    result, output = _run(tmp_path, None, None, nested=SHA + "\n")
    assert result.returncode != 0
    assert "Release commit stamp missing" in result.stderr
    assert not output.exists()


def test_root_stamp_overwrites_conflicting_nested_stamp(tmp_path):
    result, output = _run(tmp_path, SHA + "\n", None, nested=OTHER + "\n")
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == SHA + "\n"


def test_stale_service_variables_cannot_supply_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMIT_SHA", SHA)
    monkeypatch.setenv("BUILD_COMMIT_SHA", SHA)
    result, output = _run(tmp_path, None, None)
    assert result.returncode != 0
    assert not output.exists()


def test_short_sha_rejected():
    with pytest.raises(SystemExit):
        resolve_stamp(None, "abc123")


def test_seven_char_sha_accepted():
    assert resolve_stamp(None, "abc1234") == "abc1234"
