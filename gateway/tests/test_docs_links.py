"""Docs link and citation checks for the gateway docs slice.

These tests guard the go-to-market docs findings: every relative markdown
link must resolve, every citation of a document that is absent from this
repository must say so next to the citation, the golden path must use the
canonical discovery path, and the docs index must not point at missing
files or anchors.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "gateway"

CHECKED_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "QUICKSTART.md",
    GATEWAY / "README.md",
    GATEWAY / "WEDGE.md",
    GATEWAY / "DEMO_SCRIPT.md",
    GATEWAY / "DESIGN_PARTNER_GUIDE.md",
    GATEWAY / "docs" / "README.md",
    GATEWAY / "docs" / "golden-path.md",
    GATEWAY / "docs" / "quickstart.md",
    GATEWAY / "docs" / "PROOF_SURFACES.md",
    GATEWAY / "docs" / "related-work.md",
    GATEWAY / "docs" / "security-review-kit.md",
    GATEWAY / "docs" / "partner-first-tool-runbook.md",
    GATEWAY / "docs" / "stranger-test.md",
]

# Documents cited by the docs that are absent from this repository. Every
# mention must carry this qualifier nearby so readers know the cited proof
# is not checkable here.
ABSENT_DOCS = [
    "market-research-2026-08.md",
    "30-day-customer-validation.md",
    "tech-debt-remediation-plan.md",
    "hard-run-report-2026-08-12.md",
    "external-surface-review-2026-08-23.md",
]
ABSENT_QUALIFIER = "not included in this repository"
QUALIFIER_WINDOW = 300

# This runbook was referenced by a warning but never existed in this repo.
# The warning was reworded, so no mention should remain.
REMOVED_DOCS = [
    "mcp-registry-submission.md",
]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _slug(heading):
    slug = heading.strip().lower()
    slug = re.sub(r"[^a-z0-9 \-]", "", slug)
    return slug.replace(" ", "-")


def _headings(path):
    slugs = set()
    for line in path.read_text().splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m:
            slugs.add(_slug(m.group(1)))
    return slugs


def test_relative_markdown_links_resolve():
    failures = []
    for source in CHECKED_FILES:
        text = source.read_text()
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(("http", "mailto:")):
                continue
            file_part, _, anchor = target.partition("#")
            resolved = source.parent / file_part if file_part else source
            if file_part and not resolved.exists():
                failures.append(f"{source.name}: missing file {target}")
            elif anchor and resolved.suffix == ".md" and resolved.exists():
                if anchor not in _headings(resolved):
                    failures.append(f"{source.name}: missing anchor {target}")
    assert not failures, "\n".join(failures)


def test_absent_doc_citations_are_qualified():
    failures = []
    for source in CHECKED_FILES:
        text = source.read_text()
        for name in ABSENT_DOCS:
            for match in re.finditer(re.escape(name), text):
                window = text[match.end() : match.end() + QUALIFIER_WINDOW]
                if ABSENT_QUALIFIER not in re.sub(r"\s+", " ", window):
                    line = text.count("\n", 0, match.start()) + 1
                    failures.append(f"{source.name}:{line}: unqualified {name}")
    assert not failures, "\n".join(failures)


def test_removed_doc_is_not_referenced():
    failures = []
    for source in CHECKED_FILES:
        text = source.read_text()
        for name in REMOVED_DOCS:
            if name in text:
                failures.append(f"{source.name}: still references {name}")
    assert not failures, "\n".join(failures)


def test_docs_index_rows_point_at_real_files():
    index = (GATEWAY / "docs" / "README.md").read_text()
    for phrase in ("Intent is not authority", "Deployment verification checklist"):
        for line in index.splitlines():
            if phrase in line:
                assert LINK_RE.search(line), f"unlinked index row: {line}"


def test_agent_bootstrap_anchor_exists():
    gateway_readme = GATEWAY / "README.md"
    assert "agent-bootstrap" in _headings(gateway_readme)
    index = (GATEWAY / "docs" / "README.md").read_text()
    assert "../README.md#agent-bootstrap" in index


def test_golden_path_uses_canonical_discovery_path():
    text = (GATEWAY / "docs" / "golden-path.md").read_text()
    assert 'curl "$API_URL/llms.txt"' in text
    without_canonical = text.replace("/llms.txt", "")
    assert "/llm.txt" not in without_canonical


def test_golden_path_marks_dormant_surfaces_local_only():
    text = (GATEWAY / "docs" / "golden-path.md").read_text()
    criteria = text.split("## Success Criteria", 1)[1]
    assert "Dry-run simulation returns a cost estimate (local only" in criteria
    assert "local only" in criteria and "velocity" in criteria
    assert "ENABLE_PROOF_SURFACES=true" in criteria


def test_root_readme_names_the_repo_split():
    text = (REPO_ROOT / "README.md").read_text()
    assert "agent-middleware-api" in text
    assert "QUICKSTART.md" in text
