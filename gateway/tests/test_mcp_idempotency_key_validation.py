"""Present-but-unusable replay keys and malformed envelopes are refused early.

Regression coverage for two review findings against c6b0534:

* ``POST /mcp`` (standard MCP) silently replaced an explicitly supplied but
  malformed ``io.agentmiddleware/idempotency_key`` with a generated key, so
  identical retries carrying the bad key executed and charged twice.
* ``POST /mcp/messages`` (legacy JSON-RPC) assumed mapping types before
  reading the envelope and answered malformed bodies with HTTP 500.

Every negative case counts effects, ledger entries, permits, and idempotency
records: a receipt count alone cannot tell "refused" from "executed and the
receipt was dropped".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import IdempotencyRecordModel, PermitModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.idempotency import MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH
from app.services.service_registry import get_service_registry
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
_ABSENT = object()


@pytest.fixture
async def client():
    # raise_app_exceptions=False: a malformed envelope must come back as a
    # controlled status, and the assertion message should say which one.
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


class CountedTool:
    """A local governed tool whose every side effect is counted."""

    def __init__(self, name: str, credits: float = 2.0) -> None:
        self.name = name
        self.effects: list[str] = []
        self._registry = get_service_registry()
        self._registry.register_local(
            service_id=name,
            name=name,
            description="Counted side effect for idempotency-key validation tests",
            category=ServiceCategory.AGENT_COMMS,
            func=self._effect,
            credits_per_unit=credits,
            unit_name="call",
        )

    def _effect(self, text: str = "one") -> dict[str, str]:
        self.effects.append(text)
        return {"text": text}

    def close(self) -> None:
        self._registry.unregister_local(self.name)


@pytest.fixture
def counted_tool():
    tool = CountedTool("keycheck.effect")
    try:
        yield tool
    finally:
        tool.close()


async def _db_counts() -> dict[str, int]:
    async with get_session_factory()() as session:
        permits = (
            await session.execute(select(func.count()).select_from(PermitModel))
        ).scalar_one()
        records = (
            await session.execute(
                select(func.count()).select_from(IdempotencyRecordModel)
            )
        ).scalar_one()
    return {"permits": int(permits), "idempotency_records": int(records)}


async def _snapshot(
    client: AsyncClient, provisioned: dict, tool: CountedTool
) -> dict[str, int]:
    """Everything a refused call must leave untouched."""
    ledger = await client.get(
        f"/v1/billing/ledger/{provisioned['agent_wallet_id']}",
        headers=provisioned["agent_headers"],
    )
    assert ledger.status_code == 200, ledger.text
    counts = await _db_counts()
    return {
        "effects": len(tool.effects),
        "ledger_entries": len(ledger.json()["entries"]),
        **counts,
    }


def _standard_call(
    tool: str, *, meta: Any = _ABSENT, request_id: int = 1, text: str = "one"
) -> dict:
    params: dict[str, Any] = {"name": tool, "arguments": {"text": text}}
    if meta is not _ABSENT:
        params["_meta"] = {META_KEY: meta}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": params,
    }


def _assert_invalid_key(response, reason_code: str) -> None:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "result" not in payload, payload
    error = payload["error"]
    assert error["code"] == -32602, error
    assert error["message"].startswith("invalid_idempotency_key"), error
    assert error["data"]["error"] == "invalid_idempotency_key"
    assert error["data"]["reason_code"] == reason_code, error
    assert error["data"]["remediation"]["type"] == "retry_with_valid_idempotency_key"


# --------------------------------------------------------------------------- #
# Standard MCP endpoint: params._meta and Idempotency-Key header
# --------------------------------------------------------------------------- #

INVALID_META_KEYS = [
    pytest.param("", "idempotency_key_blank", id="empty-string"),
    pytest.param("   ", "idempotency_key_blank", id="whitespace"),
    pytest.param(123, "idempotency_key_not_a_string", id="integer"),
    pytest.param(True, "idempotency_key_not_a_string", id="boolean"),
    pytest.param([], "idempotency_key_not_a_string", id="array"),
    pytest.param({"key": "retry"}, "idempotency_key_not_a_string", id="object"),
    pytest.param(
        "x" * (MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH + 1),
        "idempotency_key_too_long",
        id="129-chars",
    ),
    pytest.param("x" * 257, "idempotency_key_too_long", id="257-chars"),
    pytest.param("abc\x00def", "idempotency_key_control_characters", id="nul"),
    pytest.param("abc\ndef", "idempotency_key_control_characters", id="newline"),
]


@pytest.mark.parametrize("bad_key,reason_code", INVALID_META_KEYS)
async def test_standard_meta_key_invalid_is_refused_before_any_effect(
    client, standard_mcp_enabled, clean_database, counted_tool, bad_key, reason_code
):
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}

    # Control: the same wallet and tool execute exactly once under a valid key.
    control = await client.post(
        "/mcp",
        json=_standard_call(counted_tool.name, meta="valid-control"),
        headers=headers,
    )
    assert "result" in control.json(), control.text
    assert counted_tool.effects == ["one"]

    before = await _snapshot(client, provisioned, counted_tool)
    responses = [
        await client.post(
            "/mcp",
            json=_standard_call(counted_tool.name, meta=bad_key),
            headers=headers,
        )
        for _ in range(2)
    ]
    for response in responses:
        _assert_invalid_key(response, reason_code)
    after = await _snapshot(client, provisioned, counted_tool)
    assert after == before, f"refused key changed state: {before} -> {after}"


@pytest.mark.parametrize(
    "bad_header,reason_code",
    [
        pytest.param("", "idempotency_key_blank", id="empty-header"),
        pytest.param("   ", "idempotency_key_blank", id="blank-header"),
        pytest.param(
            "h" * (MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH + 1),
            "idempotency_key_too_long",
            id="129-chars",
        ),
    ],
)
async def test_standard_header_key_invalid_is_refused_before_any_effect(
    client, standard_mcp_enabled, clean_database, counted_tool, bad_header, reason_code
):
    provisioned = await provision_agent_wallet(client)
    before = await _snapshot(client, provisioned, counted_tool)
    response = await client.post(
        "/mcp",
        json=_standard_call(counted_tool.name),
        headers={
            **provisioned["agent_headers"],
            **MCP_HEADERS,
            "Idempotency-Key": bad_header,
        },
    )
    _assert_invalid_key(response, reason_code)
    assert await _snapshot(client, provisioned, counted_tool) == before


async def test_standard_header_and_meta_conflict_is_refused(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    provisioned = await provision_agent_wallet(client)
    before = await _snapshot(client, provisioned, counted_tool)
    response = await client.post(
        "/mcp",
        json=_standard_call(counted_tool.name, meta="meta-key-1"),
        headers={
            **provisioned["agent_headers"],
            **MCP_HEADERS,
            "Idempotency-Key": "header-key-1",
        },
    )
    _assert_invalid_key(response, "idempotency_key_conflict")
    assert await _snapshot(client, provisioned, counted_tool) == before


async def test_standard_header_valid_with_meta_invalid_is_refused(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    """A malformed explicit source is never silently ignored in favor of the other."""
    provisioned = await provision_agent_wallet(client)
    before = await _snapshot(client, provisioned, counted_tool)
    response = await client.post(
        "/mcp",
        json=_standard_call(counted_tool.name, meta=123),
        headers={
            **provisioned["agent_headers"],
            **MCP_HEADERS,
            "Idempotency-Key": "header-key-2",
        },
    )
    _assert_invalid_key(response, "idempotency_key_not_a_string")
    assert await _snapshot(client, provisioned, counted_tool) == before


async def test_standard_repeated_distinct_headers_are_a_conflict(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    provisioned = await provision_agent_wallet(client)
    before = await _snapshot(client, provisioned, counted_tool)
    headers = [
        *provisioned["agent_headers"].items(),
        *MCP_HEADERS.items(),
        ("Idempotency-Key", "dup-a"),
        ("Idempotency-Key", "dup-b"),
    ]
    response = await client.post(
        "/mcp", json=_standard_call(counted_tool.name), headers=headers
    )
    _assert_invalid_key(response, "idempotency_key_conflict")
    assert await _snapshot(client, provisioned, counted_tool) == before


async def test_standard_same_key_in_header_and_meta_executes_once_and_replays(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    provisioned = await provision_agent_wallet(client)
    headers = {
        **provisioned["agent_headers"],
        **MCP_HEADERS,
        "Idempotency-Key": "same-1",
    }
    body = _standard_call(counted_tool.name, meta="same-1")
    base = await _snapshot(client, provisioned, counted_tool)

    first = await client.post("/mcp", json=body, headers=headers)
    assert first.status_code == 200, first.text
    receipt = first.json()["result"]["receipt"]
    after_first = await _snapshot(client, provisioned, counted_tool)
    assert counted_tool.effects == ["one"]
    assert after_first["permits"] == base["permits"] + 1
    assert after_first["ledger_entries"] == base["ledger_entries"] + 1

    replay = await client.post("/mcp", json=body, headers=headers)
    assert replay.json()["result"]["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert await _snapshot(client, provisioned, counted_tool) == after_first


@pytest.mark.parametrize("via", ["meta", "header"])
async def test_standard_valid_single_source_key_replays_without_second_effect(
    client, standard_mcp_enabled, clean_database, counted_tool, via
):
    provisioned = await provision_agent_wallet(client)
    key = f"single-{via}-1"
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    if via == "header":
        headers["Idempotency-Key"] = key
        body = _standard_call(counted_tool.name)
    else:
        body = _standard_call(counted_tool.name, meta=key)
    base = await _snapshot(client, provisioned, counted_tool)

    first = await client.post("/mcp", json=body, headers=headers)
    assert first.status_code == 200, first.text
    receipt_id = first.json()["result"]["receipt"]["receipt_id"]
    after_first = await _snapshot(client, provisioned, counted_tool)
    assert after_first["effects"] == 1
    assert after_first["ledger_entries"] == base["ledger_entries"] + 1
    assert after_first["permits"] == base["permits"] + 1

    replay = await client.post("/mcp", json=body, headers=headers)
    assert replay.json()["result"]["receipt"]["receipt_id"] == receipt_id
    assert await _snapshot(client, provisioned, counted_tool) == after_first


@pytest.mark.parametrize("via", ["meta", "header"])
async def test_standard_key_at_the_length_limit_is_accepted_verbatim(
    client, standard_mcp_enabled, clean_database, counted_tool, via
):
    """128 characters is the documented and durable maximum; it must replay."""
    provisioned = await provision_agent_wallet(client)
    key = "k" * MAX_CLIENT_IDEMPOTENCY_KEY_LENGTH
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    if via == "header":
        headers["Idempotency-Key"] = key
        body = _standard_call(counted_tool.name)
    else:
        body = _standard_call(counted_tool.name, meta=key)
    first = await client.post("/mcp", json=body, headers=headers)
    assert first.status_code == 200 and "result" in first.json(), first.text
    replay = await client.post("/mcp", json=body, headers=headers)
    assert (
        replay.json()["result"]["receipt"]["receipt_id"]
        == first.json()["result"]["receipt"]["receipt_id"]
    )
    assert counted_tool.effects == ["one"]


async def test_standard_distinct_valid_keys_stay_distinct_operations(
    client, standard_mcp_enabled, clean_database, counted_tool
):
    """Keys are never normalized into one another (case, whitespace, prefix)."""
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    receipts = set()
    for key in ("Key-1", "key-1", " key-1", "key-1 "):
        response = await client.post(
            "/mcp", json=_standard_call(counted_tool.name, meta=key), headers=headers
        )
        assert "result" in response.json(), response.text
        receipts.add(response.json()["result"]["receipt"]["receipt_id"])
    assert len(receipts) == 4
    assert len(counted_tool.effects) == 4


@pytest.mark.parametrize("meta", [_ABSENT, None], ids=["no-entry", "json-null"])
async def test_standard_unkeyed_calls_are_documented_as_new_operations(
    client, standard_mcp_enabled, clean_database, counted_tool, meta
):
    """No key anywhere (or a JSON null entry) means each call is a new charge.

    This pins the documented contract for un-keyed clients so it cannot drift
    silently in either direction: absent must not be refused like malformed,
    and it must not be quietly deduplicated either.
    """
    provisioned = await provision_agent_wallet(client)
    headers = {**provisioned["agent_headers"], **MCP_HEADERS}
    body = _standard_call(counted_tool.name, meta=meta)
    base = await _snapshot(client, provisioned, counted_tool)
    receipts = [
        (await client.post("/mcp", json=body, headers=headers)).json()["result"][
            "receipt"
        ]["receipt_id"]
        for _ in range(2)
    ]
    assert len(set(receipts)) == 2
    snapshot = await _snapshot(client, provisioned, counted_tool)
    assert snapshot["effects"] == 2
    assert snapshot["ledger_entries"] == base["ledger_entries"] + 2
    assert snapshot["permits"] == base["permits"] + 2


# --------------------------------------------------------------------------- #
# Legacy JSON-RPC endpoint: envelope validation
# --------------------------------------------------------------------------- #

MALFORMED_ENVELOPES = [
    pytest.param([], -32600, None, id="array-body"),
    pytest.param(None, -32600, None, id="null-body"),
    pytest.param("bad", -32600, None, id="string-body"),
    pytest.param(3, -32600, None, id="number-body"),
    pytest.param(True, -32600, None, id="boolean-body"),
    pytest.param({}, -32600, None, id="empty-object"),
    pytest.param({"id": 7, "method": 5}, -32600, 7, id="non-string-method"),
    pytest.param({"id": 8, "method": ""}, -32600, 8, id="empty-method"),
    pytest.param(
        {"id": 9, "method": "tools/call", "params": []}, -32602, 9, id="array-params"
    ),
    pytest.param(
        {"id": 10, "method": "tools/call", "params": "x"},
        -32602,
        10,
        id="string-params",
    ),
    pytest.param(
        {"id": 11, "method": "tools/list", "params": []},
        -32602,
        11,
        id="list-array-params",
    ),
    pytest.param(
        {"id": 12, "method": "tools/call", "params": {"mcpContext": [1]}},
        -32602,
        12,
        id="array-context",
    ),
    pytest.param(
        {"id": 13, "method": "tools/call", "params": {"name": 5}},
        -32602,
        13,
        id="non-string-name",
    ),
    pytest.param(
        {"id": 14, "method": "tools/call", "params": {}}, -32602, 14, id="missing-name"
    ),
    pytest.param(
        {"id": 15, "method": "tools/call", "params": {"name": "t", "arguments": []}},
        -32602,
        15,
        id="array-arguments",
    ),
    pytest.param(
        {"id": 16, "method": "tools/call", "params": {"name": "t", "arguments": "x"}},
        -32602,
        16,
        id="string-arguments",
    ),
    pytest.param(
        {
            "id": 17,
            "method": "tools/call",
            "params": {"name": "t", "mcpContext": {"wallet_id": 7}},
        },
        -32602,
        17,
        id="non-string-wallet",
    ),
    pytest.param(
        {
            "id": 18,
            "method": "tools/call",
            "params": {"name": "t", "mcpContext": {"idempotency_key": 5}},
        },
        -32602,
        18,
        id="non-string-key",
    ),
    pytest.param(
        {"id": {"nested": True}, "method": "tools/call", "params": []},
        -32602,
        None,
        id="object-id-is-nulled",
    ),
    pytest.param(
        {"id": True, "method": "tools/call", "params": []},
        -32602,
        None,
        id="boolean-id-is-nulled",
    ),
    pytest.param(
        {"id": "abc", "method": "nope"},
        -32601,
        "abc",
        id="unknown-method-keeps-string-id",
    ),
]


@pytest.mark.parametrize("body,code,echoed_id", MALFORMED_ENVELOPES)
async def test_legacy_malformed_envelope_returns_controlled_jsonrpc_error(
    client, body, code, echoed_id
):
    response = await client.post(
        "/mcp/messages",
        content=json.dumps(body),
        headers={**BOOTSTRAP_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 200, (
        f"HTTP {response.status_code}: {response.text[:200]}"
    )
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == echoed_id
    assert "result" not in payload
    assert payload["error"]["code"] == code, payload
    assert "Internal" not in payload["error"]["message"]


async def test_legacy_malformed_json_keeps_http_400(client):
    response = await client.post(
        "/mcp/messages",
        content=b"{not json",
        headers={**BOOTSTRAP_HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON"


async def test_legacy_tools_list_ignores_a_non_string_category(client):
    response = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"category": 5},
        },
        headers=BOOTSTRAP_HEADERS,
    )
    assert response.status_code == 200
    assert "tools" in response.json()["result"]


@pytest.fixture
async def governed_legacy(client, clean_database, counted_tool):
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name=counted_tool.name,
    )
    return provisioned, permit


def _legacy_call(
    tool: str,
    provisioned: dict,
    permit: dict,
    *,
    key: Any = _ABSENT,
    arguments: Any = _ABSENT,
    request_id: int = 1,
) -> dict:
    context: dict[str, Any] = {
        "wallet_id": provisioned["agent_wallet_id"],
        "permit_id": permit["permit_id"],
    }
    if key is not _ABSENT:
        context["idempotency_key"] = key
    params: dict[str, Any] = {"name": tool, "mcpContext": context}
    params["arguments"] = {"text": "one"} if arguments is _ABSENT else arguments
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": params,
    }


async def test_legacy_valid_permit_with_malformed_members_has_no_effect_or_debit(
    client, governed_legacy, counted_tool
):
    """A valid permit does not rescue a malformed envelope: nothing runs or charges."""
    provisioned, permit = governed_legacy
    headers = provisioned["agent_headers"]
    before = await _snapshot(client, provisioned, counted_tool)

    cases = [
        (
            _legacy_call(
                counted_tool.name, provisioned, permit, key="ok-1", arguments=[]
            ),
            {},
            None,
        ),
        (
            _legacy_call(
                counted_tool.name, provisioned, permit, key="ok-1", arguments="x"
            ),
            {},
            None,
        ),
        (
            _legacy_call(counted_tool.name, provisioned, permit, key=123),
            {},
            "idempotency_key_not_a_string",
        ),
        (
            _legacy_call(counted_tool.name, provisioned, permit, key=""),
            {},
            "idempotency_key_blank",
        ),
        (
            _legacy_call(counted_tool.name, provisioned, permit, key="k" * 129),
            {},
            "idempotency_key_too_long",
        ),
        (
            _legacy_call(counted_tool.name, provisioned, permit, key="body-1"),
            {"Idempotency-Key": "header-1"},
            "idempotency_key_conflict",
        ),
        (
            _legacy_call(counted_tool.name, provisioned, permit, key="body-2"),
            {"Idempotency-Key": ""},
            "idempotency_key_blank",
        ),
    ]
    for body, extra_headers, reason_code in cases:
        response = await client.post(
            "/mcp/messages", json=body, headers={**headers, **extra_headers}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "result" not in payload, payload
        assert payload["error"]["code"] == -32602, payload
        if reason_code is not None:
            assert payload["error"]["data"]["reason_code"] == reason_code, payload
    assert await _snapshot(client, provisioned, counted_tool) == before


async def test_legacy_same_key_in_header_and_context_executes_once_and_replays(
    client, governed_legacy, counted_tool
):
    """The Python SDK sends the key in both places; that shape must keep working."""
    provisioned, permit = governed_legacy
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "sdk-shape-1"}
    body = _legacy_call(counted_tool.name, provisioned, permit, key="sdk-shape-1")
    base = await _snapshot(client, provisioned, counted_tool)

    first = await client.post("/mcp/messages", json=body, headers=headers)
    assert first.status_code == 200, first.text
    receipt_id = first.json()["result"]["receipt"]["receipt_id"]
    after_first = await _snapshot(client, provisioned, counted_tool)
    assert after_first["effects"] == 1
    assert after_first["ledger_entries"] == base["ledger_entries"] + 1

    replay = await client.post("/mcp/messages", json=body, headers=headers)
    assert replay.json()["result"]["receipt"]["receipt_id"] == receipt_id
    assert await _snapshot(client, provisioned, counted_tool) == after_first


async def test_legacy_context_null_key_reads_as_absent(
    client, governed_legacy, counted_tool
):
    """``idempotency_key: null`` is the typed schema's "no key"; governed calls then need one."""
    provisioned, permit = governed_legacy
    before = await _snapshot(client, provisioned, counted_tool)
    response = await client.post(
        "/mcp/messages",
        json=_legacy_call(counted_tool.name, provisioned, permit, key=None),
        headers=provisioned["agent_headers"],
    )
    assert response.status_code == 200
    assert response.json()["error"]["message"] == "idempotency_key_required"
    assert await _snapshot(client, provisioned, counted_tool) == before


