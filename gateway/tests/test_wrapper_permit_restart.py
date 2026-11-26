"""A restarted wrapper process replays its permit against the real application (#363).

The autogen, crewai and langchain wrappers take a ``permit_store``. These tests
drive each wrapper's actual tool body through the SDK transport into the real
gateway app, so every permit, receipt, ledger entry and signature asserted on
here was produced by the product. The tests supply only inputs (tool arguments,
client chosen idempotency keys) and one injected fault (a lost permit response).

A "restart" is a new tool, a new client and a new store object on the same file.

Optional frameworks are not needed: absent imports receive the same narrow
class boundary as ``test_wrapper_permit_retry_contract``. The wrapper packages
keep their real-framework CI suites for the client-side behaviour.
"""

from __future__ import annotations

import ast
import importlib
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from b2a_sdk.client import AgentMiddlewareClient
from tests.test_trust_helpers import provision_agent_wallet
from tests.test_wrapper_permit_retry_contract import _load_wrapper

TOOL = "wrapper-restart-echo"
FRAMEWORKS = ["autogen", "crewai", "langchain"]
CALL = dict(
    tool_name=TOOL,
    idempotency_key="restart-call-key",
    permit_idempotency_key="restart-permit-key",
    arguments={"text": "hi"},
)


class LosePermitResponse(httpx.AsyncBaseTransport):
    """Deliver ``POST /v1/permits`` to the app, then drop the response once."""

    def __init__(self) -> None:
        self._inner = ASGITransport(app=app)
        self.lost = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.path == "/v1/permits" and not self.lost:
            self.lost += 1
            await response.aread()
            await response.aclose()
            raise httpx.ReadTimeout("lost permit response", request=request)
        return response


class Wrapper:
    """One framework's wrapper modules, loaded from source with real clocks."""

    def __init__(self, monkeypatch, framework: str) -> None:
        self.framework = framework
        self.module = _load_wrapper(monkeypatch, framework)
        # The retry contract pins a past clock; the real app refuses an
        # expired permit, so these tests need the real one back.
        monkeypatch.setattr(self.module, "datetime", datetime)
        self.store_module = importlib.import_module(
            f"_retry_contract_{framework}._permit_store"
        )

    def store(self, path):
        return self.store_module.JsonFilePermitStore(path)

    async def run(self, api_key, wallet_id, *, store=None, transport=None, **terms):
        """One process: build the tool, make the call, close the client."""
        client = AgentMiddlewareClient(
            api_key,
            base_url="http://test",
            transport=transport or ASGITransport(app=app),
        )
        try:
            if self.framework == "langchain":
                tool = self.module.create_mcp_tool(
                    client, wallet_id=wallet_id, permit_store=store, **terms
                )
                return await tool.coroutine(**CALL)
            if self.framework == "autogen":
                tool = self.module.B2AFunctionTool(
                    api_key=api_key, wallet_id=wallet_id, permit_store=store, **terms
                )
                await tool.client.close()
                tool.client = client
                return await tool.call_mcp_tool(**CALL)
            tool = self.module.CrewAIB2ATool(
                api_key=api_key, wallet_id=wallet_id, permit_store=store, **terms
            )
            tool.client = client
            try:
                return await tool._arun("call_tool", **CALL)
            finally:
                tool.close()
        finally:
            await client.close()


def _failed(result: object) -> bool:
    return isinstance(result, str) and result.startswith("Error:")


async def _refusal(call) -> str:
    """The failure text of a call, whether the wrapper raises or returns it."""
    try:
        result = await call()
    except Exception as exc:  # noqa: BLE001 - the text is asserted by the caller
        return f"{type(exc).__name__}: {exc}"
    assert _failed(result), result
    return result


def _receipt_id(result: object) -> str:
    assert not _failed(result), result
    # crewai returns the result dict rendered as text.
    payload = ast.literal_eval(result) if isinstance(result, str) else result
    receipt_id = payload["receipt_id"]
    assert isinstance(receipt_id, str) and receipt_id
    return receipt_id


@pytest.fixture
def echo_calls():
    registry = get_service_registry()
    calls: list[str] = []

    def echo(text: str = "") -> dict[str, str]:
        calls.append(text)
        return {"text": text}

    registry.register_local(
        service_id=TOOL,
        name="Wrapper Restart Echo",
        description="Wrapper permit restart integration tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2,
        unit_name="call",
    )
    try:
        yield calls
    finally:
        registry.unregister_local(TOOL)


