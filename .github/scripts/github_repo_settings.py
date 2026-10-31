"""Apply the repository settings in gateway/docs/github-org-configuration.md.

Dry run by default: prints each setting as current -> target and changes
nothing. Pass --apply to write. Every call goes through the gh CLI, so it
runs as whatever GitHub account gh is logged in to; that account needs admin
on the repository.

Usage:
  python .github/scripts/github_repo_settings.py                   # show drift
  python .github/scripts/github_repo_settings.py --apply           # fix drift
  python .github/scripts/github_repo_settings.py --repo agentmiddleware/amw-demo

What it manages:
  1. Repository flags: delete merged branches, allow auto-merge, allow
     "update branch", require web commit sign-off.
  2. Branch protection on the default branch. Required status checks are
     kept exactly as they are on the live rule (or taken from
     --required-check when the branch has no rule yet), and the rule is
     rewritten only when one of the other fields drifts: pull request
     required (0 approvals while there is one maintainer), stale approvals
     dismissed, admins not exempt, conversations resolved, no force push,
     no deletion.
  3. Secret scanning and push protection. GitHub refuses these on a private
     repository without the required entitlement. The script reports that
     separately from other API failures.

Exit status is 0 when nothing drifts (or everything was applied), 1 when a
dry run found drift, 2 on a gh failure or an unrepresentable protection rule,
and 3 when GitHub rejects secret scanning for an entitlement or plan reason.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

DEFAULT_REPO = "agentmiddleware/agent-middleware"

REPO_TARGET: dict[str, bool] = {
    # Merged branches pile up otherwise; the archived API repo reached 500+.
    "delete_branch_on_merge": True,
    "allow_auto_merge": True,
    "allow_update_branch": True,
    # DCO sign-off on web commits, matching CONTRIBUTING.md.
    "web_commit_signoff_required": True,
}

# Branch protection fields other than the required-check list. Approvals stay
# at 0 and code-owner review off while the project has one maintainer: GitHub
# does not let an author approve their own pull request, so 1 would block
# every merge. See gateway/docs/PROOF_MATRIX.md.
PROTECTION_TARGET: dict[str, Any] = {
    "enforce_admins": True,
    "required_conversation_resolution": True,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": False,
        "required_approving_review_count": 0,
    },
}


class GhError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def gh_api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Call the GitHub REST API through gh and return the parsed JSON body."""
    cmd = ["gh", "api", "-X", method, path]
    stdin = None
    if body is not None:
        cmd.extend(["--input", "-"])
        stdin = json.dumps(body)
    proc = subprocess.run(cmd, input=stdin, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        status = 0
        try:
            status = int(json.loads(proc.stdout or "{}").get("status", 0))
        except (ValueError, TypeError, AttributeError):
            pass
        if not status:
            match = re.search(r"\bHTTP\s+(403|404)\b", detail)
            if match:
                status = int(match.group(1))
        raise GhError(status, detail)
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def fmt(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def check_repo_flags(repo: str, apply: bool) -> int:
    repo_info = gh_api("GET", f"repos/{repo}")
    drift = {k: v for k, v in REPO_TARGET.items() if repo_info.get(k) != v}
    print(f"\n== Repository flags: {repo}")
    for key, target in REPO_TARGET.items():
        mark = "ok   " if key not in drift else "DRIFT"
        print(f"  {mark} {key}: {fmt(repo_info.get(key))} -> {fmt(target)}")
    if not drift:
        return 0
    if apply:
        gh_api("PATCH", f"repos/{repo}", drift)
        print(f"  applied {len(drift)} change(s)")
        return 0
    return 1


def _protection_view(rule: dict[str, Any]) -> dict[str, Any]:
    """Project the live protection rule onto the PROTECTION_TARGET shape."""
    reviews = rule.get("required_pull_request_reviews")
    view: dict[str, Any] = {
        "enforce_admins": bool(rule.get("enforce_admins", {}).get("enabled")),
        "required_conversation_resolution": bool(
            rule.get("required_conversation_resolution", {}).get("enabled")
        ),
        "allow_force_pushes": bool(rule.get("allow_force_pushes", {}).get("enabled")),
        "allow_deletions": bool(rule.get("allow_deletions", {}).get("enabled")),
        "required_pull_request_reviews": None,
    }
    if reviews is not None:
        view["required_pull_request_reviews"] = {
            "dismiss_stale_reviews": bool(reviews.get("dismiss_stale_reviews")),
            "require_code_owner_reviews": bool(
                reviews.get("require_code_owner_reviews")
            ),
            "required_approving_review_count": int(
                reviews.get("required_approving_review_count", 0)
            ),
        }
    return view


def _review_passthrough(reviews: dict[str, Any]) -> dict[str, Any]:
    """Convert existing review restrictions to the PUT request's identity lists."""
    result: dict[str, Any] = {}
    if "require_last_push_approval" in reviews:
        last_push = reviews["require_last_push_approval"]
        if not isinstance(last_push, bool):
            raise GhError(
                0, "cannot preserve require_last_push_approval from the live rule"
            )
        result["require_last_push_approval"] = last_push
    for field in ("dismissal_restrictions", "bypass_pull_request_allowances"):
        if field not in reviews:
            continue
        value = reviews[field]
        if value is None:
            continue
        if not isinstance(value, dict):
            raise GhError(0, f"cannot preserve {field}: unexpected value")
        converted: dict[str, list[str]] = {}
        for group, identity in (
            ("users", "login"),
            ("teams", "slug"),
            ("apps", "slug"),
        ):
            entries = value.get(group)
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise GhError(0, f"cannot preserve {field}.{group}: expected a list")
            names: list[str] = []
            for entry in entries:
                name = entry.get(identity) if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name:
                    raise GhError(
                        0, f"cannot preserve {field}.{group}: missing {identity}"
                    )
                names.append(name)
            converted[group] = names
        result[field] = converted
    return result


def check_branch_protection(
    repo: str, branch: str, apply: bool, fallback_checks: list[str]
) -> int:
    print(f"\n== Branch protection: {repo}@{branch}")
    try:
        rule = gh_api("GET", f"repos/{repo}/branches/{branch}/protection")
    except GhError as exc:
        if exc.status != 404 and "not protected" not in str(exc).lower():
            raise
        rule = None

    # Fields this script does not manage but the PUT would reset to false if
    # left out; replayed from the live rule so an unrelated fix cannot
    # silently switch them off.
    passthrough = {
        "required_linear_history": False,
        "block_creations": False,
        "lock_branch": False,
        "allow_fork_syncing": False,
    }
    if rule is None:
        if not fallback_checks:
            print(
                "  no classic rule; pass --required-check for each CI check before creating one"
            )
            return 1
        contexts = sorted(fallback_checks)
        required_status_checks = {"strict": True, "contexts": contexts}
        current = {key: None for key in PROTECTION_TARGET}
        print("  no protection rule exists yet")
    else:
        if rule.get("restrictions") is not None:
            raise GhError(
                0, "classic rule has access restrictions; refusing to replace them"
            )
        checks = rule.get("required_status_checks")
        if checks is None:
            contexts = []
            required_status_checks = None
        elif checks.get("checks"):
            app_checks = checks["checks"]
            contexts = sorted(check["context"] for check in app_checks)
            required_status_checks = {
                "strict": bool(checks.get("strict", True)),
                "checks": app_checks,
            }
        else:
            contexts = sorted(checks.get("contexts") or [])
            required_status_checks = {
                "strict": bool(checks.get("strict", True)),
                "contexts": contexts,
            }
        current = _protection_view(rule)
        for key in passthrough:
            passthrough[key] = bool((rule.get(key) or {}).get("enabled"))

    print(
        f"  required checks kept as-is ({len(contexts)}): {', '.join(contexts) or '-'}"
    )
    drift = False
    for key, target in PROTECTION_TARGET.items():
        same = current.get(key) == target
        drift = drift or not same
        mark = "ok   " if same else "DRIFT"
        print(f"  {mark} {key}: {fmt(current.get(key))} -> {fmt(target)}")
    if not drift:
        return 0

    review_target = dict(PROTECTION_TARGET["required_pull_request_reviews"])
    if rule is not None and rule.get("required_pull_request_reviews") is not None:
        review_target.update(_review_passthrough(rule["required_pull_request_reviews"]))

    body = {
        "required_status_checks": required_status_checks,
        "enforce_admins": PROTECTION_TARGET["enforce_admins"],
        "required_pull_request_reviews": review_target,
        "required_conversation_resolution": PROTECTION_TARGET[
            "required_conversation_resolution"
        ],
        "restrictions": None,
        "allow_force_pushes": PROTECTION_TARGET["allow_force_pushes"],
        "allow_deletions": PROTECTION_TARGET["allow_deletions"],
        **passthrough,
    }
    if apply:
        gh_api("PUT", f"repos/{repo}/branches/{branch}/protection", body)
        print("  applied")
        return 0
    print("  would PUT:")
    print("    " + json.dumps(body, indent=2).replace("\n", "\n    "))
    return 1


def check_secret_scanning(repo: str, apply: bool) -> int:
    print(f"\n== Secret scanning: {repo}")
    info = gh_api("GET", f"repos/{repo}")
    analysis = info.get("security_and_analysis") or {}
    wanted = ("secret_scanning", "secret_scanning_push_protection")
    drift = [
        key for key in wanted if (analysis.get(key) or {}).get("status") != "enabled"
    ]
    for key in wanted:
        status = (analysis.get(key) or {}).get("status", "unavailable")
        mark = "ok   " if key not in drift else "DRIFT"
        print(f"  {mark} {key}: {status} -> enabled")
    if not drift:
        return 0
    if not apply:
        return 1
    body = {"security_and_analysis": {key: {"status": "enabled"} for key in drift}}
    try:
        gh_api("PATCH", f"repos/{repo}", body)
    except GhError as exc:
        if exc.status in (403, 404) and re.search(
            r"plan|Advanced Security|Secret Protection|not available", str(exc), re.I
        ):
            print(
                f"  secret scanning is unavailable on this plan: {exc}", file=sys.stderr
            )
            return 3
        raise
    print("  applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument(
        "--branch",
        default=None,
        help="branch to protect (default: the repository's default branch)",
    )
    parser.add_argument(
        "--required-check",
        action="append",
        default=[],
        help=(
            "status check context to require when creating a rule from scratch; "
            "ignored when a rule already exists (repeatable)"
        ),
    )
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args(argv)

    try:
        branch = args.branch or gh_api("GET", f"repos/{args.repo}")["default_branch"]
        results = [
            check_repo_flags(args.repo, args.apply),
            check_branch_protection(args.repo, branch, args.apply, args.required_check),
            check_secret_scanning(args.repo, args.apply),
        ]
    except GhError as exc:
        print(f"gh api failed: {exc}", file=sys.stderr)
        return 2

    if 3 in results:
        return 3
    if any(results):
        print(
            "\nDrift remains." + ("" if args.apply else " Re-run with --apply to fix.")
        )
        return 1
    print("\nNo drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
