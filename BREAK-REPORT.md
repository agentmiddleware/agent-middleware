# Break report: gateway MCP routers and dispatch

Summary: 1 break found, 1 fixed and tested, 0 fixed but failing, 0 blocked.

The break is in upstream dispatch. The four inbound probes (malformed initialize, tool call without a session, oversized body, unknown method) did not produce a server error or run a tool on a refusal.

## High: a successful paid tool call is discarded when a later tool list fails

What broke: After a partner tool returned a normal success, the gateway sent a second `tools/list` on the same connection. That second request is not part of the paid call. When it failed, the gateway threw away the tool answer it already had.

Three local reproductions, all against a scripted upstream (no live partner, no charge endpoint):

- `tools/list` answered "method not found" (`-32601`). The adapter raised `UpstreamMcpDeliveryUncertainError`.
- `tools/list` returned an output schema the tool answer did not match. The adapter raised `UpstreamMcpDeliveryUncertainError`.
- `tools/list` was larger than the response cap. The adapter raised `UpstreamMcpResponseRejectedError` (`upstream_response_too_large`).

In each case the caller did not receive the tool text `paid-result`. The router treats both of those errors as charged outcomes, not refunds, so the caller pays and still gets a failure after the partner tool has already run.

How to reproduce: from `gateway/`, run `tests/test_mcp_dispatch_breaks.py::test_paid_tool_call_ignores_a_hostile_follow_up_list` on the code before the cache pin. The scripted server returns `paid-result` for `tools/call`, then answers `tools/list` with one of the three hostile bodies above. The call must keep `paid-result` and must not send `tools/list` after `tools/call`.

Root cause: `gateway/app/services/upstream_mcp.py` `_call_tool_once` (the `session.call_tool` call, now line 972). Every dispatch opens a new MCP `ClientSession` and never fills that session's output-schema cache. The installed client helper `ClientSession.call_tool` (`mcp/client/session.py` lines 411-421) calls `list_tools` when the tool name is missing from `_tool_output_schemas`, then may raise `RuntimeError` if the fresh list carries a schema the body does not match. Those exceptions are raised after the tool response has already arrived, and the adapter reports them as delivery-uncertain or response-rejected.

Fix: Before the dispatch claim, if the session has that cache dict, set this tool's entry to `None`. The helper then returns the `tools/call` body and does not send `tools/list`. The gateway still canonicalizes that body the same way as before (size cap, secret check, canonical JSON).

Test results: On the code before this pin, the same test file failed all three cases (`delivery_uncertain`, `delivery_uncertain`, `upstream_response_too_large`). After the pin, `tests/test_mcp_dispatch_breaks.py`, `tests/test_mcp_router_breaks.py`, and `tests/test_upstream_mcp.py` passed: 101 tests. A later run of the two new files passed: 38 tests. Ruff check and format check passed on the three touched Python files.

## Probes that held

Malformed initialize on `POST /mcp`: empty body, truncated JSON, non-UTF-8, `NaN`, null or non-string protocol version, missing or non-object params, missing or odd `id`, and a wrong `jsonrpc` version. Each stayed under HTTP 500 and did not run the fixture tool. Covered by `test_malformed_initialize_is_not_a_server_error`.

Tool call without a session on `POST /mcp`: a wallet key and no `Mcp-Session-Id` runs the tool and the receipt wallet is the caller's. A forged `Mcp-Session-Id` does not change that wallet and does not skip the API key (missing key is HTTP 401). A missing, boolean, array, or object `id` does not run the tool. The outbound adapter still forwards the `mcp-session-id` from initialize onto `tools/call`. Covered by `test_tool_call_without_session_does_not_skip_auth_or_crash` and the dispatch test's session header check.

Oversized body on `POST /mcp`: a body over `MAX_REQUEST_BODY_BYTES` (1 MiB) is HTTP 413 and does not run the tool. An empty `Content-Length` with that same body is a 4xx and does not run the tool. Covered by `test_oversized_standard_mcp_body_is_refused_before_the_tool`.

Unknown method on `POST /mcp`: `resources/list`, `prompts/list`, and `logging/setLevel` return `-32601` and do not run a tool. `logging/setLevel` does not change the process log level because this server does not register that handler. `notifications/initialized` is HTTP 202. A non-string method stays under HTTP 500. Covered by `test_unknown_method_is_not_a_server_error` and `test_non_string_method_is_not_a_server_error`.

## Open questions

Legacy `POST /mcp/messages` still accepts a `tools/call` whose JSON-RPC `id` is missing or null and will run it. The standard `POST /mcp` path treats that shape as a notification and does not run the tool. I did not change the legacy route. Whether callers still omit `id` is a compatibility decision, and this pass did not execute that legacy call.

Methods the MCP SDK does not model (`tools/execute`, `INITIALIZE`) come back as `-32602` invalid params, not `-32601` method not found. The tool does not run. I left that as the SDK's answer.

A JSON-RPC error on the `tools/call` request itself, after the dispatch claim, is still delivery-uncertain and charged. This fix does not refund that case. The claim is already taken, and the send may have reached the partner.

The pin uses the client's `_tool_output_schemas` attribute. The new test drives the real client, so a library upgrade that removes the attribute will fail the test. I did not edit the installed package.

The discovered output schema is still not checked against the returned body. The gateway's own result canonicalizer never did that check. Enforcing it would be a new product rule, and a mismatch must not be reported as delivery-uncertain.

Partner MCP (`gateway/app/partner_mcp.py`) is a separate app. I did not probe it. The full gateway suite was not run.
