"""Phase 6: proof-surface freeze hygiene."""

from __future__ import annotations

import re
from pathlib import Path

from app.main import PROOF_SURFACE_ROUTERS

REPO_ROOT = Path(__file__).resolve().parent.parent
FREEZE_MARKER = "PROOF SURFACE — frozen"
PROOF_SURFACES_DOC = REPO_ROOT / "docs" / "PROOF_SURFACES.md"
WEDGE_DOC = REPO_ROOT / "WEDGE.md"

# Accept/freeze stubs called out in Phase 6 (not all are routers).
ACCEPT_FREEZE_MODULES = (
    REPO_ROOT / "app" / "core" / "blob.py",
    REPO_ROOT / "app" / "services" / "iot_bridge.py",
    REPO_ROOT / "app" / "services" / "awi_rag_engine.py",
    REPO_ROOT / "app" / "services" / "llm.py",
    REPO_ROOT / "app" / "services" / "mcp_phase9_tools.py",
)


def test_proof_surfaces_doc_exists_and_lists_routers():
    assert PROOF_SURFACES_DOC.is_file()
    text = PROOF_SURFACES_DOC.read_text(encoding="utf-8")
    assert "do not expand" in text.lower()
    for mod in PROOF_SURFACE_ROUTERS:
        short = mod.__name__.split(".")[-1]
        assert f"app.routers.{short}" in text, f"missing {short} in freeze list"


def test_wedge_links_proof_surfaces_doc():
    text = WEDGE_DOC.read_text(encoding="utf-8")
    assert "docs/PROOF_SURFACES.md" in text


def test_proof_surface_routers_have_freeze_marker():
    for mod in PROOF_SURFACE_ROUTERS:
        doc = (mod.__doc__ or "").strip()
        assert doc.startswith(FREEZE_MARKER), (
            f"{mod.__name__} docstring must start with {FREEZE_MARKER!r}; got {doc[:80]!r}"
        )


def test_accept_freeze_stub_modules_have_freeze_marker():
    for path in ACCEPT_FREEZE_MODULES:
        text = path.read_text(encoding="utf-8")
        assert FREEZE_MARKER in text[:500], f"{path} missing freeze marker"


def test_proof_surface_routers_tuple_matches_doc_count():
    """Guard against silent growth of PROOF_SURFACE_ROUTERS without doc update."""
    text = PROOF_SURFACES_DOC.read_text(encoding="utf-8")
    # Count only the frozen table rows (``| `app.routers.foo` |``).
    listed = re.findall(r"^\| `(app\.routers\.[a-z0-9_]+)` \|", text, flags=re.M)
    assert len(listed) == len(PROOF_SURFACE_ROUTERS), (
        f"docs table lists {len(listed)} routers, PROOF_SURFACE_ROUTERS has "
        f"{len(PROOF_SURFACE_ROUTERS)}"
    )
    expected = {
        f"app.routers.{m.__name__.split('.')[-1]}" for m in PROOF_SURFACE_ROUTERS
    }
    assert set(listed) == expected
