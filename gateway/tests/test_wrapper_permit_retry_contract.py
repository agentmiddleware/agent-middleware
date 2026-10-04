"""Offline retry contract for the actual wrapper bodies and SDK transport.

Optional frameworks are not needed for these transport tests: absent imports
receive only their narrow class/factory boundary. This does not claim framework
runtime acceptance; the wrapper packages retain their real-framework CI suites.
"""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from b2a_sdk.client import AgentMiddlewareClient
from b2a_sdk.errors import TransportError

ROOT = Path(__file__).resolve().parents[1]


def _load_wrapper(monkeypatch, framework):
    try:
        available = importlib.util.find_spec(
            {"autogen": "autogen", "crewai": "crewai", "langchain": "langchain_core"}[
                framework
            ]
        )
    except (ImportError, ValueError):
        available = None
    if available is None:
        if framework == "autogen":
            names = (
                "autogen",
                "autogen.agentchat",
                "autogen.agentchat.conversable_agent",
            )
            for name in names:
                monkeypatch.setitem(sys.modules, name, ModuleType(name))
            sys.modules[names[-1]].ConversableAgent = type("ConversableAgent", (), {})
        elif framework == "crewai":

            class BaseTool(BaseModel):
                model_config = ConfigDict(arbitrary_types_allowed=True)

            for name in ("crewai", "crewai.tools"):
                monkeypatch.setitem(sys.modules, name, ModuleType(name))
            sys.modules["crewai.tools"].BaseTool = BaseTool
        else:

            class StructuredTool:
                @staticmethod
                def from_function(**kwargs):
                    return SimpleNamespace(**kwargs)

            for name in ("langchain_core", "langchain_core.tools"):
                monkeypatch.setitem(sys.modules, name, ModuleType(name))
            sys.modules["langchain_core.tools"].StructuredTool = StructuredTool

    # Isolated package names prevent replacing any application-imported wrapper.
    package_name = f"_retry_contract_{framework}"
    source = (
        ROOT.parent / f"integrations/{framework}-agent-middleware/src/{framework}_b2a"
    )
    package = ModuleType(package_name)
    package.__path__ = [str(source)]
    monkeypatch.setitem(sys.modules, package_name, package)
    for name, filename in (
        ("client", "client.py"),
        ("tool", "_tools.py" if framework == "langchain" else "tool.py"),
    ):
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.{name}", source / filename
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    tick = iter(range(100))
    monkeypatch.setattr(
        module,
        "datetime",
        SimpleNamespace(
            now=lambda tz: (
                datetime(2026, 10, 2, tzinfo=timezone.utc)
                + timedelta(seconds=next(tick))
            )
        ),
    )
    return module


class Gateway:
    """An accepted permit loses its first response; full-body replay is required."""

    def __init__(self, lose_first=True):
        self.lose_first = lose_first
        self.permit_bodies = []
        self.invoke_keys = []
        self.charges = set()

    async def __call__(self, request):
        body = json.loads(request.content)
        if request.url.path == "/v1/permits":
            self.permit_bodies.append(
                (request.headers["Idempotency-Key"], request.content)
            )
            if self.permit_bodies[-1] != self.permit_bodies[0]:
                return httpx.Response(409, json={"detail": "idempotency_key_reused"})
            first = len(self.permit_bodies) == 1
            await asyncio.sleep(
                0
            )  # let a concurrent caller enter while acknowledgement is pending
            if first and self.lose_first:
                raise httpx.ReadTimeout(
                    "synthetic lost permit response", request=request
                )
            return httpx.Response(
                201,
                json={
                    **body,
                    "permit_id": "permit-original",
                    "subject_key_id": None,
                    "spent_credits": "0",
                    "nonce": "nonce-local",
                    "status": "active",
                    "signature": "synthetic",
                    "key_id": "key-local",
                    "issued_at": "2026-10-02T00:00:00+00:00",
                    "revoked_at": None,
                },
            )
        assert request.url.path == "/mcp/messages"
        key = request.headers["Idempotency-Key"]
        self.invoke_keys.append(key)
        self.charges.add(key)
        assert body["params"]["mcpContext"]["permit_id"] == "permit-original"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": "ok"}],
                    "structuredContent": {},
                    "receipt": {
                        "receipt_id": "receipt-original",
                        "permit_id": "permit-original",
                        "wallet_id": "wallet-local",
                        "key_id": "key-local",
                        "tool": "partner.search",
                        "request_hash": "request-hash",
                        "response_hash": "response-hash",
                        "ledger_entry_id": "ledger-local",
                        "credits_authorized": "1",
                        "credits_charged": "1",
                        "outcome": "success",
                        "audit_event_id": "audit-local",
                        "created_at": "2026-10-02T00:00:00+00:00",
                        "signature": "synthetic",
                        "signature_key_id": "key-local",
                    },
                },
            },
        )


