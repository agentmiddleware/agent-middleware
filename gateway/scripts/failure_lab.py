#!/usr/bin/env python3
"""Failure laboratory: the action completed, but the response was lost.

One consequential workflow (a vendor payout), one injected fault (the
downstream executes, then its response is lost on the wire), measured across
three integrations of the same simulated payment rail:

1. the existing integration -- the agent calls the tool directly and sends no
   idempotency key;
2. the native-control baseline -- the same direct call, but the agent persists
   an idempotency key across the retry and the downstream honors it, with
   parameter-conflict detection (the control a correctly built integration
   already has);
3. the gateway -- the agent calls through the governed MCP boundary, and the
   fault is injected at either hop: between the gateway and the agent, or
   between the downstream and the gateway.

The gateway side is the real thing: the real ``UpstreamMcpAdapter`` registered
through ``register_configured_upstream_mcp`` against a real in-process
Streamable HTTP MCP server, with the fault injected below the adapter at the
HTTP transport. A lost response after the durable dispatch claim is therefore
the adapter's own ``UpstreamMcpDeliveryUncertainError``, not a stand-in.

Two rules keep the numbers honest. Downstream effects are counted by a record
the gateway cannot reach (the payment rail's own effects log), never by the
gateway's receipts. Gateway dispatches are counted at the downstream's HTTP
layer, never from the gateway's own attempt rows. Every expectation is
asserted and the process exits non-zero when one breaks, after writing the
failing sequence, versions, configuration, and event history to the run
directory. The report is required to say when the native baseline already
handles the fault, and it does.

Run::

    make failure-lab                 # transcript + report
    make failure-lab-check           # JSON summary only, same assertions
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "data" / "failure-lab"
ADMIN_KEY = "failure-lab-admin-key"
# The same throwaway signing seed the other local demos use. Not a secret.
LAB_PRIVATE_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

UPSTREAM_URL = "http://localhost:9000/mcp"
UPSTREAM_TOOL = "payout.send"
PUBLIC_TOOL = "vendor.payout.send"
# The adapter refuses an empty bearer credential; the rail is in-process and
# never checks it, and it is never written to any artifact.
UPSTREAM_BEARER = "failure-lab-upstream-bearer"
CREDITS_PER_CALL = "5"
CONNECT_TIMEOUT_SECONDS = 5.0
CALL_TIMEOUT_SECONDS = 10.0
DIRECT_CALL_TIMEOUT_SECONDS = 10.0

VENDOR = "Northwind Supply"
AMOUNT_USD = "250.00"
CONFLICT_AMOUNT_USD = "9500.00"

FAULT_NONE = "none"
FAULT_LOST_RESPONSE = "downstream action completed; response lost"

HOP_AGENT_DOWNSTREAM = "agent->downstream"
HOP_AGENT_GATEWAY = "agent->gateway"
HOP_GATEWAY_DOWNSTREAM = "gateway->downstream"

CONFIG_EXISTING = "existing integration (direct call, no idempotency key)"
CONFIG_NATIVE = "native idempotency baseline (direct call, key persisted and honored)"
CONFIG_GATEWAY = "gateway (downstream key not used)"
CONFIG_GATEWAY_NATIVE = "gateway + native idempotency (key forwarded to the downstream)"

PRINT_STEPS = True


# --------------------------------------------------------------------------- #
# Command line and environment (must precede the application import)           #
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable summary (report.json) to stdout.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Parent directory for the run directory (default: data/failure-lab).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run directory name (default: UTC timestamp plus a short suffix).",
    )
    parser.add_argument(
        "--latency-samples",
        type=int,
        default=5,
        help="Fault-free calls per configuration used to measure added latency.",
    )
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        action="store_true",
        help="Compatibility flag: the lab always asserts its expectations.",
    )
    return parser.parse_args(argv)


ARGS = parse_args()
RUN_ID = ARGS.run_id or (
    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
)
RUN_DIR = (ARGS.output_dir / RUN_ID).resolve()
GATEWAY_DB = RUN_DIR / "gateway.db"


def require_unused_run_dir() -> None:
    """Refuse a run directory that already holds a run.

    The event history and the downstream effects log are append-only, while
    the reports are overwritten and the gateway database is deleted. Running
    twice into one directory would therefore leave ``effects.jsonl`` holding
    two runs' effects, and ``events.jsonl`` two sequences both starting at 1,
    beside a ``report.json`` describing only the second — contradictory
    evidence of exactly the kind this lab exists not to produce.

    The generated run id is unique, so this only fires when ``--run-id``
    deliberately names an existing run. Refusing beats truncating: the
    artifacts are the product here, and silently destroying a previous run's
    evidence is the worse failure.
    """
    if not RUN_DIR.exists():
        return
    existing = sorted(entry.name for entry in RUN_DIR.iterdir())
    if not existing:
        return
    raise SystemExit(
        f"failure-lab: {RUN_DIR} already holds a run "
        f"({', '.join(existing[:6])}{'…' if len(existing) > 6 else ''}).\n"
        "Two runs in one directory would leave the saved effects and event "
        "history disagreeing with the report, so the lab will not append to "
        "them. Pass a different --run-id, or remove that directory first."
    )


def configure_environment() -> None:
    """Lab-safe defaults: strict trust mode, throwaway SQLite, throwaway seed."""
    require_unused_run_dir()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{GATEWAY_DB}"
    os.environ["VALID_API_KEYS"] = ADMIN_KEY
    os.environ["ENVIRONMENT"] = "local"
    os.environ["TRUST_MODE_ENABLED"] = "true"
    os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
    os.environ["ENABLE_PROOF_SURFACES"] = "false"
    os.environ["TRUST_SIGNING_KEY_ID"] = "lab-ed25519"
    os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = LAB_PRIVATE_KEY_B64


configure_environment()
sys.path.insert(0, str(ROOT))
# The offline verifier ships in the SDK and must not import the app.
sys.path.insert(0, str(ROOT.parent / "sdk" / "python" / "src"))

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.shared.exceptions import McpError  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.db.database import close_db, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.service_registry import get_service_registry  # noqa: E402
from app.services.upstream_mcp import register_configured_upstream_mcp  # noqa: E402
from b2a_sdk.receipt_verifier import (  # noqa: E402
    key_set_from_document,
    verify_bundle,
)


# --------------------------------------------------------------------------- #
# Event history                                                                 #
# --------------------------------------------------------------------------- #


class EventLog:
    """Append-only, sequence-numbered record of everything the lab observed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.scenario: str | None = None
        self._seq = 0
        self._t0 = time.perf_counter()
        self._handle = path.open("a", encoding="utf-8")

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "t_ms": round((time.perf_counter() - self._t0) * 1000, 3),
            "at": datetime.now(timezone.utc).isoformat(),
            "scenario": self.scenario,
            "kind": kind,
            **fields,
        }
        self._handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        self._handle.flush()
        return event

    def close(self) -> None:
        self._handle.close()


# --------------------------------------------------------------------------- #
# The fault: let the server finish, then lose its response                      #
# --------------------------------------------------------------------------- #


def _jsonrpc_method(content: bytes) -> str | None:
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("method"), str):
        return payload["method"]
    return None


def _parse_json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None


class FaultInjector:
    """Arms the lost-response fault at one hop, for the next tool call only.

    It also counts every tool call that crosses each hop, which is how the lab
    observes gateway dispatches without consulting the gateway.
    """

    def __init__(self, events: EventLog) -> None:
        self._events = events
        self.armed_hop: str | None = None
        self.tool_calls: dict[str, int] = {}
        self.drops: list[dict[str, Any]] = []

    def arm(self, hop: str) -> None:
        self.armed_hop = hop
        self._events.emit("fault_armed", hop=hop, fault=FAULT_LOST_RESPONSE)

    def reset_counts(self) -> None:
        self.tool_calls = {}

    def observe_tool_call(self, hop: str, *, status: int, elapsed_ms: float) -> None:
        self.tool_calls[hop] = self.tool_calls.get(hop, 0) + 1
        self._events.emit(
            "tool_call_crossed_hop",
            hop=hop,
            status=status,
            elapsed_ms=round(elapsed_ms, 3),
            count_at_hop=self.tool_calls[hop],
        )

    def should_drop(self, hop: str) -> bool:
        return self.armed_hop == hop

    def record_drop(self, hop: str, *, lost_body: bytes) -> dict[str, Any]:
        self.armed_hop = None
        lost = _parse_json(lost_body)
        drop = {"hop": hop, "lost_response": lost}
        # What the agent would have seen, kept for the report's benefit only.
        if isinstance(lost, dict):
            result = lost.get("result") if isinstance(lost.get("result"), dict) else {}
            receipt = result.get("receipt") if isinstance(result, dict) else None
            if isinstance(receipt, dict):
                drop["lost_receipt_id"] = receipt.get("receipt_id")
                drop["lost_receipt_outcome"] = receipt.get("outcome")
            structured = (
                result.get("structuredContent") if isinstance(result, dict) else None
            )
            if isinstance(structured, dict):
                drop["lost_confirmation"] = structured.get("confirmation")
        self.drops.append(drop)
        self._events.emit("response_lost", drop=drop)
        return drop


