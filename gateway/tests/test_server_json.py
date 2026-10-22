import json
import tomllib
from pathlib import Path

from app.services.mcp_generator import MCP_SERVER_VERSION


REPO_ROOT = Path(__file__).resolve().parents[2]


def _server_json():
    return json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))


def test_server_json_is_valid_json():
    assert isinstance(_server_json(), dict)


def test_server_identity_and_version():
    server = _server_json()
    with (REPO_ROOT / "gateway" / "pyproject.toml").open("rb") as pyproject_file:
        gateway_version = tomllib.load(pyproject_file)["project"]["version"]

    assert server["$schema"] == (
        "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    )
    assert server["name"] == "io.github.agentmiddleware/agent-middleware"
    assert server["title"] == "Agent Middleware"
    assert server["websiteUrl"] == "https://www.agentmiddleware.dev/"
    assert server["repository"]["url"] == (
        "https://github.com/agentmiddleware/agent-middleware"
    )
    assert server["repository"]["source"] == "github"
    assert server["version"] == gateway_version
    assert server["version"] == MCP_SERVER_VERSION
    assert "packages" not in server


def test_server_description_is_within_registry_limit():
    description = _server_json()["description"]
    assert description == (
        "Permits, signed receipts and safe retries for agent tool calls."
    )
    assert len(description) <= 100


def test_server_has_one_streamable_http_remote_with_api_key():
    remotes = _server_json()["remotes"]
    assert len(remotes) == 1
    remote = remotes[0]
    assert remote["type"] == "streamable-http"

    # The advertised remote is the opt-in standard MCP surface. It is off by
    # default (ENABLE_STANDARD_MCP_ENDPOINT=False), so the host must enable it.
    assert remote["url"] == "https://api.thisisatest.tech/mcp"
    assert remote["headers"] == [
        {
            "name": "X-API-Key",
            "isRequired": True,
            "isSecret": True,
            "description": "AMW API key. Get a free demo key at https://www.agentmiddleware.dev/demo-key/",
        }
    ]
