"""Focused unit coverage for openai_b2a.runner.

Complements test_governed_flow.py (which pins the wire-level governed flow):
these tests cover the runner's pure helpers, record round-trips, key stores,
tool registration, result rendering, and the run() happy path plus refusal
cases, with a stub client so no network or HTTP layer is involved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from b2a_sdk.models import InvocationResult, Receipt

from openai_b2a.runner import (
    GovernedToolRunner,
    InMemoryOperationKeyStore,
    JsonFileOperationKeyStore,
    OperationRecord,
    PermitRecord,
    function_name_for,
    normalize_tool_call,
    operation_key_for,
)

TOOL = "partner.notes.write"
WALLET = "wallet-1"


def _receipt(tool: str = TOOL, receipt_id: str = "rcpt-1") -> Receipt:
    now = datetime.now(UTC).isoformat()
    return Receipt.from_dict(
        {
            "receipt_id": receipt_id,
            "idempotency_record_id": "idem-1",
            "permit_id": "permit-1",
            "wallet_id": WALLET,
            "key_id": "key-1",
            "tool": tool,
            "request_hash": "a" * 64,
            "response_hash": "b" * 64,
            "ledger_entry_id": "ledger-1",
            "dispatch_attempt_id": None,
            "credits_authorized": "2",
            "credits_charged": "2",
            "outcome": "success",
            "audit_event_id": "audit-1",
            "created_at": now,
            "signature": "sig-1",
            "signature_key_id": "signing-key-1",
        }
    )


class StubClient:
    """Duck-typed stand-in for B2AClient with no network."""

    def __init__(self) -> None:
        self.permit_calls: list[tuple[object, str]] = []
        self.invoke_calls: list[tuple[str, dict, str, str]] = []
        self.receipt_ids = iter([f"rcpt-{n}" for n in range(1, 100)])

    async def create_permit(self, request, *, idempotency_key: str):
        self.permit_calls.append((request, idempotency_key))
        return SimpleNamespace(permit_id="permit-1")

    async def invoke_tool(self, tool_name, arguments, *, wallet_id, permit_id, idempotency_key):
        self.invoke_calls.append((tool_name, arguments, permit_id, idempotency_key))
        return InvocationResult(
            content=[{"type": "text", "text": "ok"}],
            structured_content={"ok": True},
            receipt=_receipt(tool_name, next(self.receipt_ids)),
            raw={},
        )


def _runner(client=None, **kwargs) -> GovernedToolRunner:
    return GovernedToolRunner(client or StubClient(), wallet_id=WALLET, run_id="run-1", **kwargs)  # type: ignore[arg-type]


def _chat_call(call_id="call_1", name=TOOL, arguments='{"text": "hi"}'):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=function_name_for(name), arguments=arguments),
    )


# ── normalize_tool_call ──────────────────────────────────────────────────────


def test_normalize_chat_completions_object_shape():
    call = normalize_tool_call(_chat_call())
    assert call.id == "call_1"
    assert call.name == function_name_for(TOOL)
    assert call.arguments == {"text": "hi"}


def test_normalize_responses_api_dict_shape_uses_call_id():
    call = normalize_tool_call(
        {
            "type": "function_call",
            "id": "fc_item_999",
            "call_id": "call_xyz",
            "name": "some_tool",
            "arguments": '{"a": 1}',
        }
    )
    assert call.id == "call_xyz"
    assert call.name == "some_tool"
    assert call.arguments == {"a": 1}


def test_normalize_accepts_empty_and_mapping_arguments():
    assert normalize_tool_call({"id": "c1", "function": {"name": "f"}}).arguments == {}
    assert (
        normalize_tool_call({"id": "c1", "function": {"name": "f", "arguments": ""}}).arguments
        == {}
    )
    call = normalize_tool_call({"id": "c1", "function": {"name": "f", "arguments": {"k": "v"}}})
    assert call.arguments == {"k": "v"}


def test_normalize_refuses_missing_id_and_non_object_arguments():
    with pytest.raises(ValueError, match="no id"):
        normalize_tool_call({"function": {"name": "f", "arguments": "{}"}})
    with pytest.raises(TypeError):
        normalize_tool_call({"id": "c1", "function": {"name": "f", "arguments": "[1, 2]"}})
    with pytest.raises(TypeError):
        normalize_tool_call({"id": "c1", "function": {"name": "f", "arguments": 42}})


# ── key and name helpers ─────────────────────────────────────────────────────


def test_operation_key_derives_from_tool_call_id():
    assert operation_key_for("call_abc") == "oai-call_abc"


def test_operation_key_refuses_blank_and_over_long_ids():
    with pytest.raises(ValueError):
        operation_key_for("   ")
    with pytest.raises(ValueError, match="too long"):
        operation_key_for("c" * 125)


def test_function_name_sanitizes_and_truncates():
    assert function_name_for("partner.notes.write") == "partner_notes_write"
    assert function_name_for("a" * 100) == "a" * 64


# ── registration ─────────────────────────────────────────────────────────────


def test_register_tool_definition_and_reverse_mapping():
    runner = _runner()
    definition = runner.register_tool(TOOL, description="Append a note")
    assert definition == {
        "type": "function",
        "function": {
            "name": "partner_notes_write",
            "description": "Append a note",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    assert runner.tool_name_for("partner_notes_write") == TOOL
    assert runner.permit_key_for(TOOL) == f"oai-permit-run-1-{TOOL}"


def test_register_tool_refuses_collisions_and_unknown_lookup():
    runner = _runner()
    runner.register_tool("a.b", description="x")
    with pytest.raises(ValueError, match="already maps"):
        runner.register_tool("a_b", description="y")
    with pytest.raises(ValueError, match="not registered"):
        runner.tool_name_for("nope")


def test_bad_run_id_is_refused():
    with pytest.raises(ValueError):
        GovernedToolRunner(StubClient(), wallet_id=WALLET, run_id=" run-1")  # type: ignore[arg-type]


# ── records and stores ───────────────────────────────────────────────────────


def test_operation_and_permit_records_round_trip():
    operation = OperationRecord(
        tool_call_id="call_1",
        tool_name=TOOL,
        idempotency_key="oai-call_1",
        permit_idempotency_key=f"oai-permit-run-1-{TOOL}",
        wallet_id=WALLET,
    )
    assert OperationRecord.from_dict(operation.to_dict()) == operation
    permit = PermitRecord(
        permit_idempotency_key=f"oai-permit-run-1-{TOOL}",
        tool_name=TOOL,
        expires_at=datetime.now(UTC).isoformat(),
        request_payload={"issuer_wallet_id": WALLET},
    )
    assert PermitRecord.from_dict(permit.to_dict()) == permit


def test_in_memory_store_round_trip_and_missing_returns_none():
    store = InMemoryOperationKeyStore()
    assert store.get_operation("missing") is None
    assert store.get_permit("missing") is None
    operation = OperationRecord(
        tool_call_id="call_1",
        tool_name=TOOL,
        idempotency_key="oai-call_1",
        permit_idempotency_key="pk",
    )
    store.put_operation(operation)
    assert store.get_operation("call_1") == operation
    permit = PermitRecord(permit_idempotency_key="pk", tool_name=TOOL, expires_at="x")
    store.put_permit(permit)
    assert store.get_permit("pk") == permit


def test_json_file_store_persists_across_instances(tmp_path):
    path = tmp_path / "ops.json"
    store = JsonFileOperationKeyStore(path)
    operation = OperationRecord(
        tool_call_id="call_1",
        tool_name=TOOL,
        idempotency_key="oai-call_1",
        permit_idempotency_key="pk",
        wallet_id=WALLET,
    )
    store.put_operation(operation)
    reloaded = JsonFileOperationKeyStore(path)
    assert reloaded.get_operation("call_1") == operation
    persisted = json.loads(path.read_text())
    assert persisted["operations"]["call_1"]["idempotency_key"] == "oai-call_1"


def test_json_file_store_rejects_non_object_file(tmp_path):
    path = tmp_path / "ops.json"
    path.write_text("[1, 2]")
    with pytest.raises(TypeError):
        JsonFileOperationKeyStore(path).get_operation("call_1")


# ── run() ────────────────────────────────────────────────────────────────────


async def test_run_happy_path_reuses_one_permit_for_two_calls():
    client = StubClient()
    runner = _runner(client)
    runner.register_tool(TOOL, description="Append a note")

    results = await runner.run_all([_chat_call("call_1"), _chat_call("call_2")])

    assert [r.idempotency_key for r in results] == ["oai-call_1", "oai-call_2"]
    assert results[0].receipt.receipt_id != results[1].receipt.receipt_id
    assert len(client.permit_calls) == 1
    assert len(client.invoke_calls) == 2
    request, key = client.permit_calls[0]
    assert key == f"oai-permit-run-1-{TOOL}"
    assert request.allowed_tools == [TOOL]


async def test_run_retry_of_same_id_reuses_permit_and_key():
    client = StubClient()
    runner = _runner(client)
    runner.register_tool(TOOL, description="Append a note")

    first = await runner.run(_chat_call("call_1"))
    second = await runner.run(_chat_call("call_1"))

    assert second.idempotency_key == first.idempotency_key == "oai-call_1"
    assert len(client.permit_calls) == 1
    assert [c[3] for c in client.invoke_calls] == ["oai-call_1", "oai-call_1"]


async def test_run_refuses_call_without_id_before_any_client_call():
    client = StubClient()
    runner = _runner(client)
    runner.register_tool(TOOL, description="Append a note")

    with pytest.raises(ValueError, match="no id"):
        await runner.run(SimpleNamespace(function=SimpleNamespace(name="f", arguments="{}")))

    assert client.permit_calls == [] and client.invoke_calls == []


async def test_result_renders_both_openai_output_shapes():
    runner = _runner()
    runner.register_tool(TOOL, description="Append a note")
    result = await runner.run(_chat_call("call_1"))

    message = result.as_tool_message()
    assert message["role"] == "tool" and message["tool_call_id"] == "call_1"
    payload = json.loads(message["content"])
    assert payload["idempotency_key"] == "oai-call_1"
    assert payload["receipt"]["receipt_id"] == result.receipt.receipt_id

    item = result.as_function_call_output()
    assert item["type"] == "function_call_output" and item["call_id"] == "call_1"
    assert json.loads(item["output"]) == payload
