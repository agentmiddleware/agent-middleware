"""Test 14 -- Standard MCP client."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx

from failure_lab.configurations import (
    GATEWAY_CONFIGURATIONS,
    AttemptOutcome,
    Configuration,
    Target,
)
from failure_lab.gateway import (
    GATEWAY_BASE_URL,
    GATEWAY_TOOL_ID,
    GatewaySnapshot,
    GatewayUnderTest,
)
from failure_lab.identity import OperationIdentity
from failure_lab.scenarios.base import (
    ConfigurationResult,
    EventLog,
    Measurements,
    Scenario,
    Timing,
    Verdict,
)

#: The standards-compliant transport the reference SDK speaks here.
STANDARD_ENDPOINT = "POST /mcp"
#: The project-specific compatibility transport every other scenario drives.
COMPATIBILITY_ENDPOINT = "POST /mcp/messages"

#: Where the standard endpoint publishes the signed receipt on a tools/call
#: result: the spec's own extension point, so an SDK client reaches it without
#: knowing anything about this product.
RECEIPT_META_KEY = "io.agentmiddleware/receipt"

#: A tool id the gateway does not serve, used for the not-found probe.
UNKNOWN_TOOL = "lab.t14.no.such.tool"

#: Refund amount, in minor units, for every call here.
PROBE_AMOUNT = 5000

#: Client patience for one tools/call. Nothing here should be slow; this only
#: keeps the harness from being the thing that hangs.
CALL_TIMEOUT_SECONDS = 30.0

#: The steps the scenario reports, in the order the specification names them.
STEPS: tuple[str, ...] = (
    "initialize",
    "capability_negotiation",
    "authentication",
    "tools_list",
    "tools_call",
    "error_handling",
    "retry",
)


def _receipt_from_meta(meta: Any) -> dict[str, Any] | None:
    """The signed receipt an SDK client reads off a CallToolResult."""
    if not isinstance(meta, dict):
        return None
    receipt = meta.get(RECEIPT_META_KEY)
    return receipt if isinstance(receipt, dict) else None


def _jsonrpc_error(exc: BaseException) -> dict[str, Any]:
    """Describe a JSON-RPC error the SDK surfaced as an exception."""
    error = getattr(exc, "error", None)
    data = getattr(error, "data", None)
    receipt = data.get("receipt") if isinstance(data, dict) else None
    return {
        "exception": type(exc).__name__,
        "jsonrpc_code": getattr(error, "code", None),
        "message": getattr(error, "message", None) or str(exc),
        "receipt_id": receipt.get("receipt_id") if isinstance(receipt, dict) else None,
        "receipt_outcome": receipt.get("outcome")
        if isinstance(receipt, dict)
        else None,
        "credits_charged": (
            str(receipt.get("credits_charged")) if isinstance(receipt, dict) else None
        ),
    }


def _http_statuses(exc: BaseException) -> list[int]:
    """Every HTTP status code carried by an exception or its nested group."""
    found: list[int] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            found.append(status)
        cause = current.__cause__
        if isinstance(cause, BaseException):
            pending.append(cause)
    return found


def _leaf_reasons(exc: BaseException) -> list[str]:
    """The concrete exceptions inside a (possibly nested) exception group."""
    reasons: list[str] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
            continue
        text = " ".join(str(current).split())
        reasons.append(f"{type(current).__name__}: {text[:200]}")
    return reasons


def _movement(before: GatewaySnapshot, after: GatewaySnapshot) -> dict[str, int]:
    """Gateway-reported change between two snapshots of the same wallet."""
    return {
        "sent_attempts": after.sent_attempt_count - before.sent_attempt_count,
        "debits": after.debit_count - before.debit_count,
        "refunds": after.refund_count - before.refund_count,
        "net_debits": after.net_debit_count - before.net_debit_count,
        "receipts": after.receipt_count - before.receipt_count,
    }


class StandardMcpClient(Scenario):
    """Drive the standards-compliant transport with somebody else's client.

    The client here is the official MCP SDK (``mcp.ClientSession`` over
    ``mcp.client.streamable_http``). The product's own SDK is deliberately NOT
    used: a vendor SDK talking to its own server proves interoperability with
    itself.

    WHAT TO IMPLEMENT
    -----------------
    Use ``gateway.standard_endpoint_enabled()`` and
    ``gateway.mcp_client_session(tenant, idempotency_key=...)``.

    Steps, each recorded in ``extra["steps"]`` with a pass/fail and detail:

    ``initialize``            session.initialize() succeeds and negotiates a
                              protocol version; record the version.
    ``capability_negotiation`` the initialize result advertises tools.
    ``authentication``        a session with no API key is refused. Open a
                              second ``mcp_client_session``-style client with
                              the auth header stripped and record the refusal.
                              An exception during initialize IS the refusal;
                              catch it and record it rather than letting it
                              fail the run.
    ``tools_list``            the refund tool appears in ``list_tools()``.
    ``tools_call``            calling it settles, returns a receipt under
                              ``_meta["io.agentmiddleware/receipt"]``, and
                              produces exactly one downstream execution.
    ``error_handling``        a call to a nonexistent tool, and a call with a
                              malformed argument, both produce structured
                              errors rather than a transport break, and
                              neither produces a downstream effect.
    ``retry``                 a second session using the SAME
                              ``Idempotency-Key`` header re-issues the same
                              call and gets the SAME receipt id back, with no
                              second execution and no second debit.

    Also record, in ``extra["transports"]``, that the standards-compliant
    endpoint (``POST /mcp``, exercised here by the SDK) was tested separately
    from the project-specific compatibility endpoint (``POST /mcp/messages``,
    exercised by every other scenario). Drive one governed call over
    ``/mcp/messages`` here too, using a distinct key, purely to record that
    both transports were covered by this run.

    Verdict: ``PASS`` iff every step passes. ``FAIL`` otherwise, naming the
    step.
    """

    test_id = "T14"
    title = "Standard MCP client"
    claim = (
        "An independent, standards-compliant MCP client completes the full "
        "lifecycle against the standard endpoint, including an idempotent "
        "retry that does not execute twice."
    )
    tier = "fast"
    configurations = GATEWAY_CONFIGURATIONS
    inapplicable_reason = "a direct integration exposes no MCP endpoint of its own here"
    expected = {
        Configuration.DIRECT_NAIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.DIRECT_NATIVE.value: Verdict.NOT_APPLICABLE.value,
        Configuration.GATEWAY_NATIVE.value: Verdict.PASS.value,
        Configuration.GATEWAY_NAIVE.value: Verdict.PASS.value,
    }
    limitations = (
        "One client implementation (the reference SDK) over one transport "
        "(Streamable HTTP, JSON mode). Not a conformance suite.",
        "The standard endpoint mints a permit server-side, so this exercises "
        "that path rather than a caller-supplied permit.",
    )

    async def run_configuration(
        self, target: Target, log: EventLog
    ) -> ConfigurationResult:
        from mcp.shared.exceptions import McpError

        gateway, tenant, _ = target.require_gateway()
        configuration = target.configuration.value
        steps: dict[str, dict[str, Any]] = {}

        def record(step: str, passed: bool, detail: str, **data: Any) -> None:
            steps[step] = {
                # ``case`` is the suite-wide name for a sub-case row, so one
                # report can key every scenario's table on the same field;
                # ``step`` is this scenario's own name and is kept.
                "case": step,
                "step": step,
                "passed": bool(passed),
                "detail": detail,
                **data,
            }
            log.emit(
                f"t14.{step}",
                f"{configuration}: {step} -> {'pass' if passed else 'FAIL'}: {detail}",
                scenario=self.test_id,
                configuration=configuration,
                passed=bool(passed),
                **data,
            )

        call_key = f"t14-standard-{uuid.uuid4().hex}"
        op_call, refund_call = self.refund("pay_t14_standard", amount=PROBE_AMOUNT)
        op_malformed, refund_malformed = self.refund(
            "pay_t14_malformed", amount=PROBE_AMOUNT
        )
        op_unknown, refund_unknown = self.refund("pay_t14_unknown", amount=PROBE_AMOUNT)
        op_compat, refund_compat = self.refund("pay_t14_compat", amount=PROBE_AMOUNT)

        log.emit(
            "t14.start",
            f"{configuration}: driving {STANDARD_ENDPOINT} with the reference "
            f"MCP SDK client, then one governed call over "
            f"{COMPATIBILITY_ENDPOINT} for transport coverage",
            scenario=self.test_id,
            configuration=configuration,
            tool=GATEWAY_TOOL_ID,
            standard_idempotency_key=call_key,
        )

        attempts: list[AttemptOutcome] = []
        standard_calls: list[dict[str, Any]] = []

        with gateway.standard_endpoint_enabled():
            # -- lifecycle, discovery and error handling, one session -----
            unknown_probe: dict[str, Any] = {}
            malformed_probe: dict[str, Any] = {}
            executions_before_errors = target.ledger.execution_count()
            crossings_before_errors = len(target.injector.crossings())
            snap_before_errors = await gateway.snapshot(tenant)

            async with gateway.mcp_client_session(
                tenant, idempotency_key=None
            ) as session:
                init = await session.initialize()
                negotiated = getattr(init, "protocolVersion", None)
                protocol_version = str(negotiated) if negotiated else ""
                server_info = init.serverInfo
                record(
                    "initialize",
                    bool(negotiated),
                    (
                        f"the reference SDK client initialized against "
                        f"{STANDARD_ENDPOINT} and negotiated protocol version "
                        f"{protocol_version}"
                        if negotiated
                        else f"the reference SDK client reached {STANDARD_ENDPOINT} "
                        "but the initialize result carried no protocol version"
                    ),
                    protocol_version=protocol_version,
                    server_name=getattr(server_info, "name", None),
                    server_version=getattr(server_info, "version", None),
                )

                tools_capability = getattr(init.capabilities, "tools", None)
                record(
                    "capability_negotiation",
                    tools_capability is not None,
                    (
                        "the initialize result advertises the tools capability "
                        f"({tools_capability})"
                        if tools_capability is not None
                        else "the initialize result advertises no tools capability"
                    ),
                    tools_capability=(
                        None if tools_capability is None else str(tools_capability)
                    ),
                    instructions_present=bool(init.instructions),
                )

                listed = await session.list_tools()
                tool_names = [tool.name for tool in listed.tools]
                record(
                    "tools_list",
                    GATEWAY_TOOL_ID in tool_names,
                    f"tools/list returned {tool_names}; the governed refund tool "
                    f"{GATEWAY_TOOL_ID} is "
                    f"{'present' if GATEWAY_TOOL_ID in tool_names else 'ABSENT'}",
                    tools=tool_names,
                )

                # -- probe 1: a tool the gateway does not serve -----------
                timing = Timing.start()
                try:
                    unknown_result = await session.call_tool(
                        UNKNOWN_TOOL,
                        refund_unknown.as_dict(),
                        read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS),
                    )
                except McpError as exc:
                    unknown_probe = {
                        "shape": "jsonrpc_error",
                        **_jsonrpc_error(exc),
                    }
                else:
                    unknown_probe = {
                        "shape": "tool_result",
                        "is_error": bool(unknown_result.isError),
                        "message": str(unknown_result.content)[:300],
                    }
                unknown_latency_ms = timing.elapsed_ms
                log.emit(
                    "t14.error_probe",
                    f"{configuration}: call to the unserved tool {UNKNOWN_TOOL} "
                    f"answered {unknown_probe}",
                    scenario=self.test_id,
                    configuration=configuration,
                    probe="unknown_tool",
                    result=unknown_probe,
                )

                # -- probe 2: the governed tool, malformed argument -------
                malformed_arguments = dict(refund_malformed.as_dict())
                malformed_arguments["amount"] = "not-a-number"
                timing = Timing.start()
                try:
                    malformed_result = await session.call_tool(
                        GATEWAY_TOOL_ID,
                        malformed_arguments,
                        read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS),
                    )
                except McpError as exc:
                    malformed_probe = {
                        "shape": "jsonrpc_error",
                        **_jsonrpc_error(exc),
                    }
                else:
                    malformed_probe = {
                        "shape": "tool_result",
                        "is_error": bool(malformed_result.isError),
                        "message": str(malformed_result.content)[:300],
                    }
                malformed_latency_ms = timing.elapsed_ms
                log.emit(
                    "t14.error_probe",
                    f"{configuration}: call with a malformed amount answered "
                    f"{malformed_probe}",
                    scenario=self.test_id,
                    configuration=configuration,
                    probe="malformed_argument",
                    sent_amount=malformed_arguments["amount"],
                    result=malformed_probe,
                )

                # The transport survived both errors if the same session can
                # still speak the protocol afterwards.
                after_errors = await session.list_tools()
                session_survived = GATEWAY_TOOL_ID in [
                    tool.name for tool in after_errors.tools
                ]

            executions_from_errors = (
                target.ledger.execution_count() - executions_before_errors
            )
            crossings_from_errors = (
                len(target.injector.crossings()) - crossings_before_errors
            )
            snap_after_errors = await gateway.snapshot(tenant)
            error_movement = _movement(snap_before_errors, snap_after_errors)

            structured_errors = [
                probe.get("shape") == "jsonrpc_error" or bool(probe.get("is_error"))
                for probe in (unknown_probe, malformed_probe)
            ]
            error_handling_ok = (
                all(structured_errors)
                and session_survived
                and executions_from_errors == 0
            )
            record(
                "error_handling",
                error_handling_ok,
                (
                    "the unserved tool answered "
                    f"{unknown_probe.get('message')!r} "
                    f"(code {unknown_probe.get('jsonrpc_code')}) and the "
                    "malformed argument answered "
                    f"{malformed_probe.get('message')!r} "
                    f"(code {malformed_probe.get('jsonrpc_code')}); the session "
                    f"{'still spoke the protocol afterwards' if session_survived else 'could not be used afterwards'}; "
                    f"downstream executions added by the two probes: "
                    f"{executions_from_errors} (effect ledger); downstream "
                    f"requests reaching the upstream: {crossings_from_errors} "
                    "(fault layer)"
                ),
                unknown_tool_probe=unknown_probe,
                malformed_argument_probe=malformed_probe,
                session_usable_after_errors=session_survived,
                downstream_executions_added=executions_from_errors,
                downstream_requests_added=crossings_from_errors,
                gateway_movement=error_movement,
            )

            attempts.append(
                self._standard_attempt(
                    target,
                    business_operation_id=op_unknown,
                    idempotency_key="(generated by the gateway)",
                    status="rejected",
                    client_visible_state="confirmed_rejected",
                    latency_ms=unknown_latency_ms,
                    reason=str(unknown_probe.get("message"))[:200],
                    details={"probe": "unknown_tool", **unknown_probe},
                )
            )
            attempts.append(
                self._standard_attempt(
                    target,
                    business_operation_id=op_malformed,
                    idempotency_key="(generated by the gateway)",
                    status=str(malformed_probe.get("receipt_outcome") or "rejected"),
                    client_visible_state="confirmed_rejected",
                    latency_ms=malformed_latency_ms,
                    reason=str(malformed_probe.get("message"))[:200],
                    details={"probe": "malformed_argument", **malformed_probe},
                )
            )

            # -- a client with no credentials at all ----------------------
            # The control runs the SAME construction WITH credentials. Without
            # it, "refused" is indistinguishable from "the probe is broken":
            # any exception at all -- a wrong URL, a bad argument, an import
            # error -- would otherwise be recorded as an authentication
            # refusal and pass the step.
            credentialed = await self._initialize_probe(
                gateway, tenant, with_credentials=True
            )
            unauthenticated = await self._initialize_probe(
                gateway, tenant, with_credentials=False
            )
            auth_refused = not unauthenticated["initialized"]
            control_worked = bool(credentialed["initialized"])
            if not control_worked:
                auth_detail = (
                    "INCONCLUSIVE: the control -- the same client construction "
                    "WITH the API key -- did not initialize either "
                    f"({credentialed['detail']}), so the credential-less "
                    f"outcome ({unauthenticated['detail']}) is not evidence of "
                    "a refusal"
                )
            elif auth_refused:
                auth_detail = (
                    "a client with the auth header stripped was refused at "
                    f"initialize: {unauthenticated['detail']}; the identical "
                    "construction WITH the API key initialized (control), so "
                    "the refusal is attributable to the missing credential"
                )
            else:
                auth_detail = (
                    "a client with NO API key completed initialize against "
                    f"{STANDARD_ENDPOINT}: {unauthenticated['detail']}"
                )
            record(
                "authentication",
                control_worked and auth_refused,
                auth_detail,
                unauthenticated_initialized=unauthenticated["initialized"],
                unauthenticated_refusal=unauthenticated["detail"],
                unauthenticated_http_statuses=unauthenticated["http_statuses"],
                unauthenticated_exception=unauthenticated["exception"],
                credentialed_control_initialized=control_worked,
                credentialed_control_detail=credentialed["detail"],
                refusal_status_is_credential_denial=any(
                    status in (401, 403) for status in unauthenticated["http_statuses"]
                ),
            )

            # -- the governed call, over the standard transport -----------
            snap_before_call = await gateway.snapshot(tenant)
            timing = Timing.start()
            async with gateway.mcp_client_session(
                tenant, idempotency_key=call_key
            ) as session:
                await session.initialize()
                call_result = await session.call_tool(
                    GATEWAY_TOOL_ID,
                    refund_call.as_dict(),
                    read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS),
                )
            call_latency_ms = timing.elapsed_ms
            snap_after_call = await gateway.snapshot(tenant)
            call_movement = _movement(snap_before_call, snap_after_call)
            call_receipt = _receipt_from_meta(call_result.meta)
            call_executions = target.ledger.execution_count(op_call)

            call_ok = (
                not call_result.isError
                and call_receipt is not None
                and bool(call_receipt.get("receipt_id"))
                and call_receipt.get("outcome") == "success"
                and call_executions == 1
                and call_movement["debits"] == 1
                and call_movement["receipts"] == 1
            )
            record(
                "tools_call",
                call_ok,
                (
                    f"the SDK client's tools/call settled with outcome "
                    f"{(call_receipt or {}).get('outcome')!r}, carried receipt "
                    f"{(call_receipt or {}).get('receipt_id')!r} under "
                    f'_meta["{RECEIPT_META_KEY}"], and the independent effect '
                    f"ledger recorded {call_executions} downstream execution(s) "
                    f"for {op_call}; the gateway recorded "
                    f"{call_movement['debits']} debit(s) and "
                    f"{call_movement['receipts']} receipt(s)"
                ),
                is_error=bool(call_result.isError),
                receipt_id=(call_receipt or {}).get("receipt_id"),
                receipt_outcome=(call_receipt or {}).get("outcome"),
                credits_charged=str((call_receipt or {}).get("credits_charged")),
                receipt_in_meta=call_receipt is not None,
                downstream_executions=call_executions,
                gateway_movement=call_movement,
                structured_content=call_result.structuredContent,
            )
            standard_calls.append(
                {
                    "purpose": "tools_call",
                    "idempotency_key": call_key,
                    "receipt_id": (call_receipt or {}).get("receipt_id"),
                    "downstream_executions": call_executions,
                }
            )
            attempts.append(
                self._standard_attempt(
                    target,
                    business_operation_id=op_call,
                    idempotency_key=call_key,
                    status="success" if not call_result.isError else "error",
                    client_visible_state=(
                        "confirmed_success"
                        if not call_result.isError
                        else "no_information"
                    ),
                    latency_ms=call_latency_ms,
                    receipt=call_receipt,
                    refund=(
                        dict(call_result.structuredContent)
                        if isinstance(call_result.structuredContent, dict)
                        else None
                    ),
                    details={"probe": "tools_call"},
                )
            )

            # -- the retry: new session, same Idempotency-Key -------------
            timing = Timing.start()
            async with gateway.mcp_client_session(
                tenant, idempotency_key=call_key
            ) as session:
                await session.initialize()
                retry_result = await session.call_tool(
                    GATEWAY_TOOL_ID,
                    refund_call.as_dict(),
                    read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT_SECONDS),
                )
            retry_latency_ms = timing.elapsed_ms
            snap_after_retry = await gateway.snapshot(tenant)
            retry_movement = _movement(snap_after_call, snap_after_retry)
            retry_receipt = _receipt_from_meta(retry_result.meta)
            retry_executions = target.ledger.execution_count(op_call)
            first_receipt_id = (call_receipt or {}).get("receipt_id")
            retry_receipt_id = (retry_receipt or {}).get("receipt_id")

            retry_ok = (
                not retry_result.isError
                and retry_receipt_id is not None
                and retry_receipt_id == first_receipt_id
                and retry_executions == call_executions
                and retry_movement["debits"] == 0
                and retry_movement["net_debits"] == 0
                and retry_movement["receipts"] == 0
                and retry_movement["sent_attempts"] == 0
            )
            record(
                "retry",
                retry_ok,
                (
                    f"a second SDK session presenting the same Idempotency-Key "
                    f"got receipt {retry_receipt_id!r} back (the first call's "
                    f"receipt was {first_receipt_id!r}); downstream executions "
                    f"for {op_call} stayed at {retry_executions}; the gateway "
                    f"added {retry_movement['debits']} debit(s), "
                    f"{retry_movement['sent_attempts']} sent attempt(s) and "
                    f"{retry_movement['receipts']} receipt(s)"
                ),
                is_error=bool(retry_result.isError),
                receipt_id=retry_receipt_id,
                same_receipt_id=retry_receipt_id == first_receipt_id,
                downstream_executions=retry_executions,
                gateway_movement=retry_movement,
            )
            standard_calls.append(
                {
                    "purpose": "retry",
                    "idempotency_key": call_key,
                    "receipt_id": retry_receipt_id,
                    "downstream_executions": retry_executions,
                }
            )
            attempts.append(
                self._standard_attempt(
                    target,
                    business_operation_id=op_call,
                    idempotency_key=call_key,
                    # "replayed" is a claim about what the gateway did, so it
                    # is made only when the receipt id proves it; a retry that
                    # came back with a new receipt is a second success, not a
                    # replay, and must not be recorded as one.
                    status=(
                        "error"
                        if retry_result.isError
                        else "replayed"
                        if retry_receipt_id is not None
                        and retry_receipt_id == first_receipt_id
                        else "success"
                    ),
                    client_visible_state=(
                        "no_information"
                        if retry_result.isError
                        else "confirmed_replay"
                        if retry_receipt_id is not None
                        and retry_receipt_id == first_receipt_id
                        else "confirmed_success"
                    ),
                    latency_ms=retry_latency_ms,
                    receipt=retry_receipt,
                    refund=(
                        dict(retry_result.structuredContent)
                        if isinstance(retry_result.structuredContent, dict)
                        else None
                    ),
                    details={
                        "probe": "retry",
                        "same_receipt_as_first_call": retry_receipt_id
                        == first_receipt_id,
                    },
                )
            )

        # -- transport coverage: the compatibility endpoint ---------------
        compat_identity = OperationIdentity.first_attempt(op_compat)
        compat = await target.agent.submit(
            compat_identity, refund_compat, timeout_seconds=CALL_TIMEOUT_SECONDS
        )
        snap_after_compat = await gateway.snapshot(tenant)
        compat_movement = _movement(snap_after_retry, snap_after_compat)
        compat_executions = target.ledger.execution_count(op_compat)
        attempts.append(compat)
        log.emit(
            "t14.compatibility_transport",
            f"{configuration}: one governed call over {COMPATIBILITY_ENDPOINT} "
            f"with a distinct key -> {compat.status}",
            scenario=self.test_id,
            configuration=configuration,
            idempotency_key=compat_identity.idempotency_key,
            status=compat.status,
            receipt_id=compat.receipt_id,
            downstream_executions=compat_executions,
            gateway_movement=compat_movement,
        )

        transports = {
            "note": (
                "The standards-compliant endpoint and the project-specific "
                "compatibility endpoint are separate surfaces and were driven "
                "separately by this run, by different clients."
            ),
            "standard": {
                "endpoint": STANDARD_ENDPOINT,
                "client": (
                    "official MCP SDK (mcp.ClientSession over "
                    "mcp.client.streamable_http), not this product's SDK"
                ),
                "exercised": True,
                "protocol_version": steps.get("initialize", {}).get("protocol_version"),
                "permit": "minted server-side by the endpoint",
                "governed_calls": standard_calls,
            },
            "compatibility": {
                "endpoint": COMPATIBILITY_ENDPOINT,
                "client": (
                    "the lab's own JSON-RPC client, the transport every other "
                    "scenario drives"
                ),
                "exercised": True,
                "permit": "caller-supplied permit issued before the call",
                "idempotency_key": compat_identity.idempotency_key,
                "status": compat.status,
                "receipt_id": compat.receipt_id,
                "receipt_outcome": (
                    compat.receipt.get("outcome") if compat.receipt else None
                ),
                "downstream_executions": compat_executions,
                "gateway_movement": compat_movement,
            },
        }

        measurements = await self.measure(
            target,
            attempts,
            operation_ids=[op_call, op_malformed, op_unknown, op_compat],
        )

        for name in STEPS:
            steps.setdefault(
                name,
                {
                    "case": name,
                    "step": name,
                    "passed": False,
                    "detail": "the step was never reached, so nothing was measured",
                },
            )
        failed_steps = [name for name in STEPS if not steps[name]["passed"]]
        verdict = Verdict.PASS if not failed_steps else Verdict.FAIL

        remaining_risks = [
            "One client implementation over one transport: the reference MCP "
            f"SDK speaking Streamable HTTP in JSON mode at protocol version "
            f"{steps.get('initialize', {}).get('protocol_version')}. Other clients, "
            "SSE streaming and session resumption are not exercised here.",
            "The standard endpoint mints the permit itself from the caller's "
            "wallet, so this run says nothing about a caller-supplied permit "
            "over that transport.",
            "Transport coverage here is one governed call per surface; it "
            "records that both were driven, not that they behave identically "
            "under failure.",
        ]
        if crossings_from_errors:
            remaining_risks.append(
                "The two error probes produced no downstream execution, but "
                f"{crossings_from_errors} of them did reach the upstream over "
                "the network (fault layer): the schema-invalid argument was "
                "admitted, debited and dispatched, and was refused by the "
                "upstream tool rather than by the governed manifest's "
                "inputSchema at the gateway boundary. The charge was "
                "compensated, so the net cost was zero."
            )
        if compat.status != "success":
            remaining_risks.append(
                "The compatibility-transport call recorded for coverage did "
                f"not succeed (status {compat.status!r}); that is reported, "
                "not folded into this scenario's verdict."
            )

        observation = self._observation(
            steps=steps,
            failed_steps=failed_steps,
            measurements=measurements,
            compat=compat,
            compat_executions=compat_executions,
        )

        log.emit(
            "t14.verdict",
            f"{configuration}: {verdict.value} -- {observation}",
            scenario=self.test_id,
            configuration=configuration,
            failed_steps=failed_steps,
        )

        return self.result(
            target,
            verdict=verdict,
            observation=observation,
            measurements=measurements,
            attempts=attempts,
            remaining_risks=remaining_risks,
            extra={
                # Same list under both names: "cases" is the suite-wide key
                # for a per-sub-case table, "steps" is this scenario's own.
                "cases": [steps[name] for name in STEPS],
                "steps": [steps[name] for name in STEPS],
                "failed_steps": failed_steps,
                "transports": transports,
                "client": (
                    "mcp.ClientSession over mcp.client.streamable_http "
                    "(the reference SDK), driven against the gateway's ASGI app"
                ),
                "receipt_meta_key": RECEIPT_META_KEY,
            },
        )

    # -- helpers -----------------------------------------------------------

    async def _initialize_probe(
        self, gateway: GatewayUnderTest, tenant: Any, *, with_credentials: bool
    ) -> dict[str, Any]:
        """Open an SDK session against ``POST /mcp``, with or without the key.

        One code path serves both the credential-less probe and its control,
        so the only difference between the two runs is the credential itself.
        A refusal arrives as an exception out of ``initialize`` (the SDK
        raises the transport's HTTP error), so it is caught and described
        rather than allowed to end the run.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app),
            base_url=GATEWAY_BASE_URL,
            headers=dict(tenant.headers) if with_credentials else None,
            follow_redirects=False,
        )
        try:
            async with streamable_http_client(
                f"{GATEWAY_BASE_URL}/mcp", http_client=http_client
            ) as (read, write, _session_id):
                async with ClientSession(read, write) as session:
                    result = await session.initialize()
            return {
                "initialized": True,
                "detail": (
                    "initialize succeeded "
                    + ("with the API key" if with_credentials else "with NO API key")
                    + f", negotiating protocol version {result.protocolVersion}"
                ),
                "http_statuses": [],
                "exception": None,
            }
        except Exception as exc:  # noqa: BLE001 - the refusal is the measurement
            statuses = _http_statuses(exc)
            reasons = _leaf_reasons(exc)
            return {
                "initialized": False,
                "detail": (
                    (f"HTTP {statuses[0]} -- " if statuses else "")
                    + "; ".join(reasons or [f"{type(exc).__name__}: {str(exc)[:200]}"])
                ),
                "http_statuses": statuses,
                "exception": type(exc).__name__,
            }
        finally:
            await http_client.aclose()

    def _standard_attempt(
        self,
        target: Target,
        *,
        business_operation_id: str,
        idempotency_key: str,
        status: str,
        client_visible_state: str,
        latency_ms: float,
        reason: str | None = None,
        receipt: dict[str, Any] | None = None,
        refund: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> AttemptOutcome:
        """One call made by the SDK client, in the lab's attempt shape."""
        return AttemptOutcome(
            configuration=target.configuration.value,
            identity={
                "business_operation_id": business_operation_id,
                "idempotency_key": idempotency_key,
                "transport": STANDARD_ENDPOINT,
                "client": "reference MCP SDK",
            },
            status=status,
            client_visible_state=client_visible_state,
            # The reference SDK hands back a parsed protocol result, not the
            # HTTP response, so the status of these calls is not observed
            # here. Reporting one would be inventing a measurement.
            http_status=None,
            latency_ms=latency_ms,
            reason=reason,
            refund=refund,
            receipt=receipt,
            details={
                **(details or {}),
                "http_status": "not exposed by the reference MCP SDK client",
            },
        )

    def _observation(
        self,
        *,
        steps: dict[str, dict[str, Any]],
        failed_steps: list[str],
        measurements: Measurements,
        compat: AttemptOutcome,
        compat_executions: int,
    ) -> str:
        counters = measurements.counters
        head = (
            "An independent client -- the reference MCP SDK, not this "
            f"product's -- completed the lifecycle against {STANDARD_ENDPOINT}: "
            f"initialize at protocol version "
            f"{steps.get('initialize', {}).get('protocol_version')}, tools "
            "capability "
            "advertised, the governed refund tool listed, one governed "
            f"tools/call carrying a signed receipt under "
            f'_meta["{RECEIPT_META_KEY}"], structured errors for an unserved '
            "tool and a malformed argument, and a retry on the same "
            "Idempotency-Key that returned the same receipt id."
        )
        retry_step = steps.get("retry", {})
        retry_movement = retry_step.get("gateway_movement") or {}
        retry_executions_added = retry_step.get("downstream_executions", 0) - steps.get(
            "tools_call", {}
        ).get("downstream_executions", 0)
        numbers = (
            f"Across the whole run the independent effect ledger recorded "
            f"{counters.downstream_executions} downstream execution(s) for "
            f"{counters.incoming_requests} client call(s); the gateway reported "
            f"{counters.gateway_debits} debit(s), {counters.gateway_refunds} "
            f"refund(s) and {counters.receipts} receipt(s). The retry added "
            f"{retry_executions_added} downstream execution(s) (effect ledger) "
            f"and {retry_movement.get('debits')} debit(s) (gateway-reported), "
            "and came back with "
            + (
                "the same receipt id as the first call."
                if retry_step.get("same_receipt_id")
                else "a DIFFERENT receipt id from the first call."
            )
        )
        coverage = (
            f"Both transports were driven: {STANDARD_ENDPOINT} by the SDK here, "
            f"and {COMPATIBILITY_ENDPOINT} -- the project-specific endpoint "
            "every other scenario uses -- by one governed call with a distinct "
            f"key, which answered {compat.status!r} with "
            f"{compat_executions} downstream execution(s)."
        )
        if not failed_steps:
            return f"{head} {numbers} {coverage}"
        broken = "; ".join(f"{name}: {steps[name]['detail']}" for name in failed_steps)
        return (
            f"The standards-compliant lifecycle did not complete: "
            f"{len(failed_steps)} step(s) failed -- {broken}. {numbers} {coverage}"
        )
