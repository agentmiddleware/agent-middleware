"""Stable idempotency keys in the framework ``governed_tool`` wrapper.

The wrapper must derive the key from the stable identity of the logical
tool call (permit id, tool name, canonical arguments, caller action id),
never a fresh random value per invocation, so an ordinary retry of the
same tool call reuses the key and replays instead of billing again.

These tests use a fake session (no server, no database): the key choice
happens in the wrapper before ``session.invoke`` is called.
"""

from typing import Any

import pytest
from framework_integrations._governed import (
    derive_governed_idempotency_key,
    governed_tool,
)


class _FakeResult:
    def __init__(self) -> None:
        self.receipt = object()
        self.structured_content = {"echo": True}
        self.content = [{"type": "text", "text": "echo"}]


class _FakeSession:
    """Records the idempotency key of every governed invocation."""

    permit_id = "permit-test-1"
    wallet_id = "wallet-test-1"

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
        estimated_credits=None,
    ) -> _FakeResult:
        self.keys.append(idempotency_key)
        self.calls.append((name, dict(arguments)))
        return _FakeResult()


def _echo_tool(session: _FakeSession):
    @governed_tool(session, tool_name="echo-tool")
    async def echo(message: str) -> dict[str, Any]:
        raise AssertionError("stub body must never run locally")

    return echo


def test_derive_helper_is_deterministic_and_bounded():
    """Same inputs give the same short key twice."""
    first = derive_governed_idempotency_key(
        permit_id="permit-1", tool_name="echo", arguments={"b": 2, "a": 1}
    )
    second = derive_governed_idempotency_key(
        permit_id="permit-1", tool_name="echo", arguments={"a": 1, "b": 2}
    )
    assert first == second
    assert first.startswith("gov-")
    assert len(first) <= 128


def test_derive_helper_separates_distinct_actions():
    """Different tool, args, permit, or action id give different keys."""
    base = {"permit_id": "permit-1", "tool_name": "echo", "arguments": {"a": 1}}
    assert derive_governed_idempotency_key(**base) != derive_governed_idempotency_key(
        **{**base, "tool_name": "other"}
    )
    assert derive_governed_idempotency_key(**base) != derive_governed_idempotency_key(
        **{**base, "arguments": {"a": 2}}
    )
    assert derive_governed_idempotency_key(**base) != derive_governed_idempotency_key(
        **{**base, "permit_id": "permit-2"}
    )
    assert derive_governed_idempotency_key(**base) != derive_governed_idempotency_key(
        **{**base, "action_id": "call-1"}
    )


def test_derive_helper_rejects_blank_inputs():
    """Blank permit, tool, or action id fail closed before any dispatch."""
    with pytest.raises(ValueError, match="permit_id"):
        derive_governed_idempotency_key(permit_id="  ", tool_name="echo", arguments={})
    with pytest.raises(ValueError, match="tool_name"):
        derive_governed_idempotency_key(
            permit_id="permit-1", tool_name="", arguments={}
        )
    with pytest.raises(ValueError, match="action_id"):
        derive_governed_idempotency_key(
            permit_id="permit-1",
            tool_name="echo",
            arguments={},
            action_id="   ",
        )


async def test_wrapper_retry_of_same_tool_call_reuses_key():
    """Retrying the same logical call sends the same derived key twice."""
    session = _FakeSession()
    echo = _echo_tool(session)

    await echo(message="hello")
    await echo(message="hello")

    assert len(session.keys) == 2
    assert session.keys[0] == session.keys[1]
    assert session.keys[0] == derive_governed_idempotency_key(
        permit_id="permit-test-1",
        tool_name="echo-tool",
        arguments={"message": "hello"},
    )


async def test_wrapper_distinct_arguments_get_distinct_keys():
    """Two different actions get different keys: no accidental replay."""
    session = _FakeSession()
    echo = _echo_tool(session)

    await echo(message="hello")
    await echo(message="goodbye")

    assert len(session.keys) == 2
    assert session.keys[0] != session.keys[1]


async def test_wrapper_action_id_disambiguates_identical_arguments():
    """Identical arguments with different action ids bill separately."""
    session = _FakeSession()
    echo = _echo_tool(session)

    await echo(message="hello", action_id="framework-call-1")
    await echo(message="hello", action_id="framework-call-2")
    await echo(message="hello", action_id="framework-call-1")

    assert len(session.keys) == 3
    assert session.keys[0] != session.keys[1]
    assert session.keys[0] == session.keys[2]


async def test_wrapper_explicit_key_wins_over_derivation():
    """A caller-supplied key is used as-is, even with an action id present."""
    session = _FakeSession()
    echo = _echo_tool(session)

    await echo(message="hello", idempotency_key="caller-key-1", action_id="call-9")

    assert session.keys == ["caller-key-1"]


async def test_wrapper_rejects_blank_key_and_blank_action_id():
    """Blank key material is a caller bug: reject before dispatch."""
    session = _FakeSession()
    echo = _echo_tool(session)

    with pytest.raises(ValueError, match="idempotency_key"):
        await echo(message="hello", idempotency_key="   ")
    with pytest.raises(ValueError, match="action_id"):
        await echo(message="hello", action_id="  ")
    assert session.keys == []


async def test_wrapper_coerces_non_string_action_id():
    """A numeric framework run id still derives a stable key."""
    session = _FakeSession()
    echo = _echo_tool(session)

    await echo(message="hello", action_id=7)
    await echo(message="hello", action_id=7)

    assert len(session.keys) == 2
    assert session.keys[0] == session.keys[1]