class LossyTransport(httpx.AsyncBaseTransport):
    """Forwards every request; when armed, drops the response to a tool call.

    The drop happens only after the inner transport has returned, so the
    server has completed the call and any effect it has is already durable.
    That is the fault under test: the action completed, the response did not
    arrive. The caller sees the same timeout it would see from a dead socket.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        hop: str,
        injector: FaultInjector,
    ) -> None:
        self._inner = inner
        self._hop = hop
        self._injector = injector

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return await self._inner.handle_async_request(request)
        await request.aread()
        if _jsonrpc_method(request.content) != "tools/call":
            return await self._inner.handle_async_request(request)

        started = time.perf_counter()
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._injector.observe_tool_call(
            self._hop, status=response.status_code, elapsed_ms=elapsed_ms
        )
        if self._injector.should_drop(self._hop):
            self._injector.record_drop(self._hop, lost_body=body)
            raise httpx.ReadTimeout(
                "response lost after the downstream completed", request=request
            )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=body,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# --------------------------------------------------------------------------- #
# The downstream: a payment rail with an independent record of its effects      #
# --------------------------------------------------------------------------- #


@dataclass
class Effect:
    seq: int
    confirmation: str
    vendor: str
    invoice: str
    amount_usd: str
    idempotency_key: str | None
    forwarded_meta: dict[str, Any]
    scenario: str | None


class PaymentRail:
    """Stands in for the system of record the consequential tool writes to.

    It is a real Streamable HTTP MCP server. Its effects list counts
    executions of the tool body, which is what makes every "paid once" line
    below evidence rather than a claim. It also implements the native control
    a payment API such as Stripe documents: an optional idempotency key whose
    stored response is replayed, and whose reuse with different parameters is
    refused. Whether a caller uses that control is the caller's choice.
    """

    def __init__(self, *, effects_path: Path, events: EventLog) -> None:
        self.effects: list[Effect] = []
        self._effects_path = effects_path
        self._events = events
        self._stored: dict[str, tuple[str, dict[str, Any]]] = {}
        self.server = FastMCP(
            "failure-lab-payment-rail",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                allowed_hosts=["localhost:9000"],
            ),
        )
        rail = self

        @self.server.tool(
            name=UPSTREAM_TOOL,
            description=(
                "Send one vendor payout. Executing it twice pays twice unless an "
                "idempotency key is supplied and honored."
            ),
        )
        async def payout_send(
            ctx: Context,
            vendor: str,
            invoice: str,
            amount_usd: str,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            meta = ctx.request_context.meta
            forwarded = (
                meta.model_dump(mode="json", by_alias=True, exclude_none=True)
                if meta is not None
                else {}
            )
            return rail.execute(
                vendor=vendor,
                invoice=invoice,
                amount_usd=amount_usd,
                idempotency_key=idempotency_key,
                forwarded_meta=forwarded,
            )

        self.asgi_app = self.server.streamable_http_app()

    def lifespan(self) -> Any:
        return self.asgi_app.router.lifespan_context(self.asgi_app)

    def execute(
        self,
        *,
        vendor: str,
        invoice: str,
        amount_usd: str,
        idempotency_key: str | None,
        forwarded_meta: dict[str, Any],
    ) -> dict[str, Any]:
        params_hash = hashlib.sha256(
            json.dumps(
                {"vendor": vendor, "invoice": invoice, "amount_usd": amount_usd},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if idempotency_key:
            stored = self._stored.get(idempotency_key)
            if stored is not None:
                stored_hash, stored_response = stored
                if stored_hash != params_hash:
                    self._events.emit(
                        "downstream_idempotency_conflict",
                        idempotency_key=idempotency_key,
                        invoice=invoice,
                        amount_usd=amount_usd,
                    )
                    raise ValueError(
                        "idempotency_error: key already used with different parameters"
                    )
                self._events.emit(
                    "downstream_replayed_stored_response",
                    idempotency_key=idempotency_key,
                    confirmation=stored_response["confirmation"],
                )
                return {**stored_response, "replayed": True}

        effect = Effect(
            seq=len(self.effects) + 1,
            confirmation=f"PAY-{len(self.effects) + 1:04d}",
            vendor=vendor,
            invoice=invoice,
            amount_usd=amount_usd,
            idempotency_key=idempotency_key,
            forwarded_meta=forwarded_meta,
            scenario=self._events.scenario,
        )
        self.effects.append(effect)
        with self._effects_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(effect), sort_keys=True) + "\n")
        self._events.emit(
            "downstream_effect",
            confirmation=effect.confirmation,
            invoice=invoice,
            amount_usd=amount_usd,
            idempotency_key=idempotency_key,
            forwarded_meta=forwarded_meta,
        )
        response = {
            "confirmation": effect.confirmation,
            "vendor": vendor,
            "invoice": invoice,
            "amount_usd": amount_usd,
            "status": "settled",
            "replayed": False,
        }
        if idempotency_key:
            self._stored[idempotency_key] = (params_hash, response)
        return response

    def effects_for(self, invoice: str) -> list[Effect]:
        return [effect for effect in self.effects if effect.invoice == invoice]

    def stored_response(self, idempotency_key: str) -> dict[str, Any] | None:
        stored = self._stored.get(idempotency_key)
        return None if stored is None else dict(stored[1])


# --------------------------------------------------------------------------- #
# The agent and the two ways it can reach the rail                              #
# --------------------------------------------------------------------------- #


class Agent:
    """An agent with a durable key store: one key per business operation.

    A correct client reuses that key on every retry. A restart that loses the
    store (the failure mode every configuration below shares) makes the same
    business operation look like a new one.
    """

    def __init__(self, name: str, *, generation: int = 0) -> None:
        self.name = name
        self.generation = generation
        self._keys: dict[str, str] = {}

    def key_for(self, operation: str) -> str:
        if operation not in self._keys:
            suffix = (
                ""
                if self.generation == 0
                else f"-g{self.generation}-{uuid.uuid4().hex[:6]}"
            )
            self._keys[operation] = f"pay-{operation}{suffix}"
        return self._keys[operation]

    def restarted(self) -> Agent:
        return Agent(self.name, generation=self.generation + 1)


@dataclass
class Attempt:
    number: int
    kind: str
    latency_ms: float
    idempotency_key: str | None = None
    confirmation: str | None = None
    replayed_by_downstream: bool | None = None
    receipt_id: str | None = None
    receipt_outcome: str | None = None
    credits_charged: str | None = None
    detail: str | None = None


def _flatten_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [
            inner for member in exc.exceptions for inner in _flatten_exceptions(member)
        ]
    return [exc]


def _is_transport_loss(exc: BaseException) -> bool:
    for member in _flatten_exceptions(exc):
        if isinstance(member, (httpx.TransportError, TimeoutError)):
            return True
        if isinstance(member, McpError) and "closed" in str(member).lower():
            return True
    return False


def _describe_exception(exc: BaseException) -> str:
    return "; ".join(
        f"{type(member).__name__}: {member}"[:160]
        for member in _flatten_exceptions(exc)
    )


class DirectCaller:
    """The agent speaks MCP straight to the rail. Nothing sits in between."""

    def __init__(self, rail: PaymentRail, injector: FaultInjector, events: EventLog):
        self._client = httpx.AsyncClient(
            transport=LossyTransport(
                ASGITransport(app=rail.asgi_app),
                hop=HOP_AGENT_DOWNSTREAM,
                injector=injector,
            ),
            follow_redirects=False,
        )
        self._events = events

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        *,
        number: int,
        arguments: dict[str, Any],
        idempotency_key: str | None,
    ) -> Attempt:
        args = dict(arguments)
        if idempotency_key is not None:
            args["idempotency_key"] = idempotency_key
        self._events.emit(
            "agent_attempt",
            hop=HOP_AGENT_DOWNSTREAM,
            number=number,
            arguments=args,
        )
        started = time.perf_counter()
        try:
            async with asyncio.timeout(DIRECT_CALL_TIMEOUT_SECONDS):
                async with streamable_http_client(
                    UPSTREAM_URL, http_client=self._client, terminate_on_close=True
                ) as (read_stream, write_stream, _get_session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            UPSTREAM_TOOL,
                            args,
                            read_timeout_seconds=timedelta(
                                seconds=DIRECT_CALL_TIMEOUT_SECONDS
                            ),
                        )
        except Exception as exc:  # the lost response surfaces here
            latency_ms = (time.perf_counter() - started) * 1000
            kind = "response_lost" if _is_transport_loss(exc) else "error"
            attempt = Attempt(
                number=number,
                kind=kind,
                latency_ms=latency_ms,
                idempotency_key=idempotency_key,
                detail=_describe_exception(exc),
            )
            self._events.emit("agent_observed", attempt=asdict(attempt))
            return attempt

        latency_ms = (time.perf_counter() - started) * 1000
        if result.isError:
            text = " ".join(
                getattr(item, "text", "") for item in result.content
            ).strip()
            kind = (
                "downstream_idempotency_conflict"
                if "idempotency_error" in text
                else "downstream_error"
            )
            attempt = Attempt(
                number=number,
                kind=kind,
                latency_ms=latency_ms,
                idempotency_key=idempotency_key,
                detail=text[:200],
            )
        else:
            payload = result.structuredContent
            if not isinstance(payload, dict):
                payload = json.loads(getattr(result.content[0], "text", "{}"))
            attempt = Attempt(
                number=number,
                kind="success",
                latency_ms=latency_ms,
                idempotency_key=idempotency_key,
                confirmation=payload.get("confirmation"),
                replayed_by_downstream=bool(payload.get("replayed")),
            )
        self._events.emit("agent_observed", attempt=asdict(attempt))
        return attempt


class GatewayCaller:
    """The agent calls the governed boundary; the boundary calls the rail."""

    def __init__(
        self,
        *,
        injector: FaultInjector,
        events: EventLog,
        api_key: str,
        wallet_id: str,
    ) -> None:
        self._client = AsyncClient(
            transport=LossyTransport(
                ASGITransport(app=app), hop=HOP_AGENT_GATEWAY, injector=injector
            ),
            base_url="http://gateway",
            headers={"X-API-Key": api_key},
        )
        self._events = events
        self.wallet_id = wallet_id

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        *,
        number: int,
        permit_id: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        request_id: str,
    ) -> Attempt:
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": PUBLIC_TOOL,
                "arguments": arguments,
                "mcpContext": {
                    "wallet_id": self.wallet_id,
                    "permit_id": permit_id,
                    "idempotency_key": idempotency_key,
                },
            },
        }
        self._events.emit(
            "agent_attempt",
            hop=HOP_AGENT_GATEWAY,
            number=number,
            idempotency_key=idempotency_key,
            arguments=arguments,
        )
        started = time.perf_counter()
        try:
            response = await self._client.post("/mcp/messages", json=body)
        except httpx.TransportError as exc:
            attempt = Attempt(
                number=number,
                kind="response_lost",
                latency_ms=(time.perf_counter() - started) * 1000,
                idempotency_key=idempotency_key,
                detail=f"{type(exc).__name__}: {exc}",
            )
            self._events.emit("agent_observed", attempt=asdict(attempt))
            return attempt

        latency_ms = (time.perf_counter() - started) * 1000
        envelope = response.json()
        error = envelope.get("error")
        if isinstance(error, dict):
            data: dict[str, Any] = (
                error["data"] if isinstance(error.get("data"), dict) else {}
            )
            receipt: dict[str, Any] = (
                data["receipt"] if isinstance(data.get("receipt"), dict) else {}
            )
            attempt = Attempt(
                number=number,
                kind=str(error.get("message")),
                latency_ms=latency_ms,
                idempotency_key=idempotency_key,
                receipt_id=receipt.get("receipt_id"),
                receipt_outcome=receipt.get("outcome"),
                credits_charged=(
                    None
                    if receipt.get("credits_charged") is None
                    else str(receipt.get("credits_charged"))
                ),
                detail=json.dumps(
                    {"code": error.get("code"), "http_status": response.status_code},
                    sort_keys=True,
                ),
            )
        else:
            result: dict[str, Any] = envelope["result"]
            receipt = (
                result["receipt"] if isinstance(result.get("receipt"), dict) else {}
            )
            payload = result.get("structuredContent")
            if not isinstance(payload, dict):
                payload = json.loads(result["content"][0]["text"])
            attempt = Attempt(
                number=number,
                kind="success",
                latency_ms=latency_ms,
                idempotency_key=idempotency_key,
                confirmation=payload.get("confirmation"),
                replayed_by_downstream=bool(payload.get("replayed")),
                receipt_id=receipt.get("receipt_id"),
                receipt_outcome=receipt.get("outcome"),
                credits_charged=(
                    None
                    if receipt.get("credits_charged") is None
                    else str(receipt.get("credits_charged"))
                ),
            )
        self._events.emit("agent_observed", attempt=asdict(attempt))
        return attempt


# --------------------------------------------------------------------------- #
# The gateway under test                                                         #
# --------------------------------------------------------------------------- #


class Gateway:
    """The real application, its real upstream adapter, and one agent wallet."""

    def __init__(self, rail: PaymentRail, injector: FaultInjector, events: EventLog):
        self._rail = rail
        self._injector = injector
        self._events = events
        self._upstream_client: httpx.AsyncClient | None = None
        self._admin: AsyncClient | None = None
        self._agent: AsyncClient | None = None
        self.wallet_id = ""
        self.key_id = ""
        self.api_key = ""
        self.service: dict[str, Any] | None = None
        self.configuration: dict[str, Any] = {}

    async def start(self) -> None:
        await close_db()
        for suffix in ("", "-shm", "-wal"):
            path = Path(f"{GATEWAY_DB}{suffix}")
            if path.exists():
                path.unlink()
        await init_db()

        self._upstream_client = httpx.AsyncClient(
            transport=LossyTransport(
                ASGITransport(app=self._rail.asgi_app),
                hop=HOP_GATEWAY_DOWNSTREAM,
                injector=self._injector,
            ),
            headers={"Authorization": f"Bearer {UPSTREAM_BEARER}"},
            follow_redirects=False,
        )
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            ENVIRONMENT="local",
            MCP_UPSTREAM_ENABLED=True,
            # Pinned, not left to the default: `Settings` reads unset fields
            # from the process environment, so an exported
            # MCP_UPSTREAM_MULTI_TOOL_ENABLED would otherwise put this lab in
            # multi-tool mode, which its one-tool design does not support.
            MCP_UPSTREAM_MULTI_TOOL_ENABLED=False,
            MCP_UPSTREAM_URL=UPSTREAM_URL,
            MCP_UPSTREAM_TOOL_NAME=UPSTREAM_TOOL,
            MCP_UPSTREAM_PUBLIC_TOOL_ID=PUBLIC_TOOL,
            MCP_UPSTREAM_BEARER_TOKEN=SecretStr(UPSTREAM_BEARER),
            MCP_UPSTREAM_CREDITS_PER_CALL=Decimal(CREDITS_PER_CALL),
            MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS=CONNECT_TIMEOUT_SECONDS,
            MCP_UPSTREAM_CALL_TIMEOUT_SECONDS=CALL_TIMEOUT_SECONDS,
        )
        registered = await register_configured_upstream_mcp(
            settings=settings, http_client=self._upstream_client
        )
        # The lab configures one upstream tool by name and leaves
        # MCP_UPSTREAM_MULTI_TOOL_ENABLED at its default, so registration returns
        # that single service rather than a list. Narrow it here instead of
        # widening self.service: a list would mean this configuration changed, and
        # the reads below would quietly address the wrong shape.
        if not isinstance(registered, dict):
            raise RuntimeError(
                "the failure lab registers a single upstream tool, so "
                "register_configured_upstream_mcp must return one service; got "
                f"{type(registered).__name__}"
            )
        self.service = registered
        self.configuration = {
            "public_tool_id": PUBLIC_TOOL,
            "upstream_tool_name": UPSTREAM_TOOL,
            "upstream_url": UPSTREAM_URL,
            "credits_per_call": CREDITS_PER_CALL,
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "call_timeout_seconds": CALL_TIMEOUT_SECONDS,
            "trust_mode_enabled": True,
            "allow_legacy_unpermitted_mcp": False,
            "enable_proof_surfaces": False,
            "database": "sqlite (throwaway, in the run directory)",
        }
        self._events.emit(
            "gateway_started",
            registered_tool=PUBLIC_TOOL,
            execution_backend=(self.service or {}).get("execution_backend"),
        )

        self._admin = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://gateway",
            headers={"X-API-Key": ADMIN_KEY},
        )
        sponsor = await self._post(
            self._admin,
            "/v1/billing/wallets/sponsor",
            expected_status=201,
            json_body={
                "sponsor_name": "Accounts Payable",
                "email": "ap@example.com",
                "initial_credits": 10000,
                "require_kyc": False,
            },
        )
        agent = await self._post(
            self._admin,
            "/v1/billing/wallets/agent",
            expected_status=201,
            json_body={
                "sponsor_wallet_id": sponsor["wallet_id"],
                "agent_id": "ap-agent",
                "budget_credits": 5000,
                "daily_limit": 2000,
            },
        )
        self.wallet_id = agent["wallet_id"]
        key = await self._post(
            self._admin,
            "/v1/api-keys",
            expected_status=201,
            json_body={
                "wallet_id": self.wallet_id,
                "key_name": "ap-agent-runtime",
                "expires_in_days": 30,
            },
        )
        self.key_id = key["key_id"]
        self.api_key = key["api_key"]
        self._agent = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://gateway",
            headers={"X-API-Key": self.api_key},
        )
        self._events.emit("agent_wallet_provisioned", wallet_id=self.wallet_id)

    async def stop(self) -> None:
        for client in (self._agent, self._admin, self._upstream_client):
            if client is not None:
                await client.aclose()
        get_service_registry().unregister_execution_backend("upstream_mcp")
        await close_db()

    @staticmethod
    async def _post(
        client: AsyncClient,
        url: str,
        *,
        expected_status: int,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await client.post(url, json=json_body, headers=headers or {})
        if response.status_code != expected_status:
            raise RuntimeError(f"POST {url} -> {response.status_code}: {response.text}")
        return response.json()

    async def permit(self, scenario: str, *, max_credits: int = 100) -> str:
        assert self._admin is not None
        permit = await self._post(
            self._admin,
            "/v1/permits",
            expected_status=201,
            headers={"Idempotency-Key": f"permit-{scenario}"},
            json_body={
                "issuer_wallet_id": self.wallet_id,
                "subject_wallet_id": self.wallet_id,
                "subject_key_id": self.key_id,
                "allowed_tools": [PUBLIC_TOOL],
                "scopes": [f"tool:{PUBLIC_TOOL}:invoke", "billing:charge"],
                "max_credits": max_credits,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
        )
        self._events.emit("permit_issued", permit_id=permit["permit_id"])
        return permit["permit_id"]

    async def ledger_counts(self) -> dict[str, int]:
        assert self._agent is not None
        response = await self._agent.get(
            f"/v1/billing/ledger/{self.wallet_id}", params={"limit": 200}
        )
        if response.status_code != 200:
            raise RuntimeError(f"ledger read failed: {response.status_code}")
        counts = {"debit": 0, "refund": 0}
        for entry in response.json()["entries"]:
            action = entry.get("action")
            if action in counts:
                counts[action] += 1
        return counts

    async def receipt_verifies_offline(self, receipt_id: str) -> bool:
        assert self._agent is not None
        portable = await self._agent.get(f"/v1/receipts/{receipt_id}/portable")
        if portable.status_code != 200:
            raise RuntimeError(
                f"portable receipt read failed: {portable.status_code} {portable.text}"
            )
        key_document = await self._agent.get("/.well-known/trust-keys.json")
        if key_document.status_code != 200:
            raise RuntimeError("trust key document unavailable")
        outcome = verify_bundle(
            portable.json(), key_set_from_document(key_document.json())
        )
        self._events.emit(
            "receipt_verified_offline",
            receipt_id=receipt_id,
            ok=outcome.ok,
            key_id=outcome.key_id,
            reason=outcome.reason,
        )
        return bool(outcome.ok)


# --------------------------------------------------------------------------- #
# Measurements and checks                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class Measurement:
    scenario: str
    configuration: str
    fault: str
    fault_hop: str | None
    invoice: str
    attempts: list[Attempt]
    downstream_effects: int
    duplicate_effects: int
    unresolved_outcomes: int
    final_known_outcome: str
    gateway_dispatches: int | None = None
    debits: int | None = None
    refunds: int | None = None
    receipt_ids: list[str] = field(default_factory=list)
    receipts_verified_offline: bool | None = None
    downstream_stored_response: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check["passed"] for check in self.checks)

    def check(self, description: str, observed: Any, expected: Any) -> None:
        passed = observed == expected
        self.checks.append(
            {
                "check": description,
                "observed": observed,
                "expected": expected,
                "passed": passed,
            }
        )
        if not passed:
            line(
                f"    FAIL {description}: observed {observed!r}, expected {expected!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok"] = self.ok
        return payload


WIDTH = 72


def banner(title: str) -> None:
    if not PRINT_STEPS:
        return
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def section(title: str) -> None:
    if not PRINT_STEPS:
        return
    print()
    print(f"-- {title} ".ljust(WIDTH, "-"))


def line(text: str = "") -> None:
    if PRINT_STEPS:
        print(text)


def _attempt_line(attempt: Attempt) -> str:
    parts = [f"attempt {attempt.number}: {attempt.kind}"]
    if attempt.confirmation:
        parts.append(attempt.confirmation)
        if attempt.replayed_by_downstream:
            parts.append("(replayed by the downstream)")
    if attempt.receipt_id:
        parts.append(f"receipt {attempt.receipt_id}")
    if attempt.credits_charged is not None:
        parts.append(f"charged {attempt.credits_charged}")
    parts.append(f"[{attempt.latency_ms:.0f} ms]")
    return "  ".join(parts)


def _print_measurement(measurement: Measurement) -> None:
    line(f"  configuration: {measurement.configuration}")
    fault = measurement.fault
    if measurement.fault_hop:
        fault += f" @ {measurement.fault_hop}"
    line(f"  fault:         {fault}")
    for attempt in measurement.attempts:
        line(f"    {_attempt_line(attempt)}")
    counters = [f"downstream effects: {measurement.downstream_effects}"]
    if measurement.gateway_dispatches is not None:
        counters.append(f"gateway dispatches: {measurement.gateway_dispatches}")
    if measurement.debits is not None:
        counters.append(f"debits: {measurement.debits}")
    if measurement.refunds is not None:
        counters.append(f"refunds: {measurement.refunds}")
    counters.append(f"unresolved: {measurement.unresolved_outcomes}")
    line("  " + "   ".join(counters))
    line(f"  final known outcome: {measurement.final_known_outcome}")
    passed = sum(1 for check in measurement.checks if check["passed"])
    line(f"  checks: {passed}/{len(measurement.checks)} passed")


# --------------------------------------------------------------------------- #
# The laboratory                                                                 #
# --------------------------------------------------------------------------- #


class Lab:
    def __init__(self, *, latency_samples: int) -> None:
        self.run_dir = RUN_DIR
        self.events = EventLog(self.run_dir / "events.jsonl")
        self.injector = FaultInjector(self.events)
        self.rail = PaymentRail(
            effects_path=self.run_dir / "effects.jsonl", events=self.events
        )
        self.gateway = Gateway(self.rail, self.injector, self.events)
        self.measurements: list[Measurement] = []
        self.latency_samples = max(0, latency_samples)
        self.latency: dict[str, Any] = {}
        self._invoice_seq = 0

    # -- helpers ------------------------------------------------------------ #

    def _invoice(self) -> str:
        self._invoice_seq += 1
        return f"INV-{4400 + self._invoice_seq}"

    @staticmethod
    def _arguments(invoice: str, amount_usd: str = AMOUNT_USD) -> dict[str, Any]:
        return {"vendor": VENDOR, "invoice": invoice, "amount_usd": amount_usd}

    def _begin(self, scenario: str) -> None:
        self.events.scenario = scenario
        self.injector.reset_counts()
        self.events.emit("scenario_started")
        section(scenario)

    def _finish(self, measurement: Measurement) -> Measurement:
        self.events.emit("scenario_finished", measurement=measurement.to_dict())
        self.measurements.append(measurement)
        _print_measurement(measurement)
        self.events.scenario = None
        return measurement

    @staticmethod
    def _other_confirmations(
        effects: list[Effect], attempts: list[Attempt]
    ) -> list[str]:
        known = {attempt.confirmation for attempt in attempts if attempt.confirmation}
        return [
            effect.confirmation
            for effect in effects
            if effect.confirmation not in known
        ]

    # -- direct configurations ------------------------------------------------ #

    async def _direct(
        self,
        *,
        scenario: str,
        configuration: str,
        use_key: bool,
        fault: bool,
        restart_before_retry: bool = False,
    ) -> Measurement:
        self._begin(scenario)
        invoice = self._invoice()
        agent = Agent("ap-agent")
        caller = DirectCaller(self.rail, self.injector, self.events)
        attempts: list[Attempt] = []
        try:
            key = agent.key_for(invoice) if use_key else None
            if fault:
                self.injector.arm(HOP_AGENT_DOWNSTREAM)
            attempts.append(
                await caller.call(
                    number=1, arguments=self._arguments(invoice), idempotency_key=key
                )
            )
            if attempts[-1].kind == "response_lost":
                if restart_before_retry:
                    agent = agent.restarted()
                    key = agent.key_for(invoice) if use_key else None
                    self.events.emit(
                        "agent_restarted", generation=agent.generation, new_key=key
                    )
                attempts.append(
                    await caller.call(
                        number=2,
                        arguments=self._arguments(invoice),
                        idempotency_key=key,
                    )
                )
        finally:
            await caller.aclose()

        effects = self.rail.effects_for(invoice)
        last = attempts[-1]
        unknown = self._other_confirmations(effects, attempts)
        if last.kind == "success":
            outcome = f"{last.confirmation} settled"
            if last.replayed_by_downstream:
                outcome += " (stored response replayed by the downstream)"
            if unknown:
                outcome += f"; {', '.join(unknown)} also settled, unknown to the agent"
        else:
            outcome = f"{last.kind}: {last.detail}"
        return Measurement(
            scenario=scenario,
            configuration=configuration,
            fault=FAULT_LOST_RESPONSE if fault else FAULT_NONE,
            fault_hop=HOP_AGENT_DOWNSTREAM if fault else None,
            invoice=invoice,
            attempts=attempts,
            downstream_effects=len(effects),
            duplicate_effects=max(0, len(effects) - 1),
            unresolved_outcomes=0 if last.kind == "success" else 1,
            final_known_outcome=outcome,
            downstream_stored_response=(
                self.rail.stored_response(key) if (use_key and key) else None
            ),
        )

    async def existing_control(self) -> Measurement:
        m = await self._direct(
            scenario="existing.control",
            configuration=CONFIG_EXISTING,
            use_key=False,
            fault=False,
        )
        m.check("attempts", len(m.attempts), 1)
        m.check("first attempt succeeds", m.attempts[0].kind, "success")
        m.check("downstream effects", m.downstream_effects, 1)
        return self._finish(m)

    async def existing_lost_response(self) -> Measurement:
        m = await self._direct(
            scenario="existing.lost_response",
            configuration=CONFIG_EXISTING,
            use_key=False,
            fault=True,
        )
        m.check("attempts", len(m.attempts), 2)
        m.check("first attempt lost", m.attempts[0].kind, "response_lost")
        m.check("retry succeeds", m.attempts[1].kind, "success")
        m.check("downstream effects", m.downstream_effects, 2)
        m.check("duplicate effects", m.duplicate_effects, 1)
        m.notes.append(
            "The agent did nothing wrong: it retried a call it never saw succeed. "
            "The duplicate is invisible to it."
        )
        return self._finish(m)

    async def native_control(self) -> Measurement:
        m = await self._direct(
            scenario="native.control",
            configuration=CONFIG_NATIVE,
            use_key=True,
            fault=False,
        )
        m.check("attempts", len(m.attempts), 1)
        m.check("first attempt succeeds", m.attempts[0].kind, "success")
        m.check("downstream effects", m.downstream_effects, 1)
        return self._finish(m)

    async def native_lost_response(self) -> Measurement:
        m = await self._direct(
            scenario="native.lost_response",
            configuration=CONFIG_NATIVE,
            use_key=True,
            fault=True,
        )
        m.check("attempts", len(m.attempts), 2)
        m.check("first attempt lost", m.attempts[0].kind, "response_lost")
        m.check("retry succeeds", m.attempts[1].kind, "success")
        m.check(
            "retry returned the stored response",
            m.attempts[1].replayed_by_downstream,
            True,
        )
        m.check("downstream effects", m.downstream_effects, 1)
        m.check("duplicate effects", m.duplicate_effects, 0)
        m.notes.append(
            "Verdict: native idempotency, used correctly, already prevents the "
            "duplicate for this fault. No gateway was involved."
        )
        return self._finish(m)

    async def native_agent_restart(self) -> Measurement:
        m = await self._direct(
            scenario="native.agent_restart_new_key",
            configuration=CONFIG_NATIVE,
            use_key=True,
            fault=True,
            restart_before_retry=True,
        )
        m.check("attempts", len(m.attempts), 2)
        m.check("first attempt lost", m.attempts[0].kind, "response_lost")
        m.check("retry succeeds under a new key", m.attempts[1].kind, "success")
        m.check(
            "keys differ across the restart",
            m.attempts[0].idempotency_key != m.attempts[1].idempotency_key,
            True,
        )
        m.check("downstream effects", m.downstream_effects, 2)
        m.notes.append(
            "A new key is a new business operation to the downstream. The native "
            "control cannot recover an identity the client lost."
        )
        return self._finish(m)

    async def native_key_conflict(self) -> Measurement:
        scenario = "native.key_conflict"
        self._begin(scenario)
        invoice = self._invoice()
        agent = Agent("ap-agent")
        key = agent.key_for(invoice)
        caller = DirectCaller(self.rail, self.injector, self.events)
        try:
            first = await caller.call(
                number=1, arguments=self._arguments(invoice), idempotency_key=key
            )
            second = await caller.call(
                number=2,
                arguments=self._arguments(invoice, CONFLICT_AMOUNT_USD),
                idempotency_key=key,
            )
        finally:
            await caller.aclose()
        effects = self.rail.effects_for(invoice)
        m = Measurement(
            scenario=scenario,
            configuration=CONFIG_NATIVE,
            fault=FAULT_NONE,
            fault_hop=None,
            invoice=invoice,
            attempts=[first, second],
            downstream_effects=len(effects),
            duplicate_effects=max(0, len(effects) - 1),
            unresolved_outcomes=0,
            final_known_outcome=(
                f"{first.confirmation} settled; the ${CONFLICT_AMOUNT_USD} reuse of "
                f"the key was refused by the downstream ({second.kind})"
            ),
            downstream_stored_response=self.rail.stored_response(key),
        )
        m.check("first attempt succeeds", first.kind, "success")
        m.check(
            "changed parameters under the same key are refused",
            second.kind,
            "downstream_idempotency_conflict",
        )
        m.check("downstream effects", m.downstream_effects, 1)
        return self._finish(m)

    # -- gateway configurations ---------------------------------------------- #

    async def _gateway(
        self,
        *,
        scenario: str,
        configuration: str,
        native_key: bool,
        fault_hop: str | None,
        restart_before_retry: bool = False,
    ) -> Measurement:
        self._begin(scenario)
        invoice = self._invoice()
        permit_id = await self.gateway.permit(scenario)
        before = await self.gateway.ledger_counts()
        drops_before = len(self.injector.drops)
        agent = Agent("ap-agent")
        caller = GatewayCaller(
            injector=self.injector,
            events=self.events,
            api_key=self.gateway.api_key,
            wallet_id=self.gateway.wallet_id,
        )
        attempts: list[Attempt] = []
        try:
            key = agent.key_for(invoice)
            arguments = self._arguments(invoice)
            if native_key:
                arguments["idempotency_key"] = key
            if fault_hop:
                self.injector.arm(fault_hop)
            attempts.append(
                await caller.call(
                    number=1,
                    permit_id=permit_id,
                    idempotency_key=key,
                    arguments=arguments,
                    request_id=f"{scenario}-1",
                )
            )
            if attempts[-1].kind in {"response_lost", "delivery_uncertain"}:
                if restart_before_retry:
                    agent = agent.restarted()
                    key = agent.key_for(invoice)
                    if native_key:
                        arguments["idempotency_key"] = key
                    self.events.emit(
                        "agent_restarted", generation=agent.generation, new_key=key
                    )
                attempts.append(
                    await caller.call(
                        number=2,
                        permit_id=permit_id,
                        idempotency_key=key,
                        arguments=arguments,
                        request_id=f"{scenario}-2",
                    )
                )
        finally:
            await caller.aclose()

        after = await self.gateway.ledger_counts()
        effects = self.rail.effects_for(invoice)
        dispatches = self.injector.tool_calls.get(HOP_GATEWAY_DOWNSTREAM, 0)
        drops = self.injector.drops[drops_before:]

        receipt_ids: list[str] = []
        for attempt in attempts:
            if attempt.receipt_id and attempt.receipt_id not in receipt_ids:
                receipt_ids.append(attempt.receipt_id)
        for drop in drops:
            lost_id = drop.get("lost_receipt_id")
            if lost_id and lost_id not in receipt_ids:
                receipt_ids.append(lost_id)
        verified: bool | None = None
        if receipt_ids:
            verified = all(
                [
                    await self.gateway.receipt_verifies_offline(rid)
                    for rid in receipt_ids
                ]
            )

        last = attempts[-1]
        unknown = self._other_confirmations(effects, attempts)
        if last.kind == "success":
            outcome = f"{last.confirmation} settled; receipt {last.receipt_id}"
            lost_ids = [
                d.get("lost_receipt_id") for d in drops if d.get("lost_receipt_id")
            ]
            if lost_ids and lost_ids[0] == last.receipt_id:
                outcome += " (the same receipt the lost response carried)"
            if unknown:
                outcome += f"; {', '.join(unknown)} also settled, unknown to the agent"
            if verified:
                outcome += "; verified offline"
        elif last.kind == "delivery_uncertain":
            outcome = (
                f"delivery_uncertain; receipt {last.receipt_id}; charge retained "
                f"({last.credits_charged} credits)"
            )
            if len(attempts) == 2 and attempts[0].receipt_id == attempts[1].receipt_id:
                outcome += (
                    "; the retry returned the same receipt and dispatched nothing"
                )
            outcome += (
                f". This harness's independent record shows {len(effects)} effect(s); "
                "the gateway cannot know that."
            )
        else:
            outcome = f"{last.kind}: {last.detail}"

        return Measurement(
            scenario=scenario,
            configuration=configuration,
            fault=FAULT_LOST_RESPONSE if fault_hop else FAULT_NONE,
            fault_hop=fault_hop,
            invoice=invoice,
            attempts=attempts,
            downstream_effects=len(effects),
            duplicate_effects=max(0, len(effects) - 1),
            unresolved_outcomes=1 if last.kind == "delivery_uncertain" else 0,
            final_known_outcome=outcome,
            gateway_dispatches=dispatches,
            debits=after["debit"] - before["debit"],
            refunds=after["refund"] - before["refund"],
            receipt_ids=receipt_ids,
            receipts_verified_offline=verified,
            downstream_stored_response=(
                self.rail.stored_response(key) if native_key else None
            ),
        )

    async def gateway_control(self) -> Measurement:
        m = await self._gateway(
            scenario="gateway.control",
            configuration=CONFIG_GATEWAY,
            native_key=False,
            fault_hop=None,
        )
        m.check("attempts", len(m.attempts), 1)
        m.check("first attempt succeeds", m.attempts[0].kind, "success")
        m.check("receipt outcome", m.attempts[0].receipt_outcome, "success")
        m.check("downstream effects", m.downstream_effects, 1)
        m.check("gateway dispatches", m.gateway_dispatches, 1)
        m.check("debits", m.debits, 1)
        m.check("receipt verifies offline", m.receipts_verified_offline, True)
        return self._finish(m)

    async def gateway_lost_at_agent_hop(
        self, *, scenario: str, configuration: str, native_key: bool
    ) -> Measurement:
        m = await self._gateway(
            scenario=scenario,
            configuration=configuration,
            native_key=native_key,
            fault_hop=HOP_AGENT_GATEWAY,
        )
        m.check("attempts", len(m.attempts), 2)
        m.check("first attempt lost", m.attempts[0].kind, "response_lost")
        m.check("retry succeeds", m.attempts[1].kind, "success")
        m.check("downstream effects", m.downstream_effects, 1)
        m.check("gateway dispatches", m.gateway_dispatches, 1)
        m.check("debits", m.debits, 1)
        m.check("refunds", m.refunds, 0)
        m.check("distinct receipts", len(m.receipt_ids), 1)
        m.check("receipt verifies offline", m.receipts_verified_offline, True)
        m.check(
            "retry returned the confirmation the agent lost",
            m.attempts[1].confirmation,
            self.rail.effects_for(m.invoice)[0].confirmation
            if self.rail.effects_for(m.invoice)
            else None,
        )
        return self._finish(m)

    async def gateway_lost_at_downstream_hop(
        self, *, scenario: str, configuration: str, native_key: bool
    ) -> Measurement:
        m = await self._gateway(
            scenario=scenario,
            configuration=configuration,
            native_key=native_key,
            fault_hop=HOP_GATEWAY_DOWNSTREAM,
        )
        m.check("attempts", len(m.attempts), 2)
        m.check("first attempt ambiguous", m.attempts[0].kind, "delivery_uncertain")
        m.check(
            "first receipt outcome", m.attempts[0].receipt_outcome, "delivery_uncertain"
        )
        m.check("retry is the same ambiguity", m.attempts[1].kind, "delivery_uncertain")
        m.check(
            "retry returned the same receipt",
            m.attempts[1].receipt_id,
            m.attempts[0].receipt_id,
        )
        m.check("downstream effects", m.downstream_effects, 1)
        m.check("gateway dispatches (no redispatch)", m.gateway_dispatches, 1)
        m.check("debits (charge retained)", m.debits, 1)
        m.check("refunds", m.refunds, 0)
        m.check("unresolved outcomes", m.unresolved_outcomes, 1)
        m.check("receipt verifies offline", m.receipts_verified_offline, True)
        m.notes.append(
            "The gateway did not invent certainty and did not repeat the action. "
            "It also did not resolve the ambiguity: that is the caller's and the "
            "downstream's job, using the receipt and the forwarded key."
        )
        if native_key and m.downstream_stored_response:
            m.notes.append(
                "Because the key was forwarded, the downstream holds the stored "
                f"response ({m.downstream_stored_response.get('confirmation')}) an "
                "operator could look up to close the uncertainty out of band."
            )
        return self._finish(m)

    async def gateway_agent_restart(self) -> Measurement:
        m = await self._gateway(
            scenario="gateway.agent_restart_new_key",
            configuration=CONFIG_GATEWAY,
            native_key=False,
            fault_hop=HOP_AGENT_GATEWAY,
            restart_before_retry=True,
        )
        m.check("attempts", len(m.attempts), 2)
        m.check("first attempt lost", m.attempts[0].kind, "response_lost")
        m.check("retry succeeds under a new key", m.attempts[1].kind, "success")
        m.check(
            "keys differ across the restart",
            m.attempts[0].idempotency_key != m.attempts[1].idempotency_key,
            True,
        )
        m.check("downstream effects", m.downstream_effects, 2)
        m.check("gateway dispatches", m.gateway_dispatches, 2)
        m.check("debits", m.debits, 2)
        m.check("distinct receipts", len(m.receipt_ids), 2)
        m.notes.append(
            "Both payouts were authorized, dispatched, debited, and receipted "
            "correctly. The gateway keys identity on the caller's key; it cannot "
            "recover an identity the caller lost either."
        )
        return self._finish(m)

    async def gateway_key_conflict(self) -> Measurement:
        scenario = "gateway.key_conflict"
        self._begin(scenario)
        invoice = self._invoice()
        permit_id = await self.gateway.permit(scenario)
        before = await self.gateway.ledger_counts()
        agent = Agent("ap-agent")
        key = agent.key_for(invoice)
        caller = GatewayCaller(
            injector=self.injector,
            events=self.events,
            api_key=self.gateway.api_key,
            wallet_id=self.gateway.wallet_id,
        )
        try:
            first = await caller.call(
                number=1,
                permit_id=permit_id,
                idempotency_key=key,
                arguments=self._arguments(invoice),
                request_id=f"{scenario}-1",
            )
            second = await caller.call(
                number=2,
                permit_id=permit_id,
                idempotency_key=key,
                arguments=self._arguments(invoice, CONFLICT_AMOUNT_USD),
                request_id=f"{scenario}-2",
            )
        finally:
            await caller.aclose()
        after = await self.gateway.ledger_counts()
        effects = self.rail.effects_for(invoice)
        m = Measurement(
            scenario=scenario,
            configuration=CONFIG_GATEWAY,
            fault=FAULT_NONE,
            fault_hop=None,
            invoice=invoice,
            attempts=[first, second],
            downstream_effects=len(effects),
            duplicate_effects=max(0, len(effects) - 1),
            unresolved_outcomes=0,
            final_known_outcome=(
                f"{first.confirmation} settled; the ${CONFLICT_AMOUNT_USD} reuse of "
                f"the key was refused before dispatch ({second.kind})"
            ),
            gateway_dispatches=self.injector.tool_calls.get(HOP_GATEWAY_DOWNSTREAM, 0),
            debits=after["debit"] - before["debit"],
            refunds=after["refund"] - before["refund"],
            receipt_ids=[first.receipt_id] if first.receipt_id else [],
        )
        m.check("first attempt succeeds", first.kind, "success")
        m.check(
            "changed payload under the same key is refused",
            second.kind,
            "idempotency_key_reused",
        )
        m.check("downstream effects", m.downstream_effects, 1)
        m.check("gateway dispatches", m.gateway_dispatches, 1)
        m.check("debits", m.debits, 1)
        return self._finish(m)

    # -- latency --------------------------------------------------------------- #

    async def measure_latency(self) -> None:
        if self.latency_samples == 0:
            self.latency = {"samples": 0}
            return
        self._begin("latency.samples")
        direct: list[float] = []
        caller = DirectCaller(self.rail, self.injector, self.events)
        try:
            for index in range(self.latency_samples):
                attempt = await caller.call(
                    number=index + 1,
                    arguments=self._arguments(self._invoice()),
                    idempotency_key=None,
                )
                if attempt.kind == "success":
                    direct.append(attempt.latency_ms)
        finally:
            await caller.aclose()

        through_gateway: list[float] = []
        permit_id = await self.gateway.permit(
            "latency.samples", max_credits=5 * self.latency_samples + 5
        )
        gateway_caller = GatewayCaller(
            injector=self.injector,
            events=self.events,
            api_key=self.gateway.api_key,
            wallet_id=self.gateway.wallet_id,
        )
        try:
            for index in range(self.latency_samples):
                invoice = self._invoice()
                attempt = await gateway_caller.call(
                    number=index + 1,
                    permit_id=permit_id,
                    idempotency_key=Agent("ap-agent").key_for(invoice),
                    arguments=self._arguments(invoice),
                    request_id=f"latency-{index + 1}",
                )
                if attempt.kind == "success":
                    through_gateway.append(attempt.latency_ms)
        finally:
            await gateway_caller.aclose()

        self.latency = {
            "samples": self.latency_samples,
            "direct_ms": [round(value, 2) for value in direct],
            "gateway_ms": [round(value, 2) for value in through_gateway],
            "direct_median_ms": round(statistics.median(direct), 2) if direct else None,
            "gateway_median_ms": (
                round(statistics.median(through_gateway), 2)
                if through_gateway
                else None
            ),
            "added_by_gateway_ms": (
                round(statistics.median(through_gateway) - statistics.median(direct), 2)
                if direct and through_gateway
                else None
            ),
            "note": (
                "In-process ASGI, no sockets, throwaway SQLite: this is the gateway's "
                "compute and durable-write overhead, not a network measurement."
            ),
        }
        self.events.emit("latency_measured", latency=self.latency)
        line(
            f"  direct median {self.latency['direct_median_ms']} ms, gateway median "
            f"{self.latency['gateway_median_ms']} ms, added "
            f"{self.latency['added_by_gateway_ms']} ms ({self.latency_samples} samples)"
        )
        self.events.scenario = None

    # -- orchestration ---------------------------------------------------------- #

    async def run(self) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        banner("Failure laboratory: the action completed, but the response was lost")
        line()
        line(f"  workflow: pay one vendor invoice (${AMOUNT_USD} to {VENDOR})")
        line(f"  fault:    {FAULT_LOST_RESPONSE}")
        line(f"  run dir:  {self.run_dir}")

        async with self.rail.lifespan():
            # Startup is inside the boundary: it acquires the database and the
            # upstream client before it can fail, and `stop` is null-safe, so a
            # partial start still gets cleaned up.
            try:
                await self.gateway.start()

                banner("1. The existing integration")
                await self.existing_control()
                await self.existing_lost_response()

                banner("2. The native-control baseline")
                await self.native_control()
                await self.native_lost_response()
                await self.native_agent_restart()
                await self.native_key_conflict()

                banner("3. The gateway")
                await self.gateway_control()
                await self.gateway_lost_at_agent_hop(
                    scenario="gateway.lost_response.agent_hop",
                    configuration=CONFIG_GATEWAY,
                    native_key=False,
                )
                await self.gateway_lost_at_downstream_hop(
                    scenario="gateway.lost_response.downstream_hop",
                    configuration=CONFIG_GATEWAY,
                    native_key=False,
                )
                await self.gateway_agent_restart()
                await self.gateway_key_conflict()

                banner("4. The baseline plus the gateway")
                await self.gateway_lost_at_agent_hop(
                    scenario="gateway_native.lost_response.agent_hop",
                    configuration=CONFIG_GATEWAY_NATIVE,
                    native_key=True,
                )
                await self.gateway_lost_at_downstream_hop(
                    scenario="gateway_native.lost_response.downstream_hop",
                    configuration=CONFIG_GATEWAY_NATIVE,
                    native_key=True,
                )

                banner("5. Added latency")
                await self.measure_latency()
            finally:
                await self.gateway.stop()

        summary = self.summarize(started_at)
        self.write_artifacts(summary)
        self.events.close()
        return summary

    # -- summary and artifacts ---------------------------------------------------- #

    def _by_scenario(self) -> dict[str, Measurement]:
        return {m.scenario: m for m in self.measurements}

    def summarize(self, started_at: datetime) -> dict[str, Any]:
        by = self._by_scenario()

        def effects(name: str) -> int | None:
            m = by.get(name)
            return None if m is None else m.downstream_effects

        def debits(name: str) -> int | None:
            m = by.get(name)
            return None if m is None else m.debits

        def unresolved(name: str) -> int | None:
            m = by.get(name)
            return None if m is None else m.unresolved_outcomes

        native_effects = effects("native.lost_response")
        gateway_agent_hop_effects = effects("gateway.lost_response.agent_hop")
        native_handles = (
            effects("native.lost_response") == 1
            and by["native.lost_response"].attempts[-1].replayed_by_downstream is True
            if "native.lost_response" in by and by["native.lost_response"].attempts
            else False
        )
        verdicts = {
            "existing_integration_duplicates_the_action": effects(
                "existing.lost_response"
            )
            == 2,
            "native_baseline_already_handles_this_fault": bool(native_handles),
            "gateway_reduces_effects_vs_existing": (
                effects("gateway.lost_response.agent_hop") == 1
                and effects("gateway.lost_response.downstream_hop") == 1
            ),
            "gateway_reduces_effects_vs_native_baseline": (
                gateway_agent_hop_effects is not None
                and native_effects is not None
                and gateway_agent_hop_effects < native_effects
            ),
            "gateway_never_redispatched_after_ambiguity": (
                (by.get("gateway.lost_response.downstream_hop") is not None)
                and by["gateway.lost_response.downstream_hop"].gateway_dispatches == 1
                and (by.get("gateway_native.lost_response.downstream_hop") is not None)
                and by["gateway_native.lost_response.downstream_hop"].gateway_dispatches
                == 1
            ),
            "gateway_retained_the_charge_after_ambiguity": (
                debits("gateway.lost_response.downstream_hop") == 1
                and (by.get("gateway.lost_response.downstream_hop") is not None)
                and by["gateway.lost_response.downstream_hop"].refunds == 0
            ),
            "agent_restart_with_a_new_key_defeats_every_configuration": (
                effects("native.agent_restart_new_key") == 2
                and effects("gateway.agent_restart_new_key") == 2
            ),
        }

        limitations = [
            "A new business-operation key after an agent restart is a new action in "
            "every configuration: the native baseline paid twice, and the gateway "
            "paid twice with two correct receipts and two debits. Identity is the "
            "caller's key; nothing here recovers an identity the caller lost.",
            "When the downstream honors idempotency keys and the client persists "
            "them, the gateway does not reduce downstream effects for this fault "
            f"(native {effects('native.lost_response')} vs gateway "
            f"{effects('gateway.lost_response.agent_hop')}). What it adds there is "
            "an independent signed record, a debit that cannot be taken twice, and "
            "protection that does not depend on the downstream's own controls.",
            "delivery_uncertain is preserved, not resolved. The gateway retains the "
            "charge, signs the ambiguity, and never redispatches; the harness's "
            "independent record shows the effect landed once, and the gateway does "
            "not know that. Closing it is the caller's and the downstream's job.",
            "The fault is injected at the HTTP transport of in-process servers, "
            "immediately after the downstream returned. It exercises the same "
            "adapter code a socket timeout would, but it is not a TCP-level fault, "
            "not a process crash, and not a database failover. Crash boundaries are "
            "covered by the PostgreSQL process-kill proof, not by this lab.",
            "The payment rail is simulated. It moves no money and its idempotency "
            "semantics are a model of a documented payment API, not that API.",
            "Latency is measured in-process with no network and throwaway SQLite; "
            "it bounds the gateway's own overhead only.",
        ]

        ok = all(m.ok for m in self.measurements) and all(
            verdicts[name]
            for name in (
                "existing_integration_duplicates_the_action",
                "native_baseline_already_handles_this_fault",
                "gateway_reduces_effects_vs_existing",
                "gateway_never_redispatched_after_ambiguity",
                "gateway_retained_the_charge_after_ambiguity",
                "agent_restart_with_a_new_key_defeats_every_configuration",
            )
        )
        if verdicts["gateway_reduces_effects_vs_native_baseline"]:
            # The lab must not be able to "win" against a correct baseline.
            ok = False

        return {
            "lab": "failure-lab",
            "schema_version": 1,
            "run_id": RUN_ID,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "workflow": (
                f"vendor payout: one invoice, ${AMOUNT_USD} to {VENDOR}, through "
                f"one tool ({UPSTREAM_TOOL})"
            ),
            "fault": {
                "name": FAULT_LOST_RESPONSE,
                "mechanism": (
                    "an HTTP transport below the caller forwards the tools/call, "
                    "waits for the server to finish, discards the response, and "
                    "raises a read timeout"
                ),
                "hops": [
                    HOP_AGENT_DOWNSTREAM,
                    HOP_AGENT_GATEWAY,
                    HOP_GATEWAY_DOWNSTREAM,
                ],
                "armed_per": "one tool call, then disarmed",
            },
            "software": software_versions(),
            "gateway_configuration": self.gateway.configuration,
            "scenarios": [m.to_dict() for m in self.measurements],
            "comparison": {
                "downstream_effects": {
                    "existing": effects("existing.lost_response"),
                    "native": effects("native.lost_response"),
                    "gateway_agent_hop": effects("gateway.lost_response.agent_hop"),
                    "gateway_downstream_hop": effects(
                        "gateway.lost_response.downstream_hop"
                    ),
                    "gateway_native_agent_hop": effects(
                        "gateway_native.lost_response.agent_hop"
                    ),
                    "gateway_native_downstream_hop": effects(
                        "gateway_native.lost_response.downstream_hop"
                    ),
                },
                "debits": {
                    "gateway_agent_hop": debits("gateway.lost_response.agent_hop"),
                    "gateway_downstream_hop": debits(
                        "gateway.lost_response.downstream_hop"
                    ),
                },
                "unresolved_outcomes": {
                    "existing": unresolved("existing.lost_response"),
                    "native": unresolved("native.lost_response"),
                    "gateway_agent_hop": unresolved("gateway.lost_response.agent_hop"),
                    "gateway_downstream_hop": unresolved(
                        "gateway.lost_response.downstream_hop"
                    ),
                },
                "agent_restart_new_key": {
                    "native": effects("native.agent_restart_new_key"),
                    "gateway": effects("gateway.agent_restart_new_key"),
                },
            },
            "verdicts": verdicts,
            "latency": self.latency,
            "limitations": limitations,
            "not_covered_here": {
                "concurrent_retries_same_key": "make trust-conformance-live (15-way), tests/test_permit_postgres_concurrency.py",
                "crash_before_and_after_dispatch": "make prove-crash-recovery (tests/test_mcp_postgres_multiprocess.py)",
                "budget_race": "tests/test_permits.py, postgres_permit_concurrency CI job",
                "tampered_receipt_or_untrusted_key": "make demo-ambiguous-retry, make prove-trust-plane",
            },
            "artifacts": {
                "run_dir": str(self.run_dir),
                "report": str(self.run_dir / "report.txt"),
                "summary": str(self.run_dir / "report.json"),
                "events": str(self.run_dir / "events.jsonl"),
                "effects": str(self.run_dir / "effects.jsonl"),
                "config": str(self.run_dir / "config.json"),
                "gateway_db": str(GATEWAY_DB),
            },
            "reproduce": {
                "command": "make failure-lab",
                "direct": "python scripts/failure_lab.py --output-dir data/failure-lab",
                "argv": sys.argv,
            },
        }

    def write_artifacts(self, summary: dict[str, Any]) -> None:
        (self.run_dir / "report.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        (self.run_dir / "report.txt").write_text(
            render_report(summary), encoding="utf-8"
        )
        config = {
            "run_id": RUN_ID,
            "argv": sys.argv,
            "started_at": summary["started_at"],
            "software": summary["software"],
            "environment": {
                name: os.environ.get(name)
                for name in (
                    "ENVIRONMENT",
                    "TRUST_MODE_ENABLED",
                    "ALLOW_LEGACY_UNPERMITTED_MCP",
                    "ENABLE_PROOF_SURFACES",
                    "TRUST_SIGNING_KEY_ID",
                )
            },
            "gateway_configuration": summary["gateway_configuration"],
            "fault": summary["fault"],
            "workflow": summary["workflow"],
            "latency_samples": self.latency_samples,
            "scenario_sequence": [
                {
                    "scenario": m.scenario,
                    "configuration": m.configuration,
                    "fault": m.fault,
                    "fault_hop": m.fault_hop,
                    "invoice": m.invoice,
                    "checks": m.checks,
                }
                for m in self.measurements
            ],
        }
        (self.run_dir / "config.json").write_text(
            json.dumps(config, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )


# --------------------------------------------------------------------------- #
# Report                                                                          #
# --------------------------------------------------------------------------- #


def software_versions() -> dict[str, Any]:
    version: str | None
    try:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        version = None
    commit: str | None
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.SubprocessError):
        commit = None

    def dist(name: str) -> str | None:
        try:
            return importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            return None

    return {
        "agent_native_middleware": version,
        "git_commit": commit,
        "python": platform.python_version(),
        "mcp": dist("mcp"),
        "httpx": dist("httpx"),
        "fastapi": dist("fastapi"),
        "sqlalchemy": dist("sqlalchemy"),
    }


def _scenario_block(summary: dict[str, Any], scenario: str) -> dict[str, Any] | None:
    for item in summary["scenarios"]:
        if item["scenario"] == scenario:
            return item
    return None


def render_report(summary: dict[str, Any]) -> str:
    software = summary["software"]
    version_line = (
        f"agent-native-middleware {software.get('agent_native_middleware')}"
        + (f" @ {software['git_commit']}" if software.get("git_commit") else "")
        + f"; mcp {software.get('mcp')}; httpx {software.get('httpx')}; "
        f"python {software.get('python')}"
    )
    out: list[str] = []
    out.append(f"Workflow tested: {summary['workflow']}")
    out.append(f"Integration version: {version_line}")
    out.append(f"Fault injected: {summary['fault']['name']}")
    out.append(f"Run: {summary['run_id']}  ok={str(summary['ok']).lower()}")
    out.append("")

    def block(title: str, scenario: str, *, gateway: bool) -> None:
        item = _scenario_block(summary, scenario)
        out.append(f"{title}:")
        if item is None:
            out.append("  (not run)")
            out.append("")
            return
        attempts = item["attempts"]
        out.append(f"  Tool call attempts:      {len(attempts)}")
        if gateway:
            out.append(f"  Gateway dispatches:      {item['gateway_dispatches']}")
        out.append(f"  Downstream effects:      {item['downstream_effects']}")
        if gateway:
            out.append(f"  Debits:                  {item['debits']}")
        out.append(f"  Unresolved outcomes:     {item['unresolved_outcomes']}")
        out.append(f"  Final known outcome:     {item['final_known_outcome']}")
        for note in item.get("notes", []):
            out.append(f"  Note: {note}")
        out.append("")

    block(
        "Existing integration (no idempotency key)",
        "existing.lost_response",
        gateway=False,
    )
    block(
        "Native idempotency, correctly used (key persisted across the retry)",
        "native.lost_response",
        gateway=False,
    )
    block(
        "With gateway, response lost between gateway and agent",
        "gateway.lost_response.agent_hop",
        gateway=True,
    )
    block(
        "With gateway, response lost between downstream and gateway",
        "gateway.lost_response.downstream_hop",
        gateway=True,
    )
    block(
        "Native idempotency plus gateway, response lost between gateway and agent",
        "gateway_native.lost_response.agent_hop",
        gateway=True,
    )
    block(
        "Native idempotency plus gateway, response lost between downstream and gateway",
        "gateway_native.lost_response.downstream_hop",
        gateway=True,
    )

    restart = summary["comparison"]["agent_restart_new_key"]
    out.append(
        "Agent restart, new key for the same invoice (no configuration recovers this):"
    )
    out.append(f"  Native baseline effects: {restart['native']}")
    out.append(f"  Gateway effects:         {restart['gateway']}")
    out.append("")

    latency = summary.get("latency") or {}
    if latency.get("samples"):
        out.append("Added latency (in-process, no network):")
        out.append(f"  Direct median:           {latency.get('direct_median_ms')} ms")
        out.append(f"  Through gateway median:  {latency.get('gateway_median_ms')} ms")
        out.append(
            f"  Added by the gateway:    {latency.get('added_by_gateway_ms')} ms"
        )
        out.append(f"  Samples per side:        {latency.get('samples')}")
        out.append("")

    out.append("Remaining limitations:")
    for limitation in summary["limitations"]:
        out.append(f"  - {limitation}")
    out.append("")

    out.append("Not covered by this run (see the named proofs):")
    for name, where in summary["not_covered_here"].items():
        out.append(f"  - {name.replace('_', ' ')}: {where}")
    out.append("")

    artifacts = summary["artifacts"]
    out.append("Reproduce:")
    out.append(f"  {summary['reproduce']['command']}")
    out.append(f"  saved run: {artifacts['run_dir']}")
    out.append(
        "  files: report.txt, report.json, events.jsonl (event history), "
        "effects.jsonl (independent downstream record), config.json (versions, "
        "configuration, scenario sequence and checks), gateway.db"
    )
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Entry point                                                                     #
# --------------------------------------------------------------------------- #


async def run_lab(*, json_output: bool, latency_samples: int) -> dict[str, Any]:
    global PRINT_STEPS
    PRINT_STEPS = not json_output
    lab = Lab(latency_samples=latency_samples)
    summary = await lab.run()
    if json_output:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    else:
        banner("Report")
        print()
        print(render_report(summary))
    return summary


def main() -> None:
    summary = asyncio.run(
        run_lab(json_output=ARGS.json, latency_samples=ARGS.latency_samples)
    )
    if not summary["ok"]:
        failed = [
            f"{item['scenario']}: {check['check']} (observed {check['observed']!r}, "
            f"expected {check['expected']!r})"
            for item in summary["scenarios"]
            for check in item["checks"]
            if not check["passed"]
        ]
        for verdict, held in summary["verdicts"].items():
            if verdict == "gateway_reduces_effects_vs_native_baseline":
                if held:
                    failed.append(
                        "the lab must not out-perform a correct native baseline on "
                        "this fault; it reported that it did"
                    )
            elif not held:
                failed.append(f"verdict did not hold: {verdict}")
        print(
            "failure-lab: expectations broke; see "
            f"{summary['artifacts']['run_dir']}\n  " + "\n  ".join(failed),
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
