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
    extra_env: dict[str, str] | None = None,
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
    env.pop("RAILWAY_GIT_COMMIT_SHA", None)
    env.pop("GIT_COMMIT_SHA", None)
    env.update(extra_env or {})
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


# Resolution order: RELEASE_COMMIT_SHA > RAILWAY_GIT_COMMIT_SHA >
# GIT_COMMIT_SHA > staged file. Every set source must agree.


def test_railway_sha_alone_is_used():
    assert resolve_stamp(None, "", SHA.upper(), "") == SHA


def test_ci_sha_alone_is_used():
    assert resolve_stamp(None, "", "", SHA) == SHA


def test_staged_file_is_last_resort():
    assert resolve_stamp(SHA + "\n") == SHA


@pytest.mark.parametrize(
    ("release", "railway", "ci", "staged", "loser"),
    [
        (OTHER, SHA, "", None, "RAILWAY_GIT_COMMIT_SHA"),
        ("", OTHER, SHA, None, "GIT_COMMIT_SHA"),
        ("", "", OTHER, SHA + "\n", "staged .build_commit_sha"),
        (OTHER, "", "", SHA + "\n", "staged .build_commit_sha"),
    ],
)
def test_any_disagreement_fails_instead_of_using_stale_value(
    release, railway, ci, staged, loser
):
    with pytest.raises(SystemExit, match=f"disagrees with {loser}"):
        resolve_stamp(staged, release, railway, ci)


def test_all_sources_agreeing_returns_highest_precedence_lowercase():
    assert resolve_stamp(SHA + "\n", SHA.upper(), SHA, SHA[:]) == SHA


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"railway_sha": "xyz1234"}, "RAILWAY_GIT_COMMIT_SHA"),
        ({"ci_sha": "abc12"}, "GIT_COMMIT_SHA"),
        ({"ci_sha": "a" * 41}, "GIT_COMMIT_SHA"),
    ],
)
def test_invalid_source_fails_by_name(kwargs, name):
    with pytest.raises(SystemExit, match=name):
        resolve_stamp(None, **kwargs)


def test_forty_char_upper_bound():
    assert resolve_stamp(None, ci_sha="a" * 40) == "a" * 40


def test_script_reads_railway_and_ci_build_args(tmp_path):
    result, output = _run(
        tmp_path,
        None,
        None,
        extra_env={"RAILWAY_GIT_COMMIT_SHA": SHA, "GIT_COMMIT_SHA": SHA.upper()},
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == SHA + "\n"


def test_script_fails_when_pinned_override_is_stale(tmp_path):
    result, output = _run(
        tmp_path, None, OTHER, extra_env={"RAILWAY_GIT_COMMIT_SHA": SHA}
    )
    assert result.returncode != 0
    assert "RELEASE_COMMIT_SHA disagrees with RAILWAY_GIT_COMMIT_SHA" in result.stderr
    assert not output.exists()