# --------------------------------------------------------------------------- #
# Legacy REST invoke: typed context plus header
# --------------------------------------------------------------------------- #


async def test_rest_invoke_refuses_blank_or_conflicting_keys_before_any_effect(
    client, governed_legacy, counted_tool
):
    provisioned, permit = governed_legacy
    before = await _snapshot(client, provisioned, counted_tool)
    context = {
        "wallet_id": provisioned["agent_wallet_id"],
        "permit_id": permit["permit_id"],
        "idempotency_key": "",
    }
    blank = await client.post(
        f"/mcp/tools/{counted_tool.name}/invoke",
        json={
            "name": counted_tool.name,
            "arguments": {"text": "one"},
            "mcp_context": context,
        },
        headers=provisioned["agent_headers"],
    )
    assert blank.status_code == 400, blank.text
    assert blank.json()["detail"]["reason_code"] == "idempotency_key_blank"

    context["idempotency_key"] = "rest-body-1"
    conflict = await client.post(
        f"/mcp/tools/{counted_tool.name}/invoke",
        json={
            "name": counted_tool.name,
            "arguments": {"text": "one"},
            "mcp_context": context,
        },
        headers={**provisioned["agent_headers"], "Idempotency-Key": "rest-header-1"},
    )
    assert conflict.status_code == 400, conflict.text
    assert conflict.json()["detail"]["reason_code"] == "idempotency_key_conflict"
    assert await _snapshot(client, provisioned, counted_tool) == before

    # Control: the same body with agreeing sources executes once and replays.
    headers = {**provisioned["agent_headers"], "Idempotency-Key": "rest-body-1"}
    first = await client.post(
        f"/mcp/tools/{counted_tool.name}/invoke",
        json={
            "name": counted_tool.name,
            "arguments": {"text": "one"},
            "mcp_context": context,
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"/mcp/tools/{counted_tool.name}/invoke",
        json={
            "name": counted_tool.name,
            "arguments": {"text": "one"},
            "mcp_context": context,
        },
        headers=headers,
    )
    assert (
        replay.json()["receipt"]["receipt_id"] == first.json()["receipt"]["receipt_id"]
    )
    assert counted_tool.effects == ["one"]