def _tool(module, framework, client):
    if framework == "langchain":
        tool = module.create_mcp_tool(client, wallet_id="wallet-local")
        return tool, tool.coroutine
    if framework == "autogen":
        tool = module.B2AFunctionTool(api_key="synthetic", wallet_id="wallet-local")
        original = tool.client
        tool.client = client
        return tool, tool.call_mcp_tool, original
    tool = module.CrewAIB2ATool(api_key="synthetic", wallet_id="wallet-local")
    tool.client = client

    async def call(**kwargs):
        value = await tool._arun("call_tool", **kwargs)
        if value.startswith("Error:"):
            raise ValueError(value)
        return value

    return tool, call


async def _prepare(monkeypatch, framework, client):
    prepared = _tool(_load_wrapper(monkeypatch, framework), framework, client)
    if len(prepared) == 3:
        await prepared[2].close()
    return prepared[:2]


CALL = dict(
    tool_name="partner.search",
    idempotency_key="logical-action",
    permit_idempotency_key="permit-key",
    arguments={},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ["autogen", "crewai", "langchain"])
async def test_lost_permit_ack_replays_original_body_and_original_invoke(
    monkeypatch, framework
):
    gateway = Gateway()
    async with AgentMiddlewareClient(
        "synthetic",
        base_url="http://local.invalid",
        transport=httpx.MockTransport(gateway),
    ) as client:
        _, call = await _prepare(monkeypatch, framework, client)
        with pytest.raises((TransportError, ValueError)):
            await call(**CALL)
        first = await call(**CALL)
        replay = await call(**CALL)
    assert first == replay
    assert len(gateway.permit_bodies) == 2
    assert gateway.permit_bodies[0] == gateway.permit_bodies[1]
    assert gateway.invoke_keys == ["logical-action", "logical-action"]
    assert len(gateway.charges) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ["autogen", "crewai", "langchain"])
async def test_concurrent_same_key_creation_uses_one_body(monkeypatch, framework):
    gateway = Gateway(lose_first=False)
    async with AgentMiddlewareClient(
        "synthetic",
        base_url="http://local.invalid",
        transport=httpx.MockTransport(gateway),
    ) as client:
        _, call = await _prepare(monkeypatch, framework, client)
        first, second = await asyncio.gather(call(**CALL), call(**CALL))
    assert first == second
    assert len(gateway.permit_bodies) == 2
    assert gateway.permit_bodies[0] == gateway.permit_bodies[1]
    assert len(gateway.charges) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ["autogen", "crewai", "langchain"])
@pytest.mark.parametrize("lose_first", [True, False])
async def test_changed_tool_under_same_permit_key_is_refused_before_transport(
    monkeypatch, framework, lose_first
):
    gateway = Gateway(lose_first=lose_first)
    async with AgentMiddlewareClient(
        "synthetic",
        base_url="http://local.invalid",
        transport=httpx.MockTransport(gateway),
    ) as client:
        _, call = await _prepare(monkeypatch, framework, client)
        if lose_first:
            with pytest.raises((TransportError, ValueError)):
                await call(**CALL)
        else:
            await call(**CALL)
        before = (len(gateway.permit_bodies), len(gateway.invoke_keys))
        with pytest.raises(ValueError, match="different permit terms"):
            await call(**{**CALL, "tool_name": "another.tool"})
        assert (len(gateway.permit_bodies), len(gateway.invoke_keys)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", ["autogen", "crewai"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("wallet_id", "other-wallet"), ("permit_budget", Decimal("200"))],
)
async def test_mutated_wallet_or_budget_does_not_reuse_cached_authority(
    monkeypatch, framework, field, value
):
    gateway = Gateway(lose_first=False)
    async with AgentMiddlewareClient(
        "synthetic",
        base_url="http://local.invalid",
        transport=httpx.MockTransport(gateway),
    ) as client:
        tool, call = await _prepare(monkeypatch, framework, client)
        await call(**CALL)
        setattr(tool, field, value)
        with pytest.raises(ValueError, match="different permit terms"):
            await call(**CALL)
    assert len(gateway.permit_bodies) == 1
    assert len(gateway.invoke_keys) == 1


def test_crewai_sync_entry_point_reuses_body_after_lost_ack(monkeypatch):
    module = _load_wrapper(monkeypatch, "crewai")
    gateway = Gateway()
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    client = AgentMiddlewareClient(
        "synthetic",
        base_url="http://local.invalid",
        transport=httpx.MockTransport(gateway),
    )
    tool = module.CrewAIB2ATool(api_key="synthetic", wallet_id="wallet-local")
    tool.client = client
    try:
        assert tool._run("call_tool", **CALL).startswith("Error:")
        assert "receipt-original" in tool._run("call_tool", **CALL)
        assert gateway.permit_bodies[0] == gateway.permit_bodies[1]
    finally:
        loop.run_until_complete(client.close())
        loop.close()
