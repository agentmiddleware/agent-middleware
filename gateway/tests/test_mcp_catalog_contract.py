from pathlib import Path


def test_llm_txt_documents_mcp_catalog_and_transport_auth_boundaries():
    repository_root = Path(__file__).resolve().parents[1]
    text = (repository_root / "static/llm.txt").read_text()

    assert (
        "On production-like boots, GET /mcp/tools.json requires an operator-issued "
        "key and returns 401 without one. Local quickstart stays anonymous. "
        "GET /.well-known/agent.json, GET /llms.txt, and GET /openapi.json stay "
        "unauthenticated."
    ) in text
    assert "Required on production-like boots; anonymous for local quickstart" in text
    assert "POST /mcp/public" in text
    assert "public read-only tools only" in text
    assert "opt-in local-compatible environments only" in text
    assert "unavailable in production-like environments" in text
    assert "POST /mcp/messages" in text
    assert "credentialed legacy JSON-RPC transport" in text
    assert "| MCP discovery | `/mcp/tools.json` | Public |" not in text
    assert "POST /mcp` | None" not in text
