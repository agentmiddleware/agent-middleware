"""Fail-closed instrumentation: the fault layer must not invent identifiers.

The layer stands between the gateway and the tool, so anything it records
can end up quoted as an observation. A non-string ``operation_id`` coerced
with ``str()`` would read as a real identifier while matching no fault plan,
and an oversized body must be refused as 413 without touching the tool,
never raise out of the middleware as an unclassifiable transport error.
"""

from __future__ import annotations

import asyncio
import json

from failure_lab.faults import (
    FaultInjectionMiddleware,
    FaultInjector,
    classify_request,
)


def _tools_call(operation_id):
    return json.dumps(
        {
            "method": "tools/call",
            "id": 1,
            "params": {"arguments": {"operation_id": operation_id}},
        }
    ).encode()


def test_non_string_operation_ids_stay_unidentified():
    """A dict, number or bool operation id is malformed, not a name."""
    for bad in ({"nested": True}, 42, True, ["op-1"]):
        kind, operation_id, *_ = classify_request("/mcp", _tools_call(bad), {})
        assert kind == "tools_call"
        assert operation_id is None


def test_rest_non_string_operation_ids_stay_unidentified():
    body = json.dumps({"operation_id": {"nested": True}}).encode()
    kind, operation_id, *_ = classify_request("/refunds", body, {})
    assert kind == "refund_rest"
    assert operation_id is None


def test_string_operation_ids_still_classify():
    kind, operation_id, *_ = classify_request("/mcp", _tools_call("op-1"), {})
    assert (kind, operation_id) == ("tools_call", "op-1")
    body = json.dumps({"operation_id": "op-2"}).encode()
    kind, operation_id, *_ = classify_request("/refunds", body, {})
    assert (kind, operation_id) == ("refund_rest", "op-2")


def test_oversized_body_is_refused_as_413_without_reaching_the_tool():
    """Over the buffer limit: 413, no tool contact, no raise, recorded."""
    from failure_lab.faults import _MAX_BODY_BYTES

    reached_tool = []

    async def inner(scope, receive, send):
        reached_tool.append(1)
        raise AssertionError("the tool must not run for an oversized body")

    injector = FaultInjector()
    middleware = FaultInjectionMiddleware(inner, injector)

    big = b"x" * (_MAX_BODY_BYTES + 1)
    chunks = [big, b""]

    async def receive_chunks():
        if chunks:
            chunk = chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(chunks),
            }
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
    }
    asyncio.run(middleware(scope, receive_chunks, send))

    assert reached_tool == []
    starts = [m for m in messages if m["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 413
    crossings = injector.crossings(kinds=("unclassified",))
    assert len(crossings) == 1
    assert crossings[0].reached_tool is False
    assert crossings[0].delivered is True
    assert crossings[0].fault == "oversized_body_rejected"
    # The refusal is not a dispatch: execution-counting views ignore it.
    assert injector.dispatch_count() == 0
