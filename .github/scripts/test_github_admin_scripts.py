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
        raise AssertionError((method, path))

    with (
        patch.object(cleanup, "list_branches", return_value=branches),
        patch.object(cleanup, "gh_api", side_effect=api),
        patch.object(cleanup, "delete_branch_with_lease") as delete,
        redirect_stdout(StringIO()),
    ):
        result = cleanup.main(["--repo", "repo", "--keep-prefix", "extra/", "--apply"])

    assert result == 0
    assert len([path for _, path in calls if "/compare/" in path]) == 2
    assert delete.called is deleted
    if deleted:
        delete.assert_called_once_with("repo", "branch", "sha")
    assert all(method != "DELETE" for method, _ in calls)


def test_ref_lookup_preserves_branch_slashes_and_encodes_other_chars():
    branch = {"name": "feature/a+b", "commit": {"sha": "sha"}}
    calls = []

    def api(method, path):
        calls.append((method, path))
        if "/compare/" in path:
            return {"ahead_by": 0}
        if "/branches/" in path:
            return {"commit": {"sha": "sha"}}
        if "/git/ref/" in path:
            return {"object": {"sha": "sha"}}
        raise AssertionError((method, path))

    with (
        patch.object(cleanup, "list_branches", return_value=[branch]),
        patch.object(cleanup, "gh_api", side_effect=api),
        patch.object(cleanup, "delete_branch_with_lease") as delete,
        redirect_stdout(StringIO()),
    ):
        assert cleanup.main(["--repo", "repo", "--apply"]) == 0

    assert ("GET", "repos/repo/git/ref/heads/feature/a%2Bb") in calls
    delete.assert_called_once_with("repo", "feature/a+b", "sha")


@pytest.mark.parametrize("returncode", [0, 1])
def test_delete_branch_uses_scanned_sha_as_atomic_lease(returncode):
    response = SimpleNamespace(returncode=returncode, stderr="rejected", stdout="")
    with patch.object(cleanup.subprocess, "run", return_value=response) as run:
        if returncode:
            with pytest.raises(RuntimeError, match="rejected"):
                cleanup.delete_branch_with_lease("owner/repo", "feature/a+b", "sha")
        else:
            cleanup.delete_branch_with_lease("owner/repo", "feature/a+b", "sha")

    command = run.call_args.args[0]
    assert "--force-with-lease=refs/heads/feature/a+b:sha" in command
    assert "https://github.com/owner/repo.git" in command
    assert ":refs/heads/feature/a+b" in command
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


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


@pytest.mark.parametrize(
    "optional,expected",
    [
        ({}, {}),
        (
            {"dismissal_restrictions": {"users": None, "teams": [{"slug": "team"}]}},
            {"dismissal_restrictions": {"users": [], "teams": ["team"], "apps": []}},
        ),
        (
            {"require_last_push_approval": False, "bypass_pull_request_allowances": {}},
            {
                "require_last_push_approval": False,
                "bypass_pull_request_allowances": {
                    "users": [],
                    "teams": [],
                    "apps": [],
                },
            },
        ),
        (
            {
                "bypass_pull_request_allowances": {
                    "users": None,
                    "teams": None,
                    "apps": None,
                }
            },
            {
                "bypass_pull_request_allowances": {
                    "users": [],
                    "teams": [],
                    "apps": [],
                }
            },
        ),
    ],
)
def test_protection_put_accepts_absent_optional_review_settings(optional, expected):
    rule = {
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
            **optional,
        }
    }
    requests = []

    def api(method, path, body=None):
        requests.append((method, body))
        return rule if method == "GET" else None

    with patch.object(settings, "gh_api", side_effect=api), redirect_stdout(StringIO()):
        assert settings.check_branch_protection("repo", "branch", True, []) == 0

    reviews = requests[-1][1]["required_pull_request_reviews"]
    for field in (
        "require_last_push_approval",
        "dismissal_restrictions",
        "bypass_pull_request_allowances",
    ):
        assert (field in reviews) is (field in expected)
        if field in expected:
            assert reviews[field] == expected[field]


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


@pytest.mark.parametrize(
    "reviews,field",
    [
        ({"require_last_push_approval": None}, "require_last_push_approval"),
        ({"bypass_pull_request_allowances": {"users": "user"}}, "users"),
    ],
)
def test_present_unrepresentable_review_fields_still_fail(reviews, field):
    with pytest.raises(settings.GhError, match=field):
        settings._review_passthrough(reviews)


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
