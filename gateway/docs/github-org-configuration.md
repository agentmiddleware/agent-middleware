# GitHub organization configuration

This runbook ports the operational guidance from archived API pull request
#829 to `agentmiddleware/agent-middleware`. It describes intended settings for
the monorepo. The archived PR's 2026-10-19 through 2026-10-22 audit snapshots,
Actions run IDs, branch counts, and history-rewrite claims describe a different
repository and are not evidence of this repository's current GitHub settings.

Run commands from the monorepo root. The scripts default to dry runs. Inspect
their output before any separate authorized settings change or branch deletion.

## Repository settings

The settings script reports repository flags, classic branch protection, and
GitHub secret scanning status for the selected repository. Its default target
is `agentmiddleware/agent-middleware`; `--repo` selects another repository.
It does not manage organization rulesets. It keeps the classic rule's existing
required checks when proposing a change. If no classic rule exists, supply
each required check with `--required-check` before creating one. The default
dry run exits 1 when it finds drift.

```bash
python3 .github/scripts/github_repo_settings.py
python3 .github/scripts/github_repo_settings.py --repo agentmiddleware/amw-demo
```

The script's target is to delete merged branches after merge, allow auto merge
and branch updates, require signoff for web commits, require a pull request
with zero approvals for a solo maintainer, dismiss stale reviews, include
admins, resolve conversations, and disallow force pushes and branch deletion.
Review the live ruleset and [proof matrix](PROOF_MATRIX.md) before applying a
classic rule. The monorepo's documented required checks are `CI result` and
`gitleaks`; [CONTRIBUTING.md](../../CONTRIBUTING.md) also names `License result`.
Use the actual current check names rather than assuming this list is complete.

`--apply` changes repository settings. Do not run it as part of an audit.
GitHub may decline secret scanning or push protection on a private repository
without the required plan. That does not change the local or CI gitleaks checks.

## Branch cleanup

The cleanup script compares each unprotected branch with the base branch and
lists branches with zero commits ahead. It never selects the base branch, a
protected branch, or a branch with a configured keep prefix. It rechecks the
tip and comparison before a deletion. An unrelated branch should be reviewed
manually; do not infer it is safe to delete from age alone.

```bash
python3 .github/scripts/github_branch_cleanup.py
python3 .github/scripts/github_branch_cleanup.py --stale-days 90
```

`--apply` deletes branches selected by the script. It is a separate action
after review of the dry-run list. The script does not delete the archived
repository's former `master` branch or change workflow triggers by itself.

## Dependabot and Actions budget

[Dependabot configuration](../../.github/dependabot.yml) groups Python,
TypeScript, Docker, and GitHub Actions updates for this monorepo. Its Python
directories include `/gateway`, `/sdk/python`, and `/integrations/*`; its npm
directories include `/sdk/typescript`. The configuration also lists `/spec`,
`/verifier`, and `/examples/*`, which currently have no package manifests.
Dockerfiles are under `/gateway`. There is no root `requirements.lock`,
`.railway` npm project, or `b2a_sdk` directory here.
The `gateway` and SDK package manifests remain the source of dependency pins.
Version and security updates are grouped separately. A grouped pull request
still needs its usual review and required CI checks.

For budget triage, inspect the organization Billing and licensing pages and
the repository Actions runs. The old API repository's 2026-10-19 billing
failure and public visibility change are historical context only. They do
not establish this monorepo's present billing state. Set a budget alert at
the organization level if needed; account for private repositories and
larger runners separately.

## Secret scanning and the history boundary

The monorepo's [secrets workflow](../../.github/workflows/secrets.yml) uses
gitleaks 8.30.1. Push and pull request runs scan history reachable from
`HEAD`; scheduled and manual runs scan all fetched refs. The workflow has no
pinned verified base and does not call `secret_scan_range.sh`. The local
[pre-commit configuration](../../.pre-commit-config.yaml) also runs gitleaks.
The [gitleaks rules](../../.gitleaks.toml) allowlist narrow fake test values
and lockfile hashes; do not import the archived repository's history-rewrite
marker or a broad path allowlist into this configuration.

The archived API repository rewrote its history in October 2026. Its pinned
SHA and key-rotation incident record belong to that repository. No claim
about this monorepo's complete history, old clones, forks, or GitHub garbage
collection follows from the archived audit. A current full-history scan and
repository-specific investigation are needed to make such claims here.

## Organization checklist

Organization settings are checked in GitHub's UI. The scripts above operate
at the repository level and cannot establish these organization values.

| Page | Intended setting | Reason |
| --- | --- | --- |
| Authentication security | Require two-factor authentication | Limit account takeover risk |
| Member privileges | Read as the base permission; owners control repository creation | Keep write access explicit |
| Actions: General | Limit allowed actions; use read-only default workflow permissions | Reduce workflow authority |
| Actions: General | Require approval for external fork workflows | Review new external code before it runs |
| Code security | Enable Dependabot alerts, secret scanning, and push protection where available | Cover dependency and credential exposure |
| Code security | Allow private vulnerability reporting for public repositories | Give reporters a private channel |
| Billing and licensing | Set an Actions budget alert | Detect unexpected runner costs |
| Repositories: Rulesets | Protect the default branch and require current checks | Keep merges gated |
| Profile | Verify the product domain if owned | Identify the organization |

The archived PR listed `.github`, `agent-middleware-api`, `agent-middleware`,
`agent-middleware-public`, `agent-middleware-spec`, `amw-demo`, and
`amw-paypal-agent-demo` as organization repositories on 2026-10-22. Verify
their current existence, visibility, and settings before acting on them.

## Link and scope notes

The archived PR replaced stale personal-namespace links in its old checkout.
This port updates matching links only in files present here. Historical
records and registry identity fields need their own review before edits.
This runbook and its scripts make no GitHub changes unless an operator invokes
an explicit `--apply` mode. They do not alter CI check requirements, secret
scanning policy, or deployment settings by themselves.
