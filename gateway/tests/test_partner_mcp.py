"""Security and lifecycle tests for the isolated design-partner MCP server."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

_TOKEN = "pilot-test-token-0123456789-ABCDEFGH"
_HOST = "partner.example"
_MODULE_NAME = "app.partner_mcp"


@pytest.fixture
def partner_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("PARTNER_MCP_BEARER_TOKEN", _TOKEN)
    monkeypatch.setenv("PARTNER_MCP_ALLOWED_HOSTS", _HOST)
    sys.modules.pop(_MODULE_NAME, None)
    module = importlib.import_module(_MODULE_NAME)
    try:
        yield module
    finally:
        sys.modules.pop(_MODULE_NAME, None)


def test_configuration_fails_closed_when_required_values_are_missing(
    partner_module: ModuleType,
) -> None:
    with pytest.raises(
        partner_module.PartnerMcpConfigurationError,
        match="PARTNER_MCP_BEARER_TOKEN is required",
    ):
        partner_module.create_partner_mcp_app({})

    with pytest.raises(
        partner_module.PartnerMcpConfigurationError,
        match="PARTNER_MCP_ALLOWED_HOSTS is required",
    ):
        partner_module.create_partner_mcp_app({"PARTNER_MCP_BEARER_TOKEN": _TOKEN})


@pytest.mark.parametrize(
    "token",
    [
        "too-short",
        "a" * 32,
        "contains spaces despite sufficient length 1234567890",
    ],
)
def test_configuration_rejects_weak_bearer_tokens(
    partner_module: ModuleType,
    token: str,
) -> None:
    with pytest.raises(partner_module.PartnerMcpConfigurationError):
        partner_module.create_partner_mcp_app(
            {
                "PARTNER_MCP_BEARER_TOKEN": token,
                "PARTNER_MCP_ALLOWED_HOSTS": _HOST,
            }
        )


@pytest.mark.parametrize(
    "invalid_host",
    ["*.example.com", "example.com:", "[::1]:"],
)
def test_configuration_rejects_malformed_hosts(
    partner_module: ModuleType,
    invalid_host: str,
) -> None:
    with pytest.raises(partner_module.PartnerMcpConfigurationError):
        partner_module.create_partner_mcp_app(
            {
                "PARTNER_MCP_BEARER_TOKEN": _TOKEN,
                "PARTNER_MCP_ALLOWED_HOSTS": invalid_host,
            }
        )


def test_configuration_masks_secret_repr(
    partner_module: ModuleType,
) -> None:
    configuration = partner_module.PartnerMcpConfiguration.from_environment(
        {
            "PARTNER_MCP_BEARER_TOKEN": _TOKEN,
            "PARTNER_MCP_ALLOWED_HOSTS": _HOST,
        }
    )
    assert _TOKEN not in repr(configuration)


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("authorization", [None, "Bearer definitely-wrong-token"])
async def test_every_mcp_transport_method_requires_the_bearer_token(
    partner_module: ModuleType,
    method: str,
    authorization: str | None,
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    transport = httpx.ASGITransport(app=partner_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"http://{_HOST}",
    ) as client:
        response = await client.request(method, "/mcp", headers=headers, json={})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "unauthorized"}


@pytest.mark.anyio
async def test_mcp_rejects_an_unconfigured_host_after_valid_auth(
    partner_module: ModuleType,
) -> None:
    transport = httpx.ASGITransport(app=partner_module.app)
    async with partner_module.app.router.lifespan_context(partner_module.app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://attacker.example",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        ) as client:
            response = await client.post("/mcp", json={})

    assert response.status_code == 421
    assert response.text == "Invalid Host header"


@pytest.mark.anyio
async def test_health_is_public_and_does_not_expose_configuration(
    partner_module: ModuleType,
) -> None:
    transport = httpx.ASGITransport(app=partner_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://health-probe.invalid",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "partner-mcp"}
    assert _TOKEN not in response.text


@pytest.mark.anyio
async def test_partner_echo_returns_only_echo_and_forwarded_invocation_metadata(
    partner_module: ModuleType,
) -> None:
    transport = httpx.ASGITransport(app=partner_module.app)
    async with partner_module.app.router.lifespan_context(partner_module.app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=f"http://{_HOST}",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        ) as http_client:
            async with streamable_http_client(
                f"http://{_HOST}/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool(
                        "partner.echo",
                        {"message": "hello partner"},
                        meta={
                            "io.agentmiddleware/invocation_id": "inv-live-123",
                            "io.agentmiddleware/idempotency_key": "idem-live-123",
                            "ignored": "not reflected",
                        },
                    )

    assert [tool.name for tool in tools.tools] == ["partner.echo"]
    assert result.isError is False
    assert result.structuredContent == {
        "echo": "hello partner",
        "invocation_id": "inv-live-123",
        "idempotency_key": "idem-live-123",
    }
    assert _TOKEN not in repr(result)


@pytest.mark.parametrize(
    ("app_module", "expected"),
    [
        (None, "app.main:app"),
        ("app.partner_mcp:app", "app.partner_mcp:app"),
    ],
)
def test_docker_entrypoint_defaults_to_main_and_allows_explicit_app_module(
    tmp_path: Path,
    app_module: str | None,
    expected: str,
) -> None:
    capture_path = tmp_path / "uvicorn-arguments.txt"
    fake_uvicorn = tmp_path / "uvicorn"
    fake_uvicorn.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE_PATH"\n',
        encoding="utf-8",
    )
    fake_uvicorn.chmod(0o700)

    environment: dict[str, Any] = os.environ.copy()
    environment.update(
        {
            "CAPTURE_PATH": str(capture_path),
            "PATH": f"{tmp_path}{os.pathsep}{environment['PATH']}",
            "PORT": "9123",
            "RUN_MIGRATIONS_ON_START": "false",
        }
    )
    if app_module is None:
        environment.pop("APP_MODULE", None)
    else:
        environment["APP_MODULE"] = app_module

    result = subprocess.run(
        ["sh", "scripts/docker_entrypoint.sh"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        expected,
        "--host",
        "0.0.0.0",
        "--port",
        "9123",
    ]
