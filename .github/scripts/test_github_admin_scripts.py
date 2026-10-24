"""Offline checks for the repository administration scripts."""

import importlib.util
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def load_script(name):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = load_script("github_branch_cleanup.py")
settings = load_script("github_repo_settings.py")


@pytest.mark.parametrize("ref_sha,deleted", [("moved", False), ("sha", True)])
def test_keep_prefix_adds_to_defaults_and_ref_must_match(ref_sha, deleted):
    branches = [
        {"name": name, "commit": {"sha": "sha"}}
        for name in ("backup/one", "extra/one", "branch")
    ]
    calls = []

    def api(method, path):
        calls.append((method, path))
        if "/compare/" in path:
            return {"ahead_by": 0}
        if "/branches/" in path:
            return {"commit": {"sha": "sha"}}
        if "/git/ref/" in path:
            return {"object": {"sha": ref_sha}}
        if method == "DELETE":
            return None
        raise AssertionError((method, path))

    with (
        patch.object(cleanup, "list_branches", return_value=branches),
        patch.object(cleanup, "gh_api", side_effect=api),
        redirect_stdout(StringIO()),
    ):
        result = cleanup.main(["--repo", "repo", "--keep-prefix", "extra/", "--apply"])

    assert result == 0
    assert len([path for _, path in calls if "/compare/" in path]) == 2
    assert any(method == "DELETE" for method, _ in calls) is deleted


def test_stale_tip_failure_reports_branch_and_fails_scan():
    branch = {"name": "branch", "commit": {"sha": "sha"}}

    def api(method, path):
        if "/compare/" in path:
            return {"ahead_by": 1}
        raise RuntimeError("lookup failed")

    stderr = StringIO()
    with (
        patch.object(cleanup, "list_branches", return_value=[branch]),
        patch.object(cleanup, "gh_api", side_effect=api),
        redirect_stdout(StringIO()),
        redirect_stderr(stderr),
    ):
        result = cleanup.main(["--repo", "repo", "--stale-days", "1"])

    assert result == 1
    assert "branch: commit lookup failed (lookup failed)" in stderr.getvalue()


def test_protection_put_preserves_review_settings():
    rule = {
        "required_status_checks": {"strict": True, "contexts": []},
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
            "require_last_push_approval": True,
            "dismissal_restrictions": {
                "users": [{"login": "user"}],
                "teams": [{"slug": "team"}],
                "apps": [{"slug": "app"}],
            },
            "bypass_pull_request_allowances": {
                "users": [],
                "teams": [],
                "apps": [],
            },
        },
    }
    requests = []

    def api(method, path, body=None):
        requests.append((method, body))
        return rule if method == "GET" else None

    with patch.object(settings, "gh_api", side_effect=api), redirect_stdout(StringIO()):
        assert settings.check_branch_protection("repo", "branch", True, []) == 0

    reviews = requests[-1][1]["required_pull_request_reviews"]
    assert reviews["require_last_push_approval"] is True
    assert reviews["dismissal_restrictions"] == {
        "users": ["user"],
        "teams": ["team"],
        "apps": ["app"],
    }
    assert reviews["bypass_pull_request_allowances"] == {
        "users": [],
        "teams": [],
        "apps": [],
    }


def test_unrepresentable_review_rule_fails_before_put():
    with pytest.raises(
        settings.GhError, match="cannot preserve dismissal_restrictions"
    ):
        settings._review_passthrough(
            {
                "require_last_push_approval": True,
                "dismissal_restrictions": {"users": [], "teams": [], "apps": [{}]},
                "bypass_pull_request_allowances": None,
            }
        )


def test_gh_api_reads_http_status_from_cli_error():
    response = SimpleNamespace(
        returncode=1, stderr="HTTP 403: plan unavailable", stdout=""
    )
    with patch.object(settings.subprocess, "run", return_value=response):
        with pytest.raises(settings.GhError) as error:
            settings.gh_api("GET", "path")
    assert error.value.status == 403


@pytest.mark.parametrize(
    "status,message,expected",
    [
        (403, "Advanced Security plan", 3),
        (404, "not available", 3),
        (403, "permission denied", 2),
    ],
)
def test_secret_scan_error_codes(status, message, expected):
    def api(method, path, body=None):
        if method == "GET":
            return {"security_and_analysis": {}}
        raise settings.GhError(status, message)

    with (
        patch.object(settings, "gh_api", side_effect=api),
        patch.object(settings, "check_repo_flags", return_value=0),
        patch.object(settings, "check_branch_protection", return_value=0),
        redirect_stdout(StringIO()),
        redirect_stderr(StringIO()),
    ):
        assert (
            settings.main(["--repo", "repo", "--branch", "branch", "--apply"])
            == expected
        )
