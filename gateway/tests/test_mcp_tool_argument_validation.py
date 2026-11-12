"""Tool arguments are checked against the advertised inputSchema before money moves.

Issue #348: a wrong type or an unknown property reached the local tool, the
tool raised, the debit was refunded, and the REST route answered 500 with a
``failed_refunded`` receipt. Money ended correct, but a client input error
should be a client error with no debit, no refund, no receipt and no permit
spend, and the key must stay free for the corrected call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.database import get_session_factory
from app.db.models import LedgerEntryModel, PermitModel, ReceiptModel
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services import dogfood_tool
from app.services.service_registry import get_service_registry
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

TOOL_COST = 2.0


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


@pytest.fixture
def standard_mcp_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("ENABLE_STANDARD_MCP_ENDPOINT", "false")
    get_settings.cache_clear()


@pytest.fixture
def note_tool():
    tool_name = "arg-validation-note"
    runs = {"count": 0}

    def note(text: str = "hello") -> dict[str, Any]:
        runs["count"] += 1
        return {"text": text}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Argument validation note",
        description="Argument validation test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=note,
        credits_per_unit=TOOL_COST,
        unit_name="call",
    )
    try:
        yield tool_name, runs
    finally:
        get_service_registry().unregister_local(tool_name)


async def _ledger_rows(wallet_id: str) -> int:
    async with get_session_factory()() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntryModel)
                .where(LedgerEntryModel.wallet_id == wallet_id)
            )
            or 0
        )


async def _count(model: Any) -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _permit_spent(permit_id: str) -> Decimal:
    async with get_session_factory()() as session:
        permit = await session.get(PermitModel, permit_id)
        assert permit is not None
        return Decimal(permit.spent_credits)


def _rest_body(
    *, tool_name: str, wallet_id: str, permit_id: str, key: str, arguments: Any
) -> dict[str, Any]:
    return {
        "name": tool_name,
        "arguments": arguments,
        "mcp_context": {
            "wallet_id": wallet_id,
            "permit_id": permit_id,
            "idempotency_key": key,
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "argument", "violation"),
    [
        ({"text": 5}, "text", "type"),
        ({"text": "x", "evil": 1}, "evil", "additionalProperties"),
    ],
)
async def test_rest_invalid_arguments_refused_before_any_debit(
    client: AsyncClient,
    clean_database: None,
    note_tool,
    arguments: dict[str, Any],
    argument: str,
    violation: str,
) -> None:
    tool_name, runs = note_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="arg-validation-permit-rest",
    )
    ledger_before = await _ledger_rows(wallet_id)
    receipts_before = await _count(ReceiptModel)
    spent_before = await _permit_spent(permit["permit_id"])

    resp = await client.post(
        f"/mcp/tools/{tool_name}/invoke",
        json=_rest_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            key="arg-validation-rest-1",
            arguments=arguments,
        ),
        headers=ctx["agent_headers"],
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_tool_arguments", detail
    assert detail["argument"] == argument, detail
    assert detail["violation"] == violation, detail
    assert runs["count"] == 0
    assert await _ledger_rows(wallet_id) == ledger_before
    assert await _count(ReceiptModel) == receipts_before
    assert await _permit_spent(permit["permit_id"]) == spent_before

    # The refusal holds no idempotency record: the corrected call on the same
    # key runs and is charged once.
    fixed = await client.post(
        f"/mcp/tools/{tool_name}/invoke",
        json=_rest_body(
            tool_name=tool_name,
            wallet_id=wallet_id,
            permit_id=permit["permit_id"],
            key="arg-validation-rest-1",
            arguments={"text": "x"},
        ),
        headers=ctx["agent_headers"],
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["isError"] is False, fixed.text
    assert runs["count"] == 1
    assert await _ledger_rows(wallet_id) == ledger_before + 1


@pytest.mark.anyio
async def test_jsonrpc_invalid_arguments_answer_invalid_params(
    client: AsyncClient, clean_database: None, note_tool
) -> None:
    tool_name, runs = note_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="arg-validation-permit-rpc",
    )
    ledger_before = await _ledger_rows(wallet_id)
    receipts_before = await _count(ReceiptModel)

    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"text": 5},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "arg-validation-rpc-1",
                },
            },
        },
        headers=ctx["agent_headers"],
    )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32602, error
    assert error["message"] == "Invalid params", error
    assert error["data"]["reason_code"] == "invalid_tool_arguments", error
    assert error["data"]["argument"] == "text", error
    assert error["data"]["violation"] == "type", error
    assert runs["count"] == 0
    assert await _ledger_rows(wallet_id) == ledger_before
    assert await _count(ReceiptModel) == receipts_before


@pytest.mark.anyio
async def test_standard_mcp_invalid_arguments_refused_before_permit_mint(
    client: AsyncClient, clean_database: None, standard_mcp_enabled, note_tool
) -> None:
    tool_name, runs = note_tool
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    ledger_before = await _ledger_rows(wallet_id)
    receipts_before = await _count(ReceiptModel)
    permits_before = await _count(PermitModel)

    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {"text": "x", "evil": 1}},
        },
        headers={
            **ctx["agent_headers"],
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Idempotency-Key": "arg-validation-standard-1",
        },
    )

    assert resp.status_code == 200, resp.text
    error = resp.json()["error"]
    assert error["code"] == -32602, error
    assert error["data"]["reason_code"] == "invalid_tool_arguments", error
    assert error["data"]["argument"] == "evil", error
    assert error["data"]["violation"] == "additionalProperties", error
    assert runs["count"] == 0
    assert await _ledger_rows(wallet_id) == ledger_before
    assert await _count(ReceiptModel) == receipts_before
    assert await _count(PermitModel) == permits_before


@pytest.fixture
def dogfood_registered(tmp_path, monkeypatch):
    # The flag keeps discovery sync from unregistering the tool mid-test.
    monkeypatch.setenv("ENABLE_DOGFOOD_TOOL", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(dogfood_tool, "DOGFOOD_NOTES_PATH", tmp_path / "notes.jsonl")
    dogfood_tool.unregister_dogfood_tool()
    dogfood_tool.register_dogfood_tool()
    try:
        yield dogfood_tool.DOGFOOD_TOOL_ID
    finally:
        dogfood_tool.unregister_dogfood_tool()
        get_settings.cache_clear()


def test_dogfood_tool_advertises_its_length_limit(dogfood_registered: str) -> None:
    schema = get_service_registry().get_local(dogfood_registered)["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["text"]["type"] == "string"
    assert (
        schema["properties"]["text"]["maxLength"] == dogfood_tool.DOGFOOD_MAX_TEXT_CHARS
    )


@pytest.mark.anyio
async def test_oversized_dogfood_text_refused_before_any_debit(
    client: AsyncClient, clean_database: None, dogfood_registered: str
) -> None:
    tool_name = dogfood_registered
    ctx = await provision_agent_wallet(client)
    wallet_id = ctx["agent_wallet_id"]
    permit = await create_tool_permit(
        client,
        wallet_id=wallet_id,
        key_id=ctx["key_id"],
        tool_name=tool_name,
        idem_key="arg-validation-permit-dogfood",
    )
    ledger_before = await _ledger_rows(wallet_id)
    receipts_before = await _count(ReceiptModel)

    resp = await client.post(
        "/mcp/messages",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"text": "x" * (dogfood_tool.DOGFOOD_MAX_TEXT_CHARS + 1)},
                "mcpContext": {
                    "wallet_id": wallet_id,
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "arg-validation-dogfood-1",
                },
            },
        },
        headers=ctx["agent_headers"],
    )

    error = resp.json()["error"]
    assert error["code"] == -32602, error
    assert error["data"]["argument"] == "text", error
    assert error["data"]["violation"] == "maxLength", error
    # The oversized value is not echoed back.
    assert "x" * 100 not in resp.text
    assert await _ledger_rows(wallet_id) == ledger_before
    assert await _count(ReceiptModel) == receipts_before


def test_var_keyword_handler_schema_accepts_extra_arguments() -> None:
    """``**kwargs`` is not a required argument named "kwargs"."""
    from app.services.service_registry import extract_schema_from_callable

    def handler(note: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    schema, _ = extract_schema_from_callable(handler)
    assert schema == {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
        "additionalProperties": True,
    }


def test_unevaluable_schema_is_skipped_not_a_client_error() -> None:
    """A registration bug in the schema never turns into a refusal or a 500."""
    from app.services.tool_arguments import validate_local_tool_arguments

    service = {
        "service_id": "unevaluable-schema",
        "execution_backend": "local",
        "input_schema": {"$ref": "https://www.agentmiddleware.dev/schema.json"},
    }
    validate_local_tool_arguments(service, {"anything": 1})

    invalid = {
        "service_id": "invalid-schema",
        "execution_backend": "local",
        "input_schema": {"type": "not-a-type"},
    }
    validate_local_tool_arguments(invalid, {"anything": 1})


def _model_backed_local_tools() -> list[tuple[str, Any, Any]]:
    from app.services.mcp_phase9_tools import MCP_PHASE9_TOOLS

    tools = [
        (tool["service_id"], tool["func"], tool["input_model"])
        for tool in MCP_PHASE9_TOOLS
    ]
    tools.append(
        (
            dogfood_tool.DOGFOOD_TOOL_ID,
            dogfood_tool._write_note,
            dogfood_tool._WriteNoteArguments,
        )
    )
    return tools


@pytest.mark.parametrize(
    ("service_id", "func", "input_model"),
    _model_backed_local_tools(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_input_model_names_every_required_handler_argument(
    service_id: str, func: Any, input_model: Any
) -> None:
    """The enforced schema must admit every argument the handler requires.

    The advertised schema is closed (``additionalProperties: false``), so a
    required handler argument the input model leaves out could never be
    passed once arguments are validated.
    """
    import inspect

    from app.services.service_registry import pydantic_to_mcp_schema

    schema = pydantic_to_mcp_schema(input_model)
    assert schema is not None
    required_by_handler = {
        param.name
        for param in inspect.signature(func).parameters.values()
        if param.default is inspect.Parameter.empty
        and param.kind
        not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    missing = required_by_handler - set(schema["properties"])
    assert not missing, f"{service_id} schema omits {sorted(missing)}"


def test_awi_dom_state_accepts_its_session_id() -> None:
    from app.services.mcp_phase9_tools import MCP_PHASE9_TOOLS
    from app.services.service_registry import pydantic_to_mcp_schema
    from app.services.tool_arguments import (
        ToolArgumentsInvalid,
        validate_local_tool_arguments,
    )

    tool = next(t for t in MCP_PHASE9_TOOLS if t["service_id"] == "awi_dom_state")
    service = {
        "service_id": "awi_dom_state",
        "execution_backend": "local",
        "input_schema": pydantic_to_mcp_schema(tool["input_model"]),
    }
    validate_local_tool_arguments(
        service, {"session_id": "dom-session", "representation_type": "summary"}
    )
    with pytest.raises(ToolArgumentsInvalid) as refused:
        validate_local_tool_arguments(service, {"representation_type": "summary"})
    assert refused.value.argument == "session_id"
    assert refused.value.violation == "required"