class Account:
    """A provisioned wallet plus the raw reads the assertions need."""

    def __init__(self, raw: AsyncClient, provisioned: dict) -> None:
        self.raw = raw
        self.headers = provisioned["agent_headers"]
        self.wallet_id = provisioned["agent_wallet_id"]
        self.api_key = self.headers["X-API-Key"]

    async def balance(self) -> Decimal:
        resp = await self.raw.get(
            f"/v1/billing/wallets/{self.wallet_id}", headers=self.headers
        )
        return Decimal(str(resp.json()["balance"]))

    async def debits(self) -> list[dict]:
        resp = await self.raw.get(
            f"/v1/billing/ledger/{self.wallet_id}", headers=self.headers
        )
        return [e for e in resp.json()["entries"] if TOOL in e["description"]]

    async def verified(self, receipt_id: str) -> bool:
        resp = await self.raw.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt_id},
            headers=self.headers,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["valid"] is True


@pytest.fixture
async def account(clean_database):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as raw:
        yield Account(raw, await provision_agent_wallet(raw))


@pytest.mark.anyio
@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_restart_with_a_store_replays_the_permit_and_the_receipt(
    monkeypatch, tmp_path, account, echo_calls, framework
) -> None:
    wrapper = Wrapper(monkeypatch, framework)
    path = tmp_path / "permits.json"
    before = await account.balance()

    first = _receipt_id(
        await wrapper.run(account.api_key, account.wallet_id, store=wrapper.store(path))
    )
    restarted = _receipt_id(
        await wrapper.run(account.api_key, account.wallet_id, store=wrapper.store(path))
    )

    assert restarted == first
    assert echo_calls == ["hi"], "the tool ran once across the restart"
    debits = await account.debits()
    assert len(debits) == 1
    assert before - await account.balance() == Decimal("2")
    assert await account.verified(first)


@pytest.mark.anyio
@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_restart_without_a_store_is_refused_by_the_trust_plane(
    monkeypatch, account, echo_calls, framework
) -> None:
    wrapper = Wrapper(monkeypatch, framework)
    _receipt_id(await wrapper.run(account.api_key, account.wallet_id))

    refused = await _refusal(lambda: wrapper.run(account.api_key, account.wallet_id))
    assert "idempotency_key_reused" in refused

    assert echo_calls == ["hi"]
    assert len(await account.debits()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_restart_after_a_lost_permit_response_uses_the_issued_permit(
    monkeypatch, tmp_path, account, echo_calls, framework
) -> None:
    wrapper = Wrapper(monkeypatch, framework)
    path = tmp_path / "permits.json"
    fault = LosePermitResponse()

    await _refusal(
        lambda: wrapper.run(
            account.api_key,
            account.wallet_id,
            store=wrapper.store(path),
            transport=fault,
        )
    )
    assert fault.lost == 1
    assert echo_calls == [], "no invoke happened before the crash"
    assert path.exists(), "the permit body was stored before the request"

    first = _receipt_id(
        await wrapper.run(account.api_key, account.wallet_id, store=wrapper.store(path))
    )
    again = _receipt_id(
        await wrapper.run(account.api_key, account.wallet_id, store=wrapper.store(path))
    )

    assert again == first
    assert echo_calls == ["hi"]
    assert len(await account.debits()) == 1
    assert await account.verified(first)


@pytest.mark.anyio
@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_restart_with_changed_terms_never_reaches_the_trust_plane(
    monkeypatch, tmp_path, account, echo_calls, framework
) -> None:
    wrapper = Wrapper(monkeypatch, framework)
    path = tmp_path / "permits.json"
    _receipt_id(
        await wrapper.run(account.api_key, account.wallet_id, store=wrapper.store(path))
    )
    before = await account.balance()

    refused = await _refusal(
        lambda: wrapper.run(
            account.api_key,
            account.wallet_id,
            store=wrapper.store(path),
            permit_budget=Decimal(50),
        )
    )
    assert "different permit terms" in refused

    assert echo_calls == ["hi"]
    assert await account.balance() == before
