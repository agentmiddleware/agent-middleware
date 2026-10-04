"""``POST /mcp/messages`` parses strictly, so nothing can charge and then fail.

The envelope and member-shape checks (non-object body, non-string method,
array params, malformed ``mcpContext``) landed with the replay-key contract
and are pinned in ``tests/test_mcp_idempotency_key_validation.py``. This
module pins what the plain ``request.json()`` parse still let through:

* a non-UTF-8 body raised ``UnicodeDecodeError`` past the ``JSONDecodeError``
  handler and surfaced as HTTP 500;
* JSON nested ~1000 levels deep raised ``RecursionError`` on Python 3.11
  (the standard endpoint already pre-screened raw bytes; the legacy route did
  not);
* a non-finite number (``NaN``, ``Infinity``, ``1e400``) or a JSON
  ``"\\ud800"`` escape (a lone surrogate) parsed fine, the governed tool ran,
  the wallet was charged, and *then* the response serializer refused to
  encode the echoed ``id`` or argument — HTTP 500 for a call that happened,
  and a keyed retry that 500s again on the replay;
* an envelope that *stated* a ``jsonrpc`` version other than ``"2.0"`` reached
  dispatch (a missing member is still accepted: the version-less legacy
  envelopes pinned in ``tests/test_mcp_idempotency_key_validation.py`` and
  ``test_version_less_envelope_is_still_accepted`` below depend on it).

Every case here is sent with a valid permit in hand and asserts zero tool
executions and zero ledger debits, because "refused" only counts if it is
refused before any effect.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

TOOL = "legacy-envelope-counted"
JSON_HEADERS = {"Content-Type": "application/json"}


@pytest.fixture
async def client():
    # raise_app_exceptions=False: a regression must come back as the HTTP 500
    # it would be in production, so the assertion names the status.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def counted_tool():
    calls: list[dict[str, Any]] = []

    def _run(text: str = "ok") -> dict[str, Any]:
        calls.append({"text": text})
        return {"text": text}

    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Legacy Envelope Counted Tool",
        description="Counts executions so tests can prove nothing ran",
        category=ServiceCategory.AGENT_COMMS,
        func=_run,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        yield calls
    finally:
        registry.unregister_local(TOOL)


async def _governed_agent(client: AsyncClient) -> dict[str, Any]:
    """An agent wallet plus a signed permit for TOOL: the metered, keyed path."""
    agent = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=agent["agent_wallet_id"],
        key_id=agent["key_id"],
        tool_name=TOOL,
        max_credits=10,
    )
    return {**agent, "permit_id": permit["permit_id"]}


def _params(
    agent: dict[str, Any], *, idempotency_key: str, text: str = "hi"
) -> dict[str, Any]:
    return {
        "name": TOOL,
        "arguments": {"text": text},
        "mcpContext": {
            "wallet_id": agent["agent_wallet_id"],
            "permit_id": agent["permit_id"],
            "idempotency_key": idempotency_key,
        },
    }


async def _debits(client: AsyncClient, agent: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = await client.get(
        f"/v1/billing/ledger/{agent['agent_wallet_id']}", headers=agent["agent_headers"]
    )
    assert ledger.status_code == 200
    return [e for e in ledger.json()["entries"] if TOOL in e["description"]]


# ── body-level refusals: HTTP 400, nothing parsed into the pipeline ──────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","x":"\xff"}',
        b"\xff\xfe",
    ],
    ids=["invalid-utf8-in-string", "invalid-utf8-body"],
)
async def test_non_utf8_body_is_bad_request_not_500(client, raw):
    resp = await client.post(
        "/mcp/messages", content=raw, headers={**BOOTSTRAP_HEADERS, **JSON_HEADERS}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Invalid JSON"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        b"[" * 100_000 + b"]" * 100_000,
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"category":'
        + b"[" * 100_000
        + b"]" * 100_000
        + b"}}",
    ],
    ids=["bare", "inside-params"],
)
async def test_deeply_nested_body_is_bad_request_not_500(client, raw):
    resp = await client.post(
        "/mcp/messages", content=raw, headers={**BOOTSTRAP_HEADERS, **JSON_HEADERS}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"].startswith("Invalid JSON")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "template",
    [
        '{"jsonrpc":"2.0","id":NaN,"method":"tools/call","params":%s}',
        '{"jsonrpc":"2.0","id":Infinity,"method":"tools/call","params":%s}',
        '{"jsonrpc":"2.0","id":-Infinity,"method":"tools/call","params":%s}',
        '{"jsonrpc":"2.0","id":1e400,"method":"tools/call","params":%s}',
    ],
    ids=["NaN-id", "Infinity-id", "-Infinity-id", "overflow-id"],
)
async def test_non_finite_id_is_refused_before_execution(
    client, clean_database, counted_tool, template
):
    """The worst case: the tool ran and the wallet was charged, then echoing a
    non-finite ``id`` failed response serialization (HTTP 500), and a retry
    with the same key 500'd again on the replay. Non-finite numbers are not
    RFC 8259 JSON and are now a parse error."""
    agent = await _governed_agent(client)
    params = json.dumps(_params(agent, idempotency_key="nonfinite-1"))
    for _ in (1, 2):
        resp = await client.post(
            "/mcp/messages",
            content=(template % params).encode(),
            headers={**agent["agent_headers"], **JSON_HEADERS},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Invalid JSON"
    assert counted_tool == []
    assert await _debits(client, agent) == []


@pytest.mark.anyio
async def test_non_finite_argument_is_refused_before_execution(
    client, clean_database, counted_tool
):
    """Same class inside ``arguments``: an echoed NaN would fail after the charge."""
    agent = await _governed_agent(client)
    params = json.dumps(
        _params(agent, idempotency_key="nonfinite-arg-1", text="__NAN__")
    )
    raw = (
        '{"jsonrpc":"2.0","id":"x","method":"tools/call","params":%s}' % params
    ).replace('"__NAN__"', "NaN")
    resp = await client.post(
        "/mcp/messages",
        content=raw.encode(),
        headers={**agent["agent_headers"], **JSON_HEADERS},
    )
    assert resp.status_code == 400, resp.text
    assert counted_tool == []
    assert await _debits(client, agent) == []


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["id", "argument"])
async def test_unpaired_surrogate_anywhere_is_refused_before_execution(
    client, clean_database, counted_tool, where
):
    """A JSON "\\ud800" escape is valid JSON, but the lone surrogate it decodes
    to cannot be utf-8 encoded by the response serializer. In an ``id`` or an
    echoed argument that failed the reply after the charge."""
    agent = await _governed_agent(client)
    text = "\ud800" if where == "argument" else "hi"
    request_id = "\ud800" if where == "id" else "x"
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": _params(agent, idempotency_key=f"surrogate-{where}", text=text),
    }
    # ensure_ascii (the default) keeps the \ud800 escape: the wire form a real
    # client sends. httpx's own json= path cannot encode a lone surrogate.
    raw = json.dumps(body).encode("ascii")
    for _ in (1, 2):
        resp = await client.post(
            "/mcp/messages",
            content=raw,
            headers={**agent["agent_headers"], **JSON_HEADERS},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "Invalid JSON"
    assert counted_tool == []
    assert await _debits(client, agent) == []


# ── envelope: the version member ─────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "1.0", "id": 7, "method": "tools/list", "params": {}},
        {"jsonrpc": 2.0, "id": 7, "method": "tools/list", "params": {}},
        {"jsonrpc": None, "id": 7, "method": "tools/list", "params": {}},
    ],
    ids=["jsonrpc-1.0", "jsonrpc-number", "jsonrpc-null"],
)
async def test_stated_jsonrpc_version_other_than_2_0_is_invalid_request(client, body):
    resp = await client.post(
        "/mcp/messages", json=body, headers={**BOOTSTRAP_HEADERS, **JSON_HEADERS}
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["id"] == 7
    assert payload["error"]["code"] == -32600
    assert payload["error"]["message"] == 'Invalid Request: jsonrpc must be "2.0"'


@pytest.mark.anyio
async def test_version_less_envelope_is_still_accepted(client):
    """This route has always accepted envelopes without a ``jsonrpc`` member and
    its existing contract tests send them; only a stated wrong version is refused."""
    body = {"id": 7, "method": "tools/list", "params": {}}
    resp = await client.post(
        "/mcp/messages", json=body, headers={**BOOTSTRAP_HEADERS, **JSON_HEADERS}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == 7
    assert "result" in resp.json()


@pytest.mark.anyio
async def test_jsonrpc_1_0_tools_call_never_reaches_dispatch(
    client, clean_database, counted_tool
):
    agent = await _governed_agent(client)
    body = {
        "jsonrpc": "1.0",
        "id": "v1",
        "method": "tools/call",
        "params": _params(agent, idempotency_key="v1-envelope"),
    }
    resp = await client.post("/mcp/messages", json=body, headers=agent["agent_headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == -32600
    assert counted_tool == []
    assert await _debits(client, agent) == []


# ── controls ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_finite_float_id_is_still_echoed(client):
    """Only non-finite numbers are refused; an ordinary numeric id round-trips."""
    body = {"jsonrpc": "2.0", "id": 2.5, "method": "tools/list", "params": {}}
    resp = await client.post(
        "/mcp/messages", json=body, headers={**BOOTSTRAP_HEADERS, **JSON_HEADERS}
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 2.5
    assert "result" in resp.json()


@pytest.mark.anyio
async def test_well_formed_governed_call_still_executes_once_and_replays(
    client, clean_database, counted_tool
):
    """The strict parser must not disturb a conforming call."""
    agent = await _governed_agent(client)
    body = {
        "jsonrpc": "2.0",
        "id": "ok-1",
        "method": "tools/call",
        "params": _params(agent, idempotency_key="well-formed-1", text="héllo ✓"),
    }
    first = await client.post(
        "/mcp/messages", json=body, headers=agent["agent_headers"]
    )
    second = await client.post(
        "/mcp/messages", json=body, headers=agent["agent_headers"]
    )
    assert first.status_code == 200 and second.status_code == 200, (
        first.text,
        second.text,
    )
    assert (
        first.json()["result"]["receipt"]["receipt_id"]
        == second.json()["result"]["receipt"]["receipt_id"]
    )
    assert counted_tool == [{"text": "héllo ✓"}]
    assert len(await _debits(client, agent)) == 1
