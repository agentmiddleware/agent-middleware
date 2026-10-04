"""scripts/human_preflight.sh against a local stand-in service.

Production-like services lock their tool catalogs (#444): an unauthenticated
read answers 401. The operator checklist script must expect that there, keep
expecting public catalogs on a local-compatible service, and never read
/v1/discover anonymously where a key is required.
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "human_preflight.sh"

CATALOGS = {
    "/v1/discover": "Discover index",
    "/mcp/tools.json": "MCP tools manifest",
    "/mcp/tools": "MCP tools list",
    "/.well-known/mcp/tools.json": "Well-known MCP (alternate route)",
}
REFUSAL = {
    "detail": {
        "error": "missing_credentials",
        "message": "X-API-Key or Authorization: Bearer header is required.",
        "docs": "/docs",
    }
}
AGENT_FIRST = {"start_here": "/.well-known/agent.json"}

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None,
    reason="needs bash and curl",
)


def _handler(production_like, catalog_status, requests):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            requests.append((path, self.headers.get("X-API-Key")))
            status = 200
            if path == "/health/dependencies":
                body = {
                    "status": "healthy",
                    "version": "1.3.0",
                    "unhealthy": [],
                    "enable_proof_surfaces": False,
                }
                if production_like != "absent":
                    body["production_like"] = production_like
            elif path in CATALOGS:
                status = catalog_status
                body = REFUSAL if status == 401 else {"agent_first": AGENT_FIRST}
            elif path == "/.well-known/agent.json":
                body = {"agent_first": AGENT_FIRST}
            elif path in {"/health", "/openapi.json", "/llm.txt"}:
                body = {}
            else:
                status, body = 404, {"detail": "Not Found"}
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    return Handler


@pytest.fixture
def stand_in():
    servers = []

    def start(*, production_like, catalog_status):
        requests = []
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler(production_like, catalog_status, requests)
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", requests

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(api_url):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
    }
    env.update(API_URL=api_url, NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_production_like_service_passes_with_locked_catalogs(stand_in):
    api_url, requests = stand_in(production_like=True, catalog_status=401)

    result = _run(api_url)

    assert result.returncode == 0, result.stdout + result.stderr
    for path, name in CATALOGS.items():
        assert f"  401  {name}  {path}" in result.stdout
    assert "SKIP" in result.stdout
    assert "/v1/discover requires an API key" in result.stdout
    # One status probe per catalog and no anonymous body read of discovery.
    assert [path for path, _ in requests].count("/v1/discover") == 1
    assert all(api_key is None for _, api_key in requests)


def test_production_like_service_fails_when_a_catalog_is_public(stand_in):
    api_url, _ = stand_in(production_like=True, catalog_status=200)

    result = _run(api_url)

    assert result.returncode == 1
    for path, name in CATALOGS.items():
        assert f"200 (expected 401)  {name}  {path}" in result.stdout


def test_local_service_keeps_public_catalogs_and_agent_first_check(stand_in):
    api_url, _ = stand_in(production_like=False, catalog_status=200)

    result = _run(api_url)

    assert result.returncode == 0, result.stdout + result.stderr
    for path, name in CATALOGS.items():
        assert f"  200  {name}  {path}" in result.stdout
    assert "agree on agent_first" in result.stdout


def test_local_service_fails_when_catalogs_are_locked(stand_in):
    api_url, _ = stand_in(production_like=False, catalog_status=401)

    result = _run(api_url)

    assert result.returncode == 1
    assert "401 (expected 200)  Discover index  /v1/discover" in result.stdout


@pytest.mark.parametrize("production_like", ["absent", None, "true"])
def test_unreadable_production_posture_fails(stand_in, production_like):
    api_url, _ = stand_in(production_like=production_like, catalog_status=401)

    result = _run(api_url)

    assert result.returncode == 1
    assert "does not report production_like as true or false" in result.stdout
