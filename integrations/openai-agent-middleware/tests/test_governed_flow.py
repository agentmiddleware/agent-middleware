"""The OpenAI runner turns each tool call into exactly one governed action.

These tests pin the operation-identity contract: the key on the wire is
derived from the model's ``tool_call.id``, it is persisted before any network
call, a retry (same id, even from a fresh process) reuses it, and a tool call
without an id is refused rather than given an invented key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from b2a_sdk.errors import TransportError

from openai_b2a import (
    B2AClient,
    GovernedToolRunner,
    InMemoryOperationKeyStore,
    JsonFileOperationKeyStore,
    OperationRecord,
    PermitRecord,
    normalize_tool_call,
)
from openai_b2a.runner import function_name_for

TOOL = "partner.notes.write"
WALLET = "wallet-1"


def _permit_payload(permit_id: str = "permit-1") -> dict:
    now = datetime.now(UTC)
    return {
        "permit_id": permit_id,
        "issuer_wallet_id": WALLET,
        "subject_wallet_id": WALLET,
        "subject_key_id": "key-1",
        "scopes": [f"tool:{TOOL}:invoke", "billing:charge"],
        "allowed_tools": [TOOL],
        "max_credits": "100",
        "spent_credits": "0",
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "nonce": "nonce-1",
        "status": "active",
        "signature": "sig-permit-1",
        "key_id": "signing-key-1",
        "issued_at": now.isoformat(),
        "revoked_at": None,
    }


def _receipt_payload(idempotency_key: str, receipt_id: str = "rcpt-1") -> dict:
    return {
        "receipt_id": receipt_id,
        "idempotency_record_id": "idem-1",
        "permit_id": "permit-1",
        "wallet_id": WALLET,
        "key_id": "key-1",
        "tool": TOOL,
        "request_hash": "a" * 64,
        "response_hash": "b" * 64,
        "ledger_entry_id": "ledger-1",
        "dispatch_attempt_id": None,
        "credits_authorized": "2",
        "credits_charged": "2",
        "outcome": "success",
        "audit_event_id": "audit-1",
        "created_at": datetime.now(UTC).isoformat(),
        "signature": "sig-receipt-1",
        "signature_key_id": "signing-key-1",
        "idempotency_key": idempotency_key,
    }


class FakeTrustPlane:
    """A minimal server: permits replay by key, invokes replay by key."""

    def __init__(self) -> None:
        self.permit_requests: list[httpx.Request] = []
        self.invoke_requests: list[httpx.Request] = []
        self._receipts: dict[str, str] = {}  # idempotency key -> receipt id

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            self.permit_requests.append(request)
            return httpx.Response(201, json=_permit_payload())
        if request.url.path == "/mcp/messages":
            self.invoke_requests.append(request)
            body = json.loads(request.content)
            key = body["params"]["mcpContext"]["idempotency_key"]
            assert request.headers["idempotency-key"] == key
            receipt_id = self._receipts.setdefault(key, f"rcpt-{len(self._receipts) + 1}")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {"ok": True},
                        "isError": False,
                        "receipt": _receipt_payload(key, receipt_id),
                    },
                },
            )
        return httpx.Response(404, json={"detail": "unexpected path"})


def _client(plane: FakeTrustPlane) -> B2AClient:
    return B2AClient(api_key="test-key", base_url="http://trust.test", transport=plane.transport())


def _chat_tool_call(call_id: str = "call_abc123", arguments: str = '{"text": "hi"}'):
    """The Chat Completions shape: attribute objects with a JSON-string arguments."""
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=function_name_for(TOOL), arguments=arguments),
    )


# ── operation identity ───────────────────────────────────────────────────────


async def test_tool_call_id_is_the_operation_key_on_the_wire():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    runner.register_tool(TOOL, description="Append a note")

    result = await runner.run(_chat_tool_call("call_abc123"))

    assert result.idempotency_key == "oai-call_abc123"
    invoke = json.loads(plane.invoke_requests[0].content)
    assert invoke["params"]["name"] == TOOL
    assert invoke["params"]["arguments"] == {"text": "hi"}
    assert invoke["params"]["mcpContext"]["idempotency_key"] == "oai-call_abc123"
    assert plane.invoke_requests[0].headers["idempotency-key"] == "oai-call_abc123"
    # The permit is scoped to the run and the tool, and its key is stable.
    assert plane.permit_requests[0].headers["idempotency-key"] == f"oai-permit-run-1-{TOOL}"
    permit_body = json.loads(plane.permit_requests[0].content)
    assert permit_body["allowed_tools"] == [TOOL]
    assert permit_body["max_credits"] == "100"


async def test_retrying_the_same_tool_call_is_one_action():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    runner.register_tool(TOOL, description="Append a note")

    first = await runner.run(_chat_tool_call("call_abc123"))
    second = await runner.run(_chat_tool_call("call_abc123"))

    assert first.receipt.receipt_id == second.receipt.receipt_id
    keys = {
        json.loads(r.content)["params"]["mcpContext"]["idempotency_key"]
        for r in plane.invoke_requests
    }
    assert keys == {"oai-call_abc123"}, "a retry must never carry a fresh key"
    assert len(plane.permit_requests) == 1, "the recorded permit is reused, not re-minted"


async def test_distinct_tool_calls_are_distinct_actions_under_one_permit():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    runner.register_tool(TOOL, description="Append a note")

    results = await runner.run_all([_chat_tool_call("call_1"), _chat_tool_call("call_2")])

    assert [r.idempotency_key for r in results] == ["oai-call_1", "oai-call_2"]
    assert results[0].receipt.receipt_id != results[1].receipt.receipt_id
    assert len(plane.permit_requests) == 1


async def test_a_resumed_process_reuses_the_persisted_key_and_permit(tmp_path):
    """Simulates a crash after the first attempt: a brand-new runner on the same
    store sends the same key and never asks for a second permit."""
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"

    first_process = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    first_process.register_tool(TOOL, description="Append a note")
    first = await first_process.run(_chat_tool_call("call_abc123"))

    resumed = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    resumed.register_tool(TOOL, description="Append a note")
    second = await resumed.run(_chat_tool_call("call_abc123"))

    assert second.receipt.receipt_id == first.receipt.receipt_id
    assert len(plane.permit_requests) == 1
    assert len(plane.invoke_requests) == 2
    persisted = json.loads(store_path.read_text())
    operation = persisted["operations"]["call_abc123"]
    assert operation["idempotency_key"] == "oai-call_abc123"
    assert operation["permit_id"] == "permit-1"
    assert operation["receipt_id"] == first.receipt.receipt_id

    # The next tool call in the resumed process reuses the recorded permit:
    # no new permit request, so no fresh expires_at under the same key.
    third = await resumed.run(_chat_tool_call("call_def456"))
    assert third.receipt.receipt_id != first.receipt.receipt_id
    assert len(plane.permit_requests) == 1
    assert persisted["permits"][f"oai-permit-run-1-{TOOL}"]["permit_id"] == "permit-1"


async def test_record_is_durable_before_the_first_network_call():
    """A crash between "the model asked" and the permit request must leave a
    record behind so the retry reuses the key. The permit request fails here;
    the record already exists."""

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    store = InMemoryOperationKeyStore()
    client = B2AClient(
        api_key="test-key", base_url="http://trust.test", transport=httpx.MockTransport(failing)
    )
    runner = GovernedToolRunner(client, wallet_id=WALLET, run_id="run-1", key_store=store)
    runner.register_tool(TOOL, description="Append a note")

    with pytest.raises(TransportError):
        await runner.run(_chat_tool_call("call_abc123"))

    record = store.get_operation("call_abc123")
    assert record is not None
    assert record.idempotency_key == "oai-call_abc123"
    assert record.permit_idempotency_key == f"oai-permit-run-1-{TOOL}"
    assert record.permit_id is None and record.receipt_id is None
    permit = store.get_permit(record.permit_idempotency_key)
    assert permit is not None, "the permit request is fixed before it is sent"
    assert permit.expires_at and permit.permit_id is None


async def test_permit_retry_after_crash_resends_the_identical_body(tmp_path):
    """The server hashes the whole permit body. Re-sending a new expires_at
    under the same key would be refused, so the recorded timestamp is reused."""
    calls: list[bytes] = []
    store_path = tmp_path / "operations.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/permits":
            calls.append(request.content)
            persisted = json.loads(store_path.read_text())
            assert persisted["permits"][f"oai-permit-run-1-{TOOL}"][
                "request_payload"
            ] == json.loads(request.content)
            if len(calls) == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(201, json=_permit_payload())
        body = json.loads(request.content)
        key = body["params"]["mcpContext"]["idempotency_key"]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [],
                    "structuredContent": None,
                    "isError": False,
                    "receipt": _receipt_payload(key),
                },
            },
        )

    store = JsonFileOperationKeyStore(store_path)
    client = B2AClient(
        api_key="test-key", base_url="http://trust.test", transport=httpx.MockTransport(handler)
    )
    runner = GovernedToolRunner(client, wallet_id=WALLET, run_id="run-1", key_store=store)
    runner.register_tool(TOOL, description="Append a note")
    with pytest.raises(TransportError):
        await runner.run(_chat_tool_call("call_abc123"))
    resumed = GovernedToolRunner(
        client,
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
        permit_budget=Decimal(900),
        permit_ttl_minutes=120,
    )
    resumed.register_tool(TOOL, description="Append a note")
    await resumed.run(_chat_tool_call("call_abc123"))

    assert len(calls) == 2
    assert calls[0] == calls[1], "the retried permit request must be byte-for-byte the same"
    assert json.loads(calls[1])["max_credits"] == "100"


# ── refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_id", [None, "", "   ", " call_1", "call_1 ", "call\n1", "call_é"])
async def test_tool_call_without_a_usable_id_is_refused_before_any_network_call(bad_id):
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")

    with pytest.raises(ValueError):
        await runner.run(_chat_tool_call(bad_id))  # type: ignore[arg-type]

    assert plane.permit_requests == [] and plane.invoke_requests == []


async def test_over_long_tool_call_id_is_refused_before_any_network_call():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    with pytest.raises(ValueError, match="too long"):
        await runner.run(_chat_tool_call("c" * 125))
    assert plane.invoke_requests == []


def test_responses_api_item_id_is_not_mistaken_for_the_call_id():
    item = {
        "type": "function_call",
        "id": "fc_item_999",  # the item id, not the operation identity
        "call_id": "call_xyz",
        "name": function_name_for(TOOL),
        "arguments": '{"text": "hi"}',
    }
    call = normalize_tool_call(item)
    assert call.id == "call_xyz"
    assert call.arguments == {"text": "hi"}


def test_chat_completions_dict_shape_is_accepted():
    call = normalize_tool_call(
        {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    )
    assert call == normalize_tool_call(
        SimpleNamespace(id="call_1", function=SimpleNamespace(name="f", arguments=None))
    )


def test_arguments_must_be_a_json_object():
    with pytest.raises(TypeError):
        normalize_tool_call({"id": "call_1", "function": {"name": "f", "arguments": "[1, 2]"}})


async def test_a_recorded_tool_call_cannot_be_replayed_as_a_different_tool():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    runner.register_tool(TOOL, description="Append a note")
    runner.register_tool("partner.notes.count", description="Count notes")
    await runner.run(_chat_tool_call("call_abc123"))

    other = SimpleNamespace(
        id="call_abc123",
        function=SimpleNamespace(name=function_name_for("partner.notes.count"), arguments="{}"),
    )
    with pytest.raises(ValueError, match="first recorded"):
        await runner.run(other)


@pytest.mark.parametrize("run_id", ["", "  ", " run-1", "run-1 ", "run\n1", "run-é"])
def test_run_id_must_be_a_stable_printable_identifier(run_id):
    with pytest.raises(ValueError):
        GovernedToolRunner(_client(FakeTrustPlane()), wallet_id=WALLET, run_id=run_id)


# ── OpenAI plumbing ──────────────────────────────────────────────────────────


def test_register_tool_returns_an_openai_function_definition_and_maps_back():
    runner = GovernedToolRunner(_client(FakeTrustPlane()), wallet_id=WALLET, run_id="run-1")
    definition = runner.register_tool(
        TOOL,
        description="Append a note",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    assert definition["type"] == "function"
    assert definition["function"]["name"] == "partner_notes_write"
    assert definition["function"]["parameters"]["properties"]["text"] == {"type": "string"}
    assert runner.tool_name_for("partner_notes_write") == TOOL


def test_register_tool_refuses_two_tools_that_collapse_to_one_function_name():
    runner = GovernedToolRunner(_client(FakeTrustPlane()), wallet_id=WALLET, run_id="run-1")
    runner.register_tool("a.b", description="x")
    with pytest.raises(ValueError, match="already maps"):
        runner.register_tool("a_b", description="y")


async def test_results_render_as_chat_and_responses_outputs():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    runner.register_tool(TOOL, description="Append a note")
    result = await runner.run(_chat_tool_call("call_abc123"))

    message = result.as_tool_message()
    assert message["role"] == "tool" and message["tool_call_id"] == "call_abc123"
    payload = json.loads(message["content"])
    assert payload["receipt"]["receipt_id"] == result.receipt.receipt_id
    assert payload["receipt"]["credits_charged"] == "2"
    assert payload["idempotency_key"] == "oai-call_abc123"

    item = result.as_function_call_output()
    assert item["type"] == "function_call_output" and item["call_id"] == "call_abc123"
    assert json.loads(item["output"]) == payload


def test_permit_budget_is_passed_through():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(
        _client(plane), wallet_id=WALLET, run_id="run-1", permit_budget=Decimal(7)
    )
    assert runner.permit_key_for(TOOL) == f"oai-permit-run-1-{TOOL}"


@pytest.mark.parametrize("name", ["unadvertised_tool", "partner_notes_count", TOOL])
async def test_unregistered_function_is_refused_without_persistence_or_network(tmp_path, name):
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"
    runner = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    runner.register_tool(TOOL, description="Append a note")
    call = _chat_tool_call()
    call.function.name = name
    with pytest.raises(ValueError, match="not registered"):
        await runner.run(call)
    assert not store_path.exists()
    assert plane.permit_requests == [] and plane.invoke_requests == []


@pytest.mark.parametrize(
    "part,value",
    [
        ("call", "call_é"),
        ("call", " call_1"),
        ("call", "call_1 "),
        ("call", "c" * 125),
        ("run", "run-é"),
        ("run", " run-1"),
        ("run", "run-1 "),
        ("run", "r" * 110),
        ("tool", "notes.é"),
        ("tool", " notes.write"),
        ("tool", "notes.write "),
        ("tool", ""),
        ("tool", "   "),
        ("tool", "notes\twrite"),
        ("tool", "t" * 114),
    ],
)
async def test_invalid_key_component_is_refused_before_persistence(tmp_path, part, value):
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"
    with pytest.raises(ValueError):
        runner = GovernedToolRunner(
            _client(plane),
            wallet_id=WALLET,
            run_id=value if part == "run" else "run-1",
            key_store=JsonFileOperationKeyStore(store_path),
        )
        definition = runner.register_tool(value if part == "tool" else TOOL, description="Note")
        call = _chat_tool_call(value if part == "call" else "call_1")
        call.function.name = definition["function"]["name"]
        await runner.run(call)
    assert not store_path.exists()
    assert plane.permit_requests == [] and plane.invoke_requests == []


@pytest.mark.parametrize("stage", ["permit_pending", "invoke_pending", "completed"])
@pytest.mark.parametrize("call_id", ["call_original", "call_next"])
async def test_resume_cannot_change_wallet_at_any_persisted_stage(tmp_path, stage, call_id):
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"

    def handler(request):
        if stage == "permit_pending" or (
            stage == "invoke_pending" and request.url.path == "/mcp/messages"
        ):
            raise httpx.ConnectError("interrupted", request=request)
        return plane(request)

    first = GovernedToolRunner(
        B2AClient(
            api_key="test-key", base_url="http://trust.test", transport=httpx.MockTransport(handler)
        ),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    first.register_tool(TOOL, description="Note")
    if stage == "completed":
        await first.run(_chat_tool_call("call_original"))
    else:
        with pytest.raises(TransportError):
            await first.run(_chat_tool_call("call_original"))

    before = store_path.read_bytes()
    resumed_plane = FakeTrustPlane()
    resumed = GovernedToolRunner(
        _client(resumed_plane),
        wallet_id="wallet-other",
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    resumed.register_tool(TOOL, description="Note")
    with pytest.raises(ValueError, match="wallet"):
        await resumed.run(_chat_tool_call(call_id))
    assert store_path.read_bytes() == before
    assert resumed_plane.permit_requests == [] and resumed_plane.invoke_requests == []


@pytest.mark.parametrize("issued", [False, True])
async def test_legacy_permit_without_snapshot_can_only_reuse_an_issued_permit(issued):
    plane = FakeTrustPlane()
    store = InMemoryOperationKeyStore()
    permit_key = f"oai-permit-run-1-{TOOL}"
    # from_dict exercises records written before snapshot/wallet fields existed.
    store.put_permit(
        PermitRecord.from_dict(
            {
                "permit_idempotency_key": permit_key,
                "tool_name": TOOL,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                "permit_id": "permit-1" if issued else None,
            }
        )
    )
    store.put_operation(
        OperationRecord.from_dict(
            {
                "tool_call_id": "call_legacy",
                "tool_name": TOOL,
                "idempotency_key": "oai-call_legacy",
                "permit_idempotency_key": permit_key,
                "permit_id": "permit-1" if issued else None,
                "receipt_id": None,
            }
        )
    )
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1", key_store=store)
    runner.register_tool(TOOL, description="Note")
    if issued:
        result = await runner.run(_chat_tool_call("call_legacy"))
        assert result.receipt.permit_id == "permit-1"
        assert len(plane.invoke_requests) == 1
    else:
        with pytest.raises(ValueError, match="snapshot"):
            await runner.run(_chat_tool_call("call_legacy"))
        assert plane.invoke_requests == []
    assert plane.permit_requests == []


@pytest.mark.parametrize("wallet_field", ["issuer_wallet_id", "subject_wallet_id"])
async def test_legacy_operation_checks_each_wallet_in_its_permit_snapshot(wallet_field):
    plane = FakeTrustPlane()
    store = InMemoryOperationKeyStore()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1", key_store=store)
    runner.register_tool(TOOL, description="Note")
    await runner.run(_chat_tool_call("call_1"))
    operation = store.get_operation("call_1")
    operation.wallet_id = None  # Legacy operation, with a wallet-bound permit available.
    store.put_operation(operation)
    permit = store.get_permit(operation.permit_idempotency_key)
    permit.request_payload[wallet_field] = "wallet-other"
    store.put_permit(permit)
    before = (len(plane.permit_requests), len(plane.invoke_requests))
    with pytest.raises(ValueError, match="wallet"):
        await runner.run(_chat_tool_call("call_1"))
    assert (len(plane.permit_requests), len(plane.invoke_requests)) == before


async def test_maximum_length_printable_ascii_keys_are_accepted():
    plane = FakeTrustPlane()
    runner = GovernedToolRunner(_client(plane), wallet_id=WALLET, run_id="run-1")
    tool_name = "t" * (128 - len("oai-permit-run-1-"))
    definition = runner.register_tool(tool_name, description="Boundary tool")
    call = _chat_tool_call("c" * 124)
    call.function.name = definition["function"]["name"]
    await runner.run(call)
    assert len(plane.permit_requests[0].headers["idempotency-key"]) == 128
    assert len(plane.invoke_requests[0].headers["idempotency-key"]) == 128


@pytest.mark.parametrize(
    "field,value",
    [
        ("idempotency_key", "oai-call_alternate"),
        ("permit_idempotency_key", f"oai-permit-other-run-{TOOL}"),
        ("tool_call_id", "call_alternate"),
    ],
)
async def test_persisted_operation_identity_must_match_requested_call(tmp_path, field, value):
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"
    runner = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    runner.register_tool(TOOL, description="Note")
    await runner.run(_chat_tool_call("call_1"))
    persisted = json.loads(store_path.read_text())
    persisted["operations"]["call_1"][field] = value
    store_path.write_text(json.dumps(persisted))
    before = store_path.read_bytes()
    plane.permit_requests.clear()
    plane.invoke_requests.clear()

    with pytest.raises(ValueError, match="identity"):
        await runner.run(_chat_tool_call("call_1"))

    assert store_path.read_bytes() == before
    assert plane.permit_requests == [] and plane.invoke_requests == []


async def test_resumed_call_cannot_silently_use_a_different_run_id(tmp_path):
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"
    original = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    original.register_tool(TOOL, description="Note")
    await original.run(_chat_tool_call("call_1"))
    before = store_path.read_bytes()
    plane.permit_requests.clear()
    plane.invoke_requests.clear()
    resumed = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-2",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    resumed.register_tool(TOOL, description="Note")

    with pytest.raises(ValueError, match="identity"):
        await resumed.run(_chat_tool_call("call_1"))

    assert store_path.read_bytes() == before
    assert plane.permit_requests == [] and plane.invoke_requests == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("permit_idempotency_key", "oai-permit-other-run-other_tool"),
        ("tool_name", "other_tool"),
    ],
)
@pytest.mark.parametrize("issued", [False, True])
async def test_loaded_permit_identity_must_match_its_lookup_key_and_tool(
    tmp_path, field, value, issued
):
    plane = FakeTrustPlane()
    store_path = tmp_path / "operations.json"
    runner = GovernedToolRunner(
        _client(plane),
        wallet_id=WALLET,
        run_id="run-1",
        key_store=JsonFileOperationKeyStore(store_path),
    )
    runner.register_tool(TOOL, description="Note")
    await runner.run(_chat_tool_call("call_1"))
    persisted = json.loads(store_path.read_text())
    permit = persisted["permits"][f"oai-permit-run-1-{TOOL}"]
    permit[field] = value
    if not issued:
        permit["permit_id"] = None
    store_path.write_text(json.dumps(persisted))
    before = store_path.read_bytes()
    plane.permit_requests.clear()
    plane.invoke_requests.clear()

    # A new call exercises permit acquisition rather than a completed operation's fast path.
    with pytest.raises(ValueError, match="identity"):
        await runner.run(_chat_tool_call("call_2"))

    assert store_path.read_bytes() == before
    assert plane.permit_requests == [] and plane.invoke_requests == []
