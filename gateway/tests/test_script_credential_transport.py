"""Credential-bearing scripts must stay on the origin the operator named.

Ambient proxy variables and HTTP redirects can move an API key onto a host
the operator did not select. These tests use local sockets only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from scripts.constant_test_loop import SmokeTestFailure, run_constant_test
from scripts.partner_api_key_bootstrap import provision

REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = "synthetic-canary"
BOOTSTRAP_CANARY = "synthetic-bootstrap-canary"
_PROXY_ENV = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


class _Handler(BaseHTTPRequestHandler):
    def _read_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

    def _dispatch(self) -> None:
        self._read_body()
        self.server.hits.append(
            {
                "method": self.command,
                "path": self.path,
                "key": self.headers.get("X-API-Key"),
            }
        )
        status, body, extra = self.server.reply(self)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for name, value in extra.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def log_message(self, *_args: object) -> None:
        return None


def _url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


@contextmanager
def _server(reply: Callable):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.hits = []
    server.reply = reply
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _refuse(_handler: BaseHTTPRequestHandler):
    return 500, {"detail": "proxy"}, {}


def _use_proxy(monkeypatch: pytest.MonkeyPatch, proxy_url: str) -> None:
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    for name in _PROXY_ENV:
        monkeypatch.setenv(name, proxy_url)


def _load_attacklib():
    attack_dir = str(REPO_ROOT / "scripts" / "invariant_attacks")
    if attack_dir not in sys.path:
        sys.path.insert(0, attack_dir)
    import attacklib

    return attacklib


def test_attacklib_does_not_forward_key_on_redirect_or_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacklib = _load_attacklib()

    def origin_reply(_handler: BaseHTTPRequestHandler):
        return (
            302,
            {"detail": "redirect refused"},
            {"Location": f"{_url(collector)}/steal"},
        )

    with (
        _server(origin_reply) as origin,
        _server(_refuse) as collector,
        _server(_refuse) as proxy,
    ):
        _use_proxy(monkeypatch, _url(proxy))
        monkeypatch.setattr(attacklib, "API", _url(origin))
        result = attacklib.http(
            "POST",
            "/v1/audit/summary",
            api_key=CANARY,
            body={"synthetic": True},
        )

        assert result["status"] == 302
        assert result["json"] == {"detail": "redirect refused"}
        assert proxy.hits == []
        assert collector.hits == []
        assert origin.hits[0]["key"] == CANARY
        assert urlparse(origin.hits[0]["path"]).path == "/v1/audit/summary"


def test_adversarial_battery_ignores_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import adversarial_battery as battery

    def origin_reply(_handler: BaseHTTPRequestHandler):
        return 200, {"ok": True}, {}

    with _server(origin_reply) as origin, _server(_refuse) as proxy:
        _use_proxy(monkeypatch, _url(proxy))
        monkeypatch.setattr(battery, "API_URL", _url(origin))
        status, body = battery.req("GET", "/v1/audit/summary", key=CANARY)

        assert status == 200
        assert body == {"ok": True}
        assert proxy.hits == []
        assert origin.hits[0]["key"] == CANARY


def test_constant_test_loop_keeps_keys_off_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def origin_reply(handler: BaseHTTPRequestHandler):
        path = handler.path.split("?", 1)[0]
        if handler.command == "POST" and path == "/v1/dev-keys/self-provision":
            return (
                201,
                {
                    "api_key": "amw_dev_originissued",
                    "wallet_id": "agt-originwallet",
                    "key_id": "kid-origin",
                },
                {},
            )
        if handler.command == "GET" and path == "/v1/billing/wallets":
            return 200, {"wallets": [{"wallet_id": "agt-originwallet"}]}, {}
        return 500, {"detail": "stop"}, {}

    with _server(origin_reply) as origin, _server(_refuse) as proxy:
        _use_proxy(monkeypatch, _url(proxy))
        origin_url = _url(origin)
        with pytest.raises(SmokeTestFailure):
            run_constant_test(origin_url, "", "", "")
        with pytest.raises(SmokeTestFailure):
            run_constant_test(origin_url, "amw_dev_suppliedkey", "", "")

        assert proxy.hits == []
        paths = [hit["path"].split("?", 1)[0] for hit in origin.hits]
        assert "/v1/dev-keys/self-provision" in paths
        assert "/mcp/tools.json" in paths
        assert "/v1/billing/wallets" in paths
        tools = [hit for hit in origin.hits if hit["path"].startswith("/mcp/tools")]
        assert tools[0]["key"] == "amw_dev_originissued"
        wallets = [
            hit for hit in origin.hits if hit["path"].split("?", 1)[0] == "/v1/billing/wallets"
        ]
        assert wallets[0]["key"] == "amw_dev_suppliedkey"


def test_partner_bootstrap_keeps_bootstrap_key_off_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def origin_reply(handler: BaseHTTPRequestHandler):
        path = handler.path.split("?", 1)[0]
        if path == "/health":
            return 200, {"status": "ok"}, {}
        if path == "/v1/billing/wallets/sponsor":
            return 200, {"wallet_id": "spn-originwallet"}, {}
        if path == "/v1/billing/wallets/agent":
            return 200, {"wallet_id": "agt-originwallet"}, {}
        if path == "/v1/api-keys":
            return (
                200,
                {
                    "api_key": "b2a_syntheticissuedkey",
                    "key_id": "key-origin",
                    "key_prefix": "b2a_synt",
                },
                {},
            )
        return 500, {"detail": "unexpected"}, {}

    with _server(origin_reply) as origin, _server(_refuse) as proxy:
        _use_proxy(monkeypatch, _url(proxy))
        try:
            result = provision(
                api_url=_url(origin),
                bootstrap_key=BOOTSTRAP_CANARY,
                sponsor_name="QA",
                agent_id="qa-agent",
                budget_credits=1,
                initial_credits=1,
                key_name="qa",
                sponsor_wallet_id=None,
                agent_wallet_id=None,
            )
        except SystemExit as exc:
            pytest.fail(f"bootstrap left the origin: {exc}; proxy_hits={proxy.hits}")

        assert proxy.hits == []
        assert result["api_key"] == "b2a_syntheticissuedkey"
        assert {hit["key"] for hit in origin.hits} == {BOOTSTRAP_CANARY}


async def test_stress_req_disables_proxy_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import stress_test_live as stress

    seen: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(
            self,
            *,
            base_url: str,
            timeout: int,
            trust_env: bool,
            follow_redirects: bool,
        ) -> None:
            seen["base_url"] = base_url
            seen["trust_env"] = trust_env
            seen["follow_redirects"] = follow_redirects

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def request(self, method: str, path: str, headers: dict | None = None, **kwargs: object):
            seen["method"] = method
            seen["path"] = path
            seen["key"] = (headers or {}).get("X-API-Key")
            return httpx.Response(204)

    monkeypatch.setattr(stress.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(stress, "API_URL", "https://cli.example.test")
    monkeypatch.setattr(stress, "API_KEY", CANARY)

    response = await stress.req("GET", "/health")

    assert response.status_code == 204
    assert seen == {
        "base_url": "https://cli.example.test",
        "trust_env": False,
        "follow_redirects": False,
        "method": "GET",
        "path": "/health",
        "key": CANARY,
    }


def test_quickstart_health_ignores_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import quickstart

    def healthy(_handler: BaseHTTPRequestHandler):
        return 200, {"status": "ok"}, {}

    monkeypatch.setattr(quickstart, "HEALTH_DEADLINE_SECONDS", 1.0)
    process = subprocess.Popen(["sleep", "30"])
    try:
        with _server(healthy) as origin, _server(_refuse) as proxy:
            _use_proxy(monkeypatch, _url(proxy))
            port = origin.server_address[1]
            assert quickstart.wait_for_health(port, process) is True
            assert proxy.hits == []
            assert origin.hits[0]["path"].split("?", 1)[0] == "/health"
    finally:
        process.kill()
        process.wait(timeout=5)
