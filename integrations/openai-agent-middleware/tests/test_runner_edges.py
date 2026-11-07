"""Edge cases for the OpenAI runner: names, arguments and stored state."""

import json

import pytest

from openai_b2a.runner import function_name_for, normalize_tool_call


def test_function_name_for_refuses_a_blank_tool_name():
    with pytest.raises(ValueError, match="non-blank"):
        function_name_for("   ")


def test_function_name_for_sanitizes_and_truncates():
    assert function_name_for("partner.notes.write") == "partner_notes_write"
    assert function_name_for("héllo world!") == "h_llo_world_"
    assert len(function_name_for("t" * 100)) == 64


def test_tool_call_arguments_must_be_valid_json():
    with pytest.raises(ValueError):
        normalize_tool_call({"id": "call_1", "function": {"name": "f", "arguments": "{bad json"}})


def test_corrupt_key_store_fails_closed_without_network(tmp_path):
    from openai_b2a import JsonFileOperationKeyStore

    store_path = tmp_path / "operations.json"
    store_path.write_text("{bad json")
    with pytest.raises(ValueError):
        JsonFileOperationKeyStore(store_path).get_operation("call_1")
    store_path.write_text("[1, 2]")
    with pytest.raises(TypeError, match="JSON object"):
        JsonFileOperationKeyStore(store_path).get_operation("call_1")


def test_run_id_must_be_a_string():
    from openai_b2a import GovernedToolRunner

    with pytest.raises(TypeError, match="run_id"):
        GovernedToolRunner(None, wallet_id="w", run_id=123)  # type: ignore[arg-type]


def test_output_payload_carries_receipt_and_operation_key():
    from types import SimpleNamespace

    from openai_b2a.runner import GovernedToolResult

    receipt = SimpleNamespace(
        receipt_id="rcpt-1",
        outcome="success",
        credits_charged="2",
        signature="sig",
        signature_key_id="kid",
    )
    result = GovernedToolResult(
        tool_call_id="call_1",
        tool_name="tool",
        idempotency_key="oai-call_1",
        result=SimpleNamespace(content=[], structured_content=None, receipt=receipt),  # type: ignore[arg-type]
    )
    payload = result.output_payload()
    assert payload["idempotency_key"] == "oai-call_1"
    assert payload["receipt"]["receipt_id"] == "rcpt-1"
    message = result.as_tool_message()
    assert message["tool_call_id"] == "call_1"
    assert json.loads(message["content"]) == payload
    assert json.loads(result.as_function_call_output()["output"]) == payload
