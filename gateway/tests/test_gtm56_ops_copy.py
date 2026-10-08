"""GTM-56 gateway ops copy: honesty labels a buyer can rely on.

The failure lab, the discovery docs, and the migration chain must never read
as production proof or as a conflict. Each test below pins one label that the
go-to-market review asked for.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

GATEWAY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_refund_tool_states_simulation_and_pilot_replacement() -> None:
    doc = (GATEWAY_ROOT / "failure_lab" / "refund_tool.py").read_text(
        encoding="utf-8"
    )
    assert "SIMULATION NOTICE" in doc
    assert "no real money moves here" in doc
    assert "paid pilot" in doc
    assert "register_configured_upstream_mcp" in doc


def test_llm_base_url_line_explains_template() -> None:
    lines = (GATEWAY_ROOT / "static" / "llm.txt").read_text(encoding="utf-8").splitlines()
    base_lines = [line for line in lines if line.startswith("**Base URL:**")]
    assert len(base_lines) == 1
    # The raw file keeps the serving placeholder token, but the same line must
    # tell an offline reader how it gets rendered.
    assert "{{PUBLIC_URL}}" in base_lines[0]
    assert "configured PUBLIC_URL" in base_lines[0]


@pytest.mark.anyio
async def test_served_llms_has_no_unrendered_placeholders(client) -> None:
    import re

    resp = await client.get("/llms.txt")
    assert resp.status_code == 200
    assert "{{PUBLIC_URL}}" not in resp.text
    # Template tokens look like {{UPPER_SNAKE}}; single-brace path params
    # such as {receipt_id} and JSON sketches are legitimate content.
    assert not re.search(r"\{\{[A-Z_]+\}\}", resp.text)


def test_sample_agent_warns_it_is_not_a_shipping_example() -> None:
    text = (
        GATEWAY_ROOT
        / "failure_lab"
        / "integration_check"
        / "sample_app"
        / "agent.py"
    ).read_text(encoding="utf-8")
    head = text[:2000]
    assert "DO NOT SHIP" in head
    assert "never a shipping example" in head


def test_duplicate_025_prefix_carries_a_chain_note() -> None:
    text = (
        GATEWAY_ROOT
        / "migrations"
        / "versions"
        / "025_remove_plaintext_owner_keys.py"
    ).read_text(encoding="utf-8")
    assert "025_refresh_token_key_binding" in text
    assert "applies immediately after" in text
    assert "not a" in text and "branch" in text
