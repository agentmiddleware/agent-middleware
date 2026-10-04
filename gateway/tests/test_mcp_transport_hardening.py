"""Transport hardening on top of the replay-key and envelope validation.

Each case here is a gap an adversarial review found after the main fix
landed, reproduced against it before being closed:

* JSON that Python's parser accepts but the response path cannot render
  (``NaN``, ``Infinity``, lone-surrogate escapes) executed, debited, and then
  failed with a 500 on both transports — or, for a non-finite id on ``/mcp``,
  was silently dropped as a notification.
* A body nested past the parser's recursion limit, or one that was not UTF-8,
  was a 500 on ``/mcp/messages``.
* ``POST /mcp`` silently ran a call un-keyed when the caller sent its key in
  the legacy ``params.mcpContext`` shape.
* A UTF-8 ``Idempotency-Key`` header was read as latin-1 mojibake, so it
  conflicted with the identical key in the body and could trip the
  control-character check on its continuation bytes. The header is now read
  as UTF-8 and nothing else: bytes that are not UTF-8 are refused, so two
  different wire values can never alias to one key.
* A ``tools/call`` whose params contained a key named ``params`` was unwrapped
  as if it were an envelope and misrouted.
* The REST invoke's legacy shape raised a pydantic error inside the handler
  for a non-string ``arguments.wallet_id``.

Every refusal is proven by counting effects and ledger debits.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from app.trust import validate_tools_call_params
from tests.test_trust_helpers import (
    BOOTSTRAP_HEADERS,
    create_tool_permit,
    provision_agent_wallet,
)

META_KEY = "io.agentmiddleware/idempotency_key"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
JSON_HEADERS = {**BOOTSTRAP_HEADERS, "Content-Type": "application/json"}
TOOL = "hardening.effect"
# The euro sign's UTF-8 bytes include 0x82, a C1 control in latin-1.
UTF8_KEY = "€-clé-1"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def effects():
    calls: list[str] = []

    def effect(text: str = "one") -> dict[str, str]:
        calls.append(text)
        return {"text": text}

    registry = get_service_registry()
    registry.register_local(
        service_id=TOOL,
        name="Hardening effect",
        description="Counts executions for the transport hardening tests",
        category=ServiceCategory.AGENT_COMMS,
        func=effect,
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        yield calls
    finally:
        registry.unregister_local(TOOL)


async def _debits(client: AsyncClient, provisioned: dict) -> int:
    ledger = await client.get(
        f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    assert ledger.status_code == 200, ledger.text
    return len([e for e in ledger.json()["entries"] if TOOL in e["description"]])


def _receipt_id(response) -> str:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "result" in payload, payload
    return payload["result"]["receipt"]["receipt_id"]


# --------------------------------------------------------------------------- #
# POST /mcp (standard endpoint)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def caller(client, standard_mcp_enabled, clean_database, effects):
    provisioned = await provision_agent_wallet(client)
    return {
        "provisioned": provisioned,
        "headers": {**provisioned["agent_headers"], **MCP_HEADERS},
        "effects": effects,
    }


def _standard_raw(raw_id: str, meta_key: str = '"k-1"') -> bytes:
    return (
        '{"jsonrpc":"2.0","id":%s,"method":"tools/call","params":{"name":"%s",'
        '"arguments":{"text":"one"},"_meta":{"%s":%s}}}'
        % (raw_id, TOOL, META_KEY, meta_key)
    ).encode("utf-8")


def _standard_call(*, meta: Any = None, context: Any = None) -> dict:
    params: dict[str, Any] = {"name": TOOL, "arguments": {"text": "one"}}
    if meta is not None:
        params["_meta"] = {META_KEY: meta}
    if context is not None:
        params["mcpContext"] = context
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw_id",
    [
        pytest.param('"\\ud800"', id="lone-surrogate-id"),
        pytest.param("NaN", id="nan-id"),
        pytest.param("1e999", id="overflow-id"),
    ],
)
async def test_standard_unrenderable_json_is_refused_with_no_effect(
    client, caller, raw_id
):
    # Valid syntax the reply could not carry is an invalid request (-32600),
    # HTTP 400, with a null id: the sent id is the very thing that cannot be
    # echoed. Syntax errors stay the SDK's own -32700.
    response = await client.post(
        "/mcp", content=_standard_raw(raw_id), headers=caller["headers"]
    )
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["id"] is None
    assert payload["error"]["code"] == -32600
    assert caller["effects"] == []
    assert await _debits(client, caller["provisioned"]) == 0


@pytest.mark.anyio
async def test_standard_legacy_context_key_is_honored_and_replays(client, caller):
    body = _standard_call(context={"wallet_id": "ignored", "idempotency_key": "ctx-1"})
    first = _receipt_id(await client.post("/mcp", json=body, headers=caller["headers"]))
    second = _receipt_id(
        await client.post("/mcp", json=body, headers=caller["headers"])
    )
    assert first == second
    assert caller["effects"] == ["one"]
    assert await _debits(client, caller["provisioned"]) == 1


@pytest.mark.anyio
async def test_standard_legacy_context_key_conflicting_with_meta_is_refused(
    client, caller
):
    body = _standard_call(meta="meta-1", context={"idempotency_key": "ctx-1"})
    response = await client.post("/mcp", json=body, headers=caller["headers"])
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["reason_code"] == "idempotency_key_conflict"
    assert "params.mcpContext.idempotency_key" in error["data"]["source"]
    assert caller["effects"] == []
    assert await _debits(client, caller["provisioned"]) == 0


@pytest.mark.anyio
async def test_standard_malformed_legacy_context_key_is_refused(client, caller):
    body = _standard_call(context={"idempotency_key": 123})
    response = await client.post("/mcp", json=body, headers=caller["headers"])
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["reason_code"] == "idempotency_key_not_a_string"
    assert error["data"]["source"] == "params.mcpContext.idempotency_key"
    assert caller["effects"] == []
    assert await _debits(client, caller["provisioned"]) == 0


@pytest.mark.anyio
async def test_standard_non_object_legacy_context_is_invalid_params(client, caller):
    body = _standard_call(context=["not", "an", "object"])
    response = await client.post("/mcp", json=body, headers=caller["headers"])
    assert response.json()["error"]["code"] == -32602, response.text
    assert caller["effects"] == []
    assert await _debits(client, caller["provisioned"]) == 0


@pytest.mark.anyio
async def test_standard_utf8_header_key_agrees_with_the_same_meta_key(client, caller):
    # On the wire the header is UTF-8 bytes; httpx refuses to encode a
    # non-ASCII str itself, so send exactly what a client sends.
    headers = {**caller["headers"], "Idempotency-Key": UTF8_KEY.encode("utf-8")}
    first = _receipt_id(
        await client.post("/mcp", json=_standard_call(meta=UTF8_KEY), headers=headers)
    )
    header_only = _receipt_id(
        await client.post("/mcp", json=_standard_call(), headers=headers)
    )
    meta_only = _receipt_id(
        await client.post(
            "/mcp", json=_standard_call(meta=UTF8_KEY), headers=caller["headers"]
        )
    )
    assert first == header_only == meta_only
    assert caller["effects"] == ["one"]


# --------------------------------------------------------------------------- #
# POST /mcp/messages (legacy transport) and the REST invoke
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_standard_non_utf8_header_key_is_refused_not_aliased(client, caller):
    # b"\xe9" is "é" in latin-1 and is not UTF-8. Read as latin-1 it would be
    # the same stored key as the UTF-8 "é" (b"\xc3\xa9"), so two different
    # wire values would alias; the header has exactly one reading instead.
    latin1 = {**caller["headers"], "Idempotency-Key": b"\xe9-1"}
    response = await client.post("/mcp", json=_standard_call(), headers=latin1)
    error = response.json()["error"]
    assert error["code"] == -32602, response.text
    assert error["data"]["reason_code"] == "idempotency_key_not_utf8"
    assert error["data"]["source"] == "Idempotency-Key header"
    assert caller["effects"] == []
    assert await _debits(client, caller["provisioned"]) == 0

    utf8 = {**caller["headers"], "Idempotency-Key": "é-1".encode("utf-8")}
    first = _receipt_id(await client.post("/mcp", json=_standard_call(), headers=utf8))
    replay = _receipt_id(await client.post("/mcp", json=_standard_call(), headers=utf8))
    assert first == replay
    assert caller["effects"] == ["one"]
    assert await _debits(client, caller["provisioned"]) == 1


def test_adapter_recognises_an_envelope_by_its_protocol_members():
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "name": "not-the-tool",
        "params": {"name": TOOL, "arguments": {"text": "one"}},
    }
    unwrapped = validate_tools_call_params(envelope)
    assert unwrapped["name"] == TOOL
    assert unwrapped["arguments"] == {"text": "one"}

    bare_envelope = {"params": {"name": TOOL, "arguments": {"text": "one"}}}
    assert validate_tools_call_params(bare_envelope)["name"] == TOOL

    params_with_a_params_key = {"name": TOOL, "arguments": {}, "params": {"name": "x"}}
    assert validate_tools_call_params(params_with_a_params_key)["name"] == TOOL


@pytest.fixture
async def governed(client, clean_database, effects):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=TOOL,
        idem_key="hardening-permit-1",
    )
    return {
        "provisioned": provisioned,
        "permit": permit,
        "headers": {**provisioned["agent_headers"], "Content-Type": "application/json"},
        "effects": effects,
    }


def _legacy_call(
    governed: dict, *, key: str | None = "legacy-1", **overrides: Any
) -> dict:
    context: dict[str, Any] = {
        "wallet_id": governed["provisioned"]["agent_wallet_id"],
        "permit_id": governed["permit"]["permit_id"],
    }
    if key is not None:
        context["idempotency_key"] = key
    params: dict[str, Any] = {
        "name": TOOL,
        "arguments": {"text": "one"},
        "mcpContext": context,
    }
    params.update(overrides)
    return {
        "jsonrpc": "2.0",
        "id": "legacy-1",
        "method": "tools/call",
        "params": params,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw_id",
    [
        pytest.param('"\\ud800"', id="lone-surrogate-id"),
        pytest.param("NaN", id="nan-id"),
        pytest.param("Infinity", id="infinity-id"),
        pytest.param("1e999", id="overflow-id"),
    ],
)
async def test_legacy_unrenderable_json_is_invalid_json_with_no_effect(
    client, governed, raw_id
):
    body = _legacy_call(governed)
    raw = json.dumps(body).replace('"legacy-1"', raw_id, 1).encode("utf-8")
    response = await client.post(
        "/mcp/messages", content=raw, headers=governed["headers"]
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Invalid JSON"
    assert governed["effects"] == []
    assert await _debits(client, governed["provisioned"]) == 0


@pytest.mark.anyio
async def test_legacy_deeply_nested_body_is_invalid_json_not_a_500(client):
    raw = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        + b"[" * 5000
        + b"]" * 5000
        + b"}"
    )
    response = await client.post("/mcp/messages", content=raw, headers=JSON_HEADERS)
    # The legacy route answers every unparseable body as HTTP 400 with a
    # ``detail`` string; a body it refuses to parse at all has no trustworthy
    # id to echo, so it is not a JSON-RPC error envelope.
    assert response.status_code == 400, response.text
    assert (
        response.json()["detail"]
        == "Invalid JSON: nesting depth exceeds the supported limit"
    )


@pytest.mark.anyio
async def test_legacy_non_utf8_body_is_invalid_json_not_a_500(client):
    response = await client.post(
        "/mcp/messages", content=b"\xff\xfe\x00", headers=JSON_HEADERS
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Invalid JSON"


@pytest.mark.anyio
async def test_legacy_params_key_inside_params_is_not_mistaken_for_the_envelope(
    client, governed
):
    body = _legacy_call(governed, params={"name": "some-other-tool", "arguments": {}})
    response = await client.post(
        "/mcp/messages", json=body, headers=governed["headers"]
    )
    assert response.status_code == 200, response.text
    assert "result" in response.json(), response.text
    assert governed["effects"] == ["one"]


@pytest.mark.anyio
async def test_legacy_utf8_header_key_matches_the_same_context_key(client, governed):
    headers = {**governed["headers"], "Idempotency-Key": UTF8_KEY.encode("utf-8")}
    first = await client.post(
        "/mcp/messages", json=_legacy_call(governed, key=UTF8_KEY), headers=headers
    )
    replay = await client.post(
        "/mcp/messages", json=_legacy_call(governed, key=None), headers=headers
    )
    assert _receipt_id(first) == _receipt_id(replay)
    assert governed["effects"] == ["one"]


@pytest.mark.anyio
async def test_legacy_non_utf8_header_key_is_refused_before_any_effect(
    client, governed
):
    headers = {**governed["headers"], "Idempotency-Key": b"\xe9-1"}
    response = await client.post(
        "/mcp/messages", json=_legacy_call(governed, key=None), headers=headers
    )
    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["code"] == -32602
    assert error["data"]["reason_code"] == "idempotency_key_not_utf8"
    assert governed["effects"] == []
    assert await _debits(client, governed["provisioned"]) == 0


@pytest.mark.anyio
async def test_rest_invoke_refuses_a_non_string_argument_wallet_id(client, governed):
    response = await client.post(
        f"/mcp/tools/{TOOL}/invoke",
        json={"name": TOOL, "arguments": {"wallet_id": 12345}},
        headers=governed["provisioned"]["agent_headers"],
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Missing wallet_id"
    assert governed["effects"] == []
