"""List, and optionally delete, remote branches already merged into main.

Dry run by default: nothing is deleted unless --apply is given, and even then
only branches whose every commit is already reachable from the base branch
(GitHub reports them as 0 commits ahead). Protected branches, the base branch,
and anything under a --keep-prefix are never touched. Unmerged branches are
never deleted by this script; with --stale-days they are listed separately
so you can review them by hand.

Usage:
  python .github/scripts/github_branch_cleanup.py                  # list merged branches
  python .github/scripts/github_branch_cleanup.py --apply          # delete them
  python .github/scripts/github_branch_cleanup.py --stale-days 90  # also list old unmerged ones

Runs through the gh CLI as the logged-in account; deleting needs write
access to the repository. The scan makes one compare request per branch, so a
repository with 500 branches takes a few minutes.

Before each deletion, the script reads the ref again and requires its SHA to
match the scanned SHA. REST ref deletion has no SHA precondition, so a branch
can still move between that final read and DELETE.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

DEFAULT_REPO = "agentmiddleware/agent-middleware"
DEFAULT_KEEP_PREFIXES = ("backup/",)


def gh_api(method: str, path: str) -> Any:
    proc = subprocess.run(
        ["gh", "api", "-X", method, path], check=False, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def list_branches(repo: str) -> list[dict[str, Any]]:
    branches: list[dict[str, Any]] = []
    page = 1
    while True:
        chunk = gh_api("GET", f"repos/{repo}/branches?per_page=100&page={page}")
        branches.extend(chunk)
        if len(chunk) < 100:
            return branches
        page += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name")
    parser.add_argument("--base", default="main", help="branch merged into")
    parser.add_argument(
        "--keep-prefix",
        action="append",
        default=None,
        help=f"also keep branches starting with this (always kept: {', '.join(DEFAULT_KEEP_PREFIXES)})",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        help="also list unmerged branches whose last commit is older than this",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete merged branches after a final ref SHA check; a move before DELETE remains possible",
    )
    args = parser.parse_args(argv)
    keep_prefixes = DEFAULT_KEEP_PREFIXES + tuple(args.keep_prefix or ())

    try:
        branches = list_branches(args.repo)
    except RuntimeError as exc:
        print(f"gh api failed: {exc}", file=sys.stderr)
        return 2

    merged: list[tuple[str, str]] = []  # (name, sha seen during the scan)
    stale: list[tuple[str, str, int]] = []
    scan_failures = 0
    skipped = {"protected": 0, "kept": 0, "base": 0}
    cutoff = None
    if args.stale_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=args.stale_days)

    print(f"{len(branches)} branches on {args.repo}; comparing against {args.base} ...")
    for index, branch in enumerate(branches, start=1):
        name = branch["name"]
        if name == args.base:
            skipped["base"] += 1
            continue
        if branch.get("protected"):
            skipped["protected"] += 1
            continue
        if name.startswith(keep_prefixes):
            skipped["kept"] += 1
            continue
        try:
            compare = gh_api(
                "GET",
                f"repos/{args.repo}/compare/{quote(args.base, safe='')}...{quote(name, safe='')}",
            )
        except RuntimeError as exc:
            scan_failures += 1
            print(f"  ? {name}: compare failed ({exc})", file=sys.stderr)
            continue
        if compare.get("ahead_by") == 0:
            merged.append((name, branch["commit"]["sha"]))
        elif cutoff is not None:
            # The compare response lists at most one page of commits, oldest
            # first, so its last entry is not the tip of a long branch. Read
            # the tip commit itself.
            try:
                tip = gh_api(
                    "GET", f"repos/{args.repo}/commits/{branch['commit']['sha']}"
                )
            except RuntimeError as exc:
                scan_failures += 1
                print(f"  ? {name}: commit lookup failed ({exc})", file=sys.stderr)
                continue
            last = tip["commit"]["committer"]["date"]
            if datetime.fromisoformat(last) < cutoff:
                stale.append((name, last[:10], int(compare.get("ahead_by", 0))))
        if index % 50 == 0:
            print(f"  ... {index}/{len(branches)}")

    print(
        f"\nSkipped: {skipped['protected']} protected, {skipped['kept']} kept by prefix, "
        f"{skipped['base']} base."
    )
    print(f"\nMerged into {args.base} (safe to delete): {len(merged)}")
    for name, _sha in merged:
        print(f"  {name}")
    if cutoff is not None:
        print(
            f"\nUnmerged and older than {args.stale_days} days (review by hand): {len(stale)}"
        )
        for name, date, ahead in sorted(stale, key=lambda item: item[1]):
            print(f"  {date}  {ahead:4d} ahead  {name}")

    if not args.apply:
        if merged:
            print("\nDry run. Re-run with --apply to delete the merged branches.")
        return 1 if scan_failures else 0

    # The scan above takes minutes on a large repository. A branch that moved
    # in the meantime is re-checked right before its ref is deleted: same tip
    # as during the scan, and still zero commits ahead of the base.
    failures = scan_failures
    deleted = 0
    for name, scanned_sha in merged:
        encoded = quote(name, safe="")
        try:
            tip = gh_api("GET", f"repos/{args.repo}/branches/{encoded}")["commit"][
                "sha"
            ]
            if tip != scanned_sha:
                print(f"  skipped {name}: moved since the scan ({tip[:8]})")
                continue
            recheck = gh_api(
                "GET",
                f"repos/{args.repo}/compare/{quote(args.base, safe='')}...{encoded}",
            )
            if recheck.get("ahead_by") != 0:
                print(f"  skipped {name}: now {recheck.get('ahead_by')} ahead")
                continue
            ref_sha = gh_api("GET", f"repos/{args.repo}/git/ref/heads/{encoded}")[
                "object"
            ]["sha"]
            if ref_sha != scanned_sha:
                print(f"  skipped {name}: ref moved since the scan ({ref_sha[:8]})")
                continue
            # REST DELETE has no expected-SHA precondition. A move after this
            # read and before DELETE can still delete the new ref target.
            gh_api("DELETE", f"repos/{args.repo}/git/refs/heads/{encoded}")
            deleted += 1
            print(f"  deleted {name}")
        except RuntimeError as exc:
            failures += 1
            print(f"  failed {name}: {exc}")
    print(f"\nDeleted {deleted} of {len(merged)} merged branches.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
