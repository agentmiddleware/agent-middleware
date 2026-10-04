# Contributing to AMW

Thank you for helping improve AMW, Agent Middleware. AMW is a pre-revenue, open-core project maintained by Chris Sellers, GitHub user PetrefiedThunder. The hosted AMW service is proprietary and is not part of this repository.

## Open-core licensing split

The repository has two contribution and licensing areas:

- `spec/`, `verifier/`, `sdk/python/`, `sdk/typescript/`, `integrations/`, and `examples/` are Apache-2.0.
- `gateway/` is Fair Source under FSL-1.1-ALv2 (Functional Source License 1.1, Apache-2.0 future license). It is not OSI open source; each release converts to Apache-2.0 two years after it is published. See `LICENSING.md`.

Please check the license file and any applicable header before contributing. A contribution to a file is made under the license that applies to that area. Contributions to `gateway/` are therefore made under FSL-1.1-ALv2, not Apache-2.0.

We welcome bug fixes, tests, documentation, examples, integrations, SDK improvements, verifier improvements, and thoughtful design discussion. Contributions to the gateway are welcome when they fit the project direction and the FSL terms. We do not accept code for the hosted service through this repository.

## Developer setup

Python 3.11+, [`uv`](https://docs.astral.sh/uv/) (or pip), `make`, and Node 22.18+ for the TypeScript verifier.

```bash
git clone https://github.com/PetrefiedThunder/agent-middleware.git
cd agent-middleware
python -m venv .venv && . .venv/bin/activate
pip install -r gateway/requirements.txt -e "./sdk/python[verify]" pytest pytest-xdist pytest-timeout

# gateway (FSL): full suite, about a minute in parallel
cd gateway && pytest tests -m "not production_trust" -n auto && cd ..

# SDK, integrations, spec vectors (Apache-2.0)
pytest sdk/python/tests
python verifier/check_vectors.py
(cd sdk/typescript && npm test)
```

`make -C gateway prove-trust-plane` and `make -C gateway demo-ambiguous-retry`
exercise the invariants end to end. Never commit `.env` files, keys, or local
databases; `pre-commit install` enables the secret scan locally.

## Developer Certificate of Origin

AMW uses the Developer Certificate of Origin, version 1.1. We do not require a Contributor License Agreement. By contributing, you certify that you have the right to submit the work under the applicable license and that you agree to the DCO.

Sign off each commit by adding a line like this to the commit message:

```text
Signed-off-by: Your Name <your.email@example.com>
```

You can usually add it automatically with:

```bash
git commit -s
```

Pull requests with commits that are not signed off may be asked to correct their history before merge.

## Spec changes and RFCs

Changes to the AMW protocol or other normative material in `spec/` must begin with an RFC issue. Explain the problem, goals, non-goals, compatibility effects, security considerations, and a proposed approach. Link the RFC issue from the pull request. A pull request should not change the normative spec until the RFC discussion has reached a clear decision.

Small editorial fixes do not need a separate RFC issue, but please label them clearly and avoid changing meaning without discussion.

## Pull request process

1. Open or find an issue that describes the problem or proposal. For spec changes, use the spec RFC template.
2. Create a focused branch from the current default branch.
3. Make the smallest coherent change, including documentation and tests where applicable.
4. Run the relevant checks locally and report the commands and results in the pull request.
5. Open a pull request using the template. Link related issues and call out compatibility, security, licensing, and migration concerns.
6. A maintainer will review the change. Review may take time because AMW currently has a solo maintainer.
7. Address requested changes in new commits where practical so the discussion stays easy to follow. A maintainer may ask for a clean or squashed history before merge.

Please do not claim production guarantees, traction, or exactly-once behavior unless the repository has explicitly documented and tested such a claim.

## Commit style

Use a short, imperative subject line, preferably no longer than 72 characters. Add a blank line and a concise body when context is useful. Keep commits focused and use conventional prefixes when they improve clarity, for example:

```text
spec: clarify permit expiry
verifier: reject an invalid receipt signature
sdk: add Python receipt parsing example
```

Every commit submitted for review must include a valid DCO sign-off.

## Tests and checks

Every behavior change must include or update an appropriate automated test. Documentation-only changes should still run link, formatting, or other relevant checks when available. A pull request should pass the applicable test suite, formatter, and linter before merge. If a test cannot be added or a check cannot be run, explain why in the pull request and describe the remaining risk.

## Questions

For general questions, open a discussion issue with enough context for others to follow. For a security concern, follow `SECURITY.md` instead of opening a public issue.
