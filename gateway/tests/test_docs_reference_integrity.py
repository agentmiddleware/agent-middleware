"""Docs in gateway/docs must name files and revocation behavior that exist.

Operators follow these pages during key incidents and the local quickstart.
A path or script that is not in the tree, or a denial amount the API does
not return, is a broken instruction, not a style nit.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from app.main import DORMANT_TRUST_ROUTERS
from app.schemas.billing import (
    EmergencyKeyRevocationRequest,
    RotateAPIKeyRequest,
)
from app.schemas.pods import MAX_POD_MEMBERS

GATEWAY = Path(__file__).resolve().parents[1]
DOCS = GATEWAY / "docs"
REPO = GATEWAY.parent

BACKTICK_MD = re.compile(r"`([^`\n]*?\.md)`")
BACKTICK_SCRIPT = re.compile(r"`(scripts/[A-Za-z0-9_./-]+\.py)`")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")


def _load_rotate_script():
    spec = importlib.util.spec_from_file_location(
        "rotate_api_keys_for_docs",
        GATEWAY / "scripts" / "rotate_api_keys.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _markdown_ref_resolves(doc: Path, ref: str, *, linked: bool = False) -> bool:
    target = ref.strip().split("#", 1)[0].split("?", 1)[0]
    if not target.endswith(".md") or target.startswith("http"):
        return True
    relative = Path(target)
    if linked:
        return (doc.parent / relative).is_file()
    candidates = [
        (doc.parent / relative).resolve(),
        (GATEWAY / relative).resolve(),
        (REPO / relative).resolve(),
    ]
    return any(path.is_file() for path in candidates)


def test_docs_markdown_references_exist() -> None:
    missing: list[str] = []
    for doc in sorted(DOCS.glob("*.md")):
        text = doc.read_text(encoding="utf-8")
        for match in BACKTICK_MD.finditer(text):
            ref = match.group(1)
            if not _markdown_ref_resolves(doc, ref):
                missing.append(f"{doc.name}: {ref}")
        for match in MARKDOWN_LINK.finditer(text):
            ref = match.group(1)
            if not _markdown_ref_resolves(doc, ref, linked=True):
                missing.append(f"{doc.name}: {ref}")
    assert not missing, (
        "docs name markdown files that are not in the tree:\n" + "\n".join(missing)
    )


def test_docs_script_references_exist() -> None:
    missing: list[str] = []
    for doc in sorted(DOCS.glob("*.md")):
        text = doc.read_text(encoding="utf-8")
        assert "retire_owner_keys.py" not in text, doc.name
        for match in BACKTICK_SCRIPT.finditer(text):
            ref = match.group(1)
            if not (GATEWAY / ref).is_file():
                missing.append(f"{doc.name}: {ref}")
    assert not missing, "docs name scripts that are not in the tree:\n" + "\n".join(
        missing
    )


def test_api_key_rotation_runbook_matches_the_code() -> None:
    text = (DOCS / "api-key-rotation.md").read_text(encoding="utf-8")
    rotate = _load_rotate_script()

    assert RotateAPIKeyRequest.model_fields["revoke_old"].default is False
    assert EmergencyKeyRevocationRequest.model_fields["create_new_key"].default is True
    assert "`revoke_old` defaults to false" in text
    assert "`create_new_key` defaults to true" in text
    assert rotate.CHECK_PATH == "/v1/audit/summary"
    assert rotate.CHECK_PATH in text
    assert rotate.KEY_PREFIX == "amw_live_"
    assert rotate.KEY_PREFIX in text
    for name in ("AGENT_MIDDLEWARE_API_URL", "OLD_API_KEY", "NEW_API_KEY"):
        assert name in text
    assert "401" in text and "403" in text
    assert "bounded_key_cannot_mint" in text
    assert "wallet_access_denied" in text
    assert "scripts/rotate_api_keys.py" in text
    assert "from the gateway directory" in text
    assert "`revoke_old: true`" in text
    assert "`422` `invalid_rotation_request`" in text
    assert "bounded wallet key may call emergency revoke" in text
    assert "caller key's remaining bounds" in text
    assert "revokes the wallet's refresh tokens" in text
    assert "POST /v1/api-keys/rotate" in text
    assert "POST /v1/api-keys/emergency-revoke" in text
    assert "DELETE /v1/api-keys/{wallet_id}/{key_id}" in text
    assert "STATIC_DEV_API_KEYS" in text
    assert "amw_dev_" in text
    assert "key-management.md" in text
    # A restart is required: get_settings caches VALID_API_KEYS for the process.
    assert "restart" in text


def test_pods_doc_matches_the_dormant_router() -> None:
    text = (DOCS / "pods.md").read_text(encoding="utf-8")
    schema = " ".join(
        (GATEWAY / "app" / "schemas" / "pods.py").read_text(encoding="utf-8").split()
    )
    names = {module.__name__.rsplit(".", 1)[-1] for module in DORMANT_TRUST_ROUTERS}
    assert "pods" in names
    assert MAX_POD_MEMBERS == 50
    assert "50" in text
    assert "ENABLE_PROOF_SURFACES" in text
    assert "require_bootstrap_admin" in text
    assert "POST /v1/pods" in text
    for limit in (
        "no department templates",
        "no on-demand provisioning trigger",
        "no cross-pod budget sharing",
    ):
        assert limit in schema
        assert limit in text


def test_quickstart_denial_amounts_and_working_directory() -> None:
    text = (DOCS / "quickstart.md").read_text(encoding="utf-8")
    pin = (GATEWAY / "tests" / "test_quickstart_path.py").read_text(encoding="utf-8")
    assert 'details["required_credits"] == "2.0"' in pin
    assert 'details["remaining_credits"] == "1.00000000"' in pin
    assert '`required_credits: "2.0"`' in text
    assert '`remaining_credits: "1.00000000"`' in text
    second_terminal = text.split("second terminal", 1)[1].split("## 2.", 1)[0]
    assert "gateway directory" in second_terminal
    assert "repository root" not in second_terminal


def test_human_preflight_uses_the_gateway_directory() -> None:
    text = (DOCS / "human-onboarding.md").read_text(encoding="utf-8")
    section = text.split("## Preflight script", 1)[1].split("---", 1)[0]
    assert "bash scripts/human_preflight.sh" in section
    assert "gateway directory" in section
    assert "repository root" not in section


def test_readme_index_does_not_name_missing_material() -> None:
    text = (DOCS / "README.md").read_text(encoding="utf-8")
    assert "[API key rotation](api-key-rotation.md)" in text
    assert "[Pods](pods.md) are a dormant trust feature" in text
    for missing in (
        "mcp-registry-submission.md",
        "aegis/",
        "superpowers/",
        "Intent is not authority",
        "Deployment verification checklist",
    ):
        assert missing not in text, missing
