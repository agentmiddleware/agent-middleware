"""GTM-56 gateway ops copy: honesty labels a buyer can rely on.

The failure lab, the discovery docs, and the migration chain must never read
as production proof or as a conflict. Each test below pins one label that the
go-to-market review asked for.
"""

import ast
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

GATEWAY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_refund_tool_states_simulation_and_pilot_replacement() -> None:
    doc = ast.get_docstring(
        ast.parse(
            (GATEWAY_ROOT / "failure_lab" / "refund_tool.py").read_text(
                encoding="utf-8"
            )
        )
    )
    assert doc is not None
    normalized = " ".join(doc.split())
    assert "SIMULATION NOTICE" in normalized
    assert "no real money moves here" in normalized
    assert "no payment rail or wallet settlement" in normalized
    assert "paid pilot must configure an actual upstream tool" in normalized
    assert "This lab configures only its simulated refund tool" in normalized
    assert "register_configured_upstream_mcp" in normalized


def test_llm_base_url_line_explains_template() -> None:
    text = (GATEWAY_ROOT / "static" / "llm.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    base_lines = [line for line in lines if line.startswith("**Base URL:**")]
    assert len(base_lines) == 1
    # The raw file keeps the placeholder and explains both rendering states.
    assert base_lines == ["**Base URL:** {{PUBLIC_URL}}"]
    assert "configured `PUBLIC_URL`" in text
    assert "If unset, the response asks operators to configure it" in text


@pytest.mark.parametrize(
    ("public_url", "expected_base"),
    [
        ("https://api.example.com/", "**Base URL:** https://api.example.com"),
        (
            "",
            "**Base URL:** (configure PUBLIC_URL for this deployment; "
            "local example http://localhost:8000)",
        ),
    ],
)
@pytest.mark.anyio
async def test_served_llms_has_no_unrendered_placeholders(
    client, monkeypatch, public_url: str, expected_base: str
) -> None:
    monkeypatch.setenv("PUBLIC_URL", public_url)
    get_settings.cache_clear()
    try:
        responses = [await client.get(path) for path in ("/llms.txt", "/llm.txt")]
        for resp in responses:
            assert resp.status_code == 200
            assert "{{PUBLIC_URL}}" not in resp.text
            assert [
                line
                for line in resp.text.splitlines()
                if line.startswith("**Base URL:**")
            ] == [expected_base]
            # Template tokens look like {{UPPER_SNAKE}}; single-brace path
            # params such as {receipt_id} and JSON sketches are legitimate.
            assert not re.search(r"\{\{[A-Z_]+\}\}", resp.text)
        assert responses[0].text == responses[1].text
    finally:
        get_settings.cache_clear()


def test_sample_agent_warns_it_is_not_a_shipping_example() -> None:
    text = (
        GATEWAY_ROOT / "failure_lab" / "integration_check" / "sample_app" / "agent.py"
    ).read_text(encoding="utf-8")
    head = text[:2000]
    assert "DO NOT SHIP" in head
    assert "deliberately broken" in head
    assert "never a shipping example" in head


def test_duplicate_025_prefix_carries_a_chain_note() -> None:
    text = (
        GATEWAY_ROOT / "migrations" / "versions" / "025_remove_plaintext_owner_keys.py"
    ).read_text(encoding="utf-8")
    note = ast.get_docstring(ast.parse(text))
    assert note is not None
    normalized = " ".join(note.split())
    assert "Alembic follows" in normalized
    assert "filename sorting" in normalized
    assert "not a branch" in normalized
    version_dir = GATEWAY_ROOT / "migrations" / "versions"
    revisions = {}
    for filename in (
        "025_refresh_token_key_binding.py",
        "025_remove_plaintext_owner_keys.py",
    ):
        tree = ast.parse((version_dir / filename).read_text(encoding="utf-8"))
        revisions[filename] = {
            node.target.id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in {"revision", "down_revision"}
        }
    assert (
        revisions["025_remove_plaintext_owner_keys.py"]["down_revision"]
        == revisions["025_refresh_token_key_binding.py"]["revision"]
    )
