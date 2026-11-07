"""Retry a Stripe TEST MODE refund behind Agent Middleware without refunding twice.

This example runs the real gateway in process (SQLite, memory state) and puts
two governed upstream MCP tools behind it:

* tool 1: ``partner.echo`` from the repository's partner smoke server
  (``gateway/app/partner_mcp.py``), unchanged.
* tool 2: a Stripe test mode ``refunds.create`` for USD 1.00 (``amount=100``,
  currency ``usd``), served by a small local MCP wrapper defined below.

An agent wallet gets one permit that covers both tools. The agent calls the
refund tool twice with the SAME idempotency key. The example then proves and
prints:

1. both dispatches return the same ``receipt_id``;
2. the gateway ledger holds exactly one debit for that idempotency key;
3. the second dispatch never reaches Stripe (the wrapper's Stripe call counter
   stays at 1, and Stripe lists exactly one new refund for the payment).

Safety
------
* ``STRIPE_SECRET_KEY`` is read from the environment only. The example refuses
  to run (exit 3) unless it starts with ``sk_test_`` or ``rk_test_``. The key is
  never printed or logged, and it is never used as an MCP bearer credential.
* The refund tool accepts only ``amount=100`` and ``currency=usd``.
* The gateway's idempotency key is forwarded to Stripe as its
  ``Idempotency-Key`` header (prefixed ``amw-refund-``), so Stripe also
  deduplicates if a dispatch were ever repeated.

Configuration
-------------
Environment variables read by this example:

=========================== ==================================================
``STRIPE_SECRET_KEY``       Required. Stripe test mode secret or restricted key.
``STRIPE_TEST_PAYMENT_INTENT`` Optional. A succeeded test mode PaymentIntent of
                            at least USD 1.00 to refund. When unset, the
                            example creates one with Stripe's documented test
                            payment method ``pm_card_visa``.
``UPSTREAM_MCP_ORIGIN_2``   Optional, default ``http://localhost:9102``. Origin
                            of the refund MCP server. Must be a loopback origin
                            because the wrapper runs in this process.
``UPSTREAM_TOOL_2``         Optional, default ``stripe.refunds.create``. MCP
                            tool name of the refund tool, also used as its
                            public gateway tool ID.
``UPSTREAM_MCP_ORIGIN_1``   Optional, default ``http://localhost:9101``. Origin
                            of the ``partner.echo`` server (loopback only).
=========================== ==================================================

Mapping to gateway configuration
--------------------------------
The gateway has ONE upstream settings block (see
``gateway/app/core/config.py`` and ``docs/partner-first-tool-runbook.md``):
``MCP_UPSTREAM_URL``, ``MCP_UPSTREAM_TOOL_NAME``, ``MCP_UPSTREAM_PUBLIC_TOOL_ID``,
``MCP_UPSTREAM_BEARER_TOKEN``, ``MCP_UPSTREAM_CREDITS_PER_CALL``. There are no
``UPSTREAM_MCP_ORIGIN_2`` or ``UPSTREAM_TOOL_2`` gateway settings, and this
example does not add any. Instead it maps each tool onto that same block:

=========================== ==================================================
Example env                 Gateway setting used to build the tool's config
=========================== ==================================================
``UPSTREAM_MCP_ORIGIN_1``   ``MCP_UPSTREAM_URL = <origin>/mcp`` (tool 1)
``partner.echo``            ``MCP_UPSTREAM_TOOL_NAME`` and ``_PUBLIC_TOOL_ID``
``UPSTREAM_MCP_ORIGIN_2``   ``MCP_UPSTREAM_URL = <origin>/mcp`` (tool 2)
``UPSTREAM_TOOL_2``         ``MCP_UPSTREAM_TOOL_NAME`` and ``_PUBLIC_TOOL_ID``
=========================== ==================================================

Each tool's ``UpstreamMcpConfiguration`` is built with the gateway's own
validator, ``UpstreamMcpConfiguration.from_settings(Settings(...))``, wrapped in
``UpstreamMcpAdapter`` (real MCP ``initialize`` and ``tools/list`` discovery)
and registered with ``ServiceRegistry.register_upstream``: the exact calls
``register_configured_upstream_mcp`` makes for its single tool. The example
does not call ``register_configured_upstream_mcp`` because it first removes
every upstream tool. Each MCP server gets its own random bearer token.

Permits, idempotency and receipts use the documented gateway endpoints only:
``POST /v1/billing/wallets/sponsor``, ``POST /v1/billing/wallets/agent``,
``POST /v1/api-keys``, ``POST /v1/permits`` and ``POST /mcp/messages`` with
``params.mcpContext`` carrying ``wallet_id``, ``permit_id`` and
``idempotency_key``.

Run from the repository root:

    STRIPE_SECRET_KEY=sk_test_... \\
    uv run --no-project --isolated --python 3.12 \\
      --with-requirements gateway/requirements.txt \\
      --with-editable './sdk/python[verify,mcp]' \\
      python examples/permit_stripe_refund_retry.py

THIS IS A TEST MODE EXAMPLE. It moves no real money. Do not adapt it to a
live key.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = Path(__file__).resolve().parent

TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")
SAFETY_REFUSAL_EXIT = 3
STRIPE_API = "https://api.stripe.com"
REFUND_AMOUNT = 100
REFUND_CURRENCY = "usd"
ECHO_TOOL = "partner.echo"
DEFAULT_ORIGIN_1 = "http://localhost:9101"
DEFAULT_ORIGIN_2 = "http://localhost:9102"
DEFAULT_TOOL_2 = "stripe.refunds.create"
CREDITS_PER_CALL = "1"
RETRY_KEY_PREFIX = "t03-stripe-refund-"
IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"

# Tests replace this with an ``httpx.MockTransport``. ``None`` means the real
# network transport to api.stripe.com.
stripe_transport: httpx.AsyncBaseTransport | None = None


class SafetyRefusal(SystemExit):
    """Refuse to run. The message never contains the key."""

    def __init__(self, message: str) -> None:
        print(f"refusing to run: {message}", file=sys.stderr)
        super().__init__(SAFETY_REFUSAL_EXIT)


def require_test_key(environ: Mapping[str, str]) -> str:
    """Return STRIPE_SECRET_KEY only if it is a Stripe test mode key."""
    key = environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise SafetyRefusal("STRIPE_SECRET_KEY is not set")
    if not key.startswith(TEST_KEY_PREFIXES):
        raise SafetyRefusal(
            "STRIPE_SECRET_KEY is not a test mode key "
            "(it must start with sk_test_ or rk_test_)"
        )
    if key != key.strip() or any(ord(char) < 33 for char in key):
        raise SafetyRefusal("STRIPE_SECRET_KEY contains whitespace")
    return key


@dataclass(frozen=True)
class UpstreamSpec:
    origin: str
    tool: str

    @property
    def url(self) -> str:
        return f"{self.origin}/mcp"

    @property
    def host(self) -> str:
        return urlsplit(self.origin).netloc


def loopback_origin(name: str, value: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "http"
        or host not in {"localhost", "127.0.0.1"}
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise SafetyRefusal(
            f"{name} must be a loopback origin such as {DEFAULT_ORIGIN_2}; "
            "the MCP server for this example runs in this process"
        )
    return value.rstrip("/")


def read_upstreams(environ: Mapping[str, str]) -> tuple[UpstreamSpec, UpstreamSpec]:
    origin_1 = loopback_origin(
        "UPSTREAM_MCP_ORIGIN_1", environ.get("UPSTREAM_MCP_ORIGIN_1", DEFAULT_ORIGIN_1)
    )
    origin_2 = loopback_origin(
        "UPSTREAM_MCP_ORIGIN_2", environ.get("UPSTREAM_MCP_ORIGIN_2", DEFAULT_ORIGIN_2)
    )
    tool_2 = environ.get("UPSTREAM_TOOL_2", DEFAULT_TOOL_2)
    if origin_1 == origin_2:
        raise SafetyRefusal(
            "UPSTREAM_MCP_ORIGIN_1 and UPSTREAM_MCP_ORIGIN_2 must differ"
        )
    if tool_2 == ECHO_TOOL:
        raise SafetyRefusal(f"UPSTREAM_TOOL_2 must not be {ECHO_TOOL}")
    return UpstreamSpec(origin_1, ECHO_TOOL), UpstreamSpec(origin_2, tool_2)


# --- Stripe (test mode only) -------------------------------------------------


def stripe_client(key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=STRIPE_API,
        headers={"Authorization": f"Bearer {key}"},
        transport=stripe_transport,
        timeout=20.0,
        follow_redirects=False,
    )


def _stripe_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code = error.get("code") or error.get("type") or "unknown"
    return f"stripe returned HTTP {response.status_code} ({code})"


async def create_test_payment_intent(key: str) -> str:
    """Create a confirmed USD 1.00 test mode PaymentIntent and return its id."""
    async with stripe_client(key) as client:
        response = await client.post(
            "/v1/payment_intents",
            data={
                "amount": str(REFUND_AMOUNT),
                "currency": REFUND_CURRENCY,
                "payment_method": "pm_card_visa",
                "payment_method_types[]": "card",
                "confirm": "true",
                "description": "Agent Middleware T-03 test mode refund example",
            },
        )
    if response.status_code != 200:
        raise RuntimeError(_stripe_error(response))
    body = response.json()
    if body.get("livemode") is not False:
        raise RuntimeError("stripe reported a live mode object; stopping")
    if body.get("status") != "succeeded":
        raise RuntimeError("test PaymentIntent did not succeed")
    return str(body["id"])


STRIPE_LIST_PAGE_LIMIT = 100


async def list_refunds(key: str, payment_intent: str) -> list[dict[str, Any]]:
    """Return every refund for ``payment_intent``, following Stripe pagination."""
    refunds: list[dict[str, Any]] = []
    params = {"payment_intent": payment_intent, "limit": str(STRIPE_LIST_PAGE_LIMIT)}
    async with stripe_client(key) as client:
        while True:
            response = await client.get("/v1/refunds", params=params)
            if response.status_code != 200:
                raise RuntimeError(_stripe_error(response))
            body = response.json()
            page = list(body.get("data", []))
            refunds.extend(page)
            if not body.get("has_more"):
                return refunds
            if not page:
                raise RuntimeError("stripe reported more refunds but sent none")
            params = {**params, "starting_after": str(page[-1]["id"])}


@dataclass
class StripeRefundServer:
    """Local MCP wrapper around Stripe ``refunds.create``."""

    key: str = field(repr=False)
    tool_name: str
    allowed_host: str
    refund_calls: list[dict[str, str]] = field(default_factory=list)

    def build_app(self) -> Any:
        server = FastMCP(
            "amw-t03-stripe-refund",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[self.allowed_host],
            ),
        )

        @server.tool(
            name=self.tool_name,
            description="Stripe test mode refunds.create for exactly USD 1.00.",
        )
        async def refund(
            payment_intent: str, amount: int, currency: str, ctx: Context
        ) -> dict[str, Any]:
            return await self.refund(payment_intent, amount, currency, ctx)

        return server.streamable_http_app()

    async def refund(
        self, payment_intent: str, amount: int, currency: str, ctx: Context
    ) -> dict[str, Any]:
        if amount != REFUND_AMOUNT or currency != REFUND_CURRENCY:
            raise ValueError("this example refunds exactly amount=100 currency=usd")
        if not payment_intent.startswith("pi_"):
            raise ValueError("payment_intent must be a Stripe PaymentIntent id")
        meta = ctx.request_context.meta
        forwarded = (
            meta.model_dump(mode="json", by_alias=True, exclude_none=True)
            if meta is not None
            else {}
        )
        gateway_key = forwarded.get(IDEMPOTENCY_META_KEY)
        if not isinstance(gateway_key, str) or not gateway_key:
            raise ValueError("missing forwarded gateway idempotency key")
        stripe_idempotency_key = f"amw-refund-{gateway_key}"
        self.refund_calls.append(
            {
                "payment_intent": payment_intent,
                "stripe_idempotency_key": stripe_idempotency_key,
            }
        )
        async with stripe_client(self.key) as client:
            response = await client.post(
                "/v1/refunds",
                data={"payment_intent": payment_intent, "amount": str(amount)},
                headers={"Idempotency-Key": stripe_idempotency_key},
            )
        if response.status_code != 200:
            raise RuntimeError(_stripe_error(response))
        refund = response.json()
        if refund.get("amount") != REFUND_AMOUNT or refund.get("currency") != (
            REFUND_CURRENCY
        ):
            raise RuntimeError("stripe refund amount or currency differs")
        return {
            "refund_id": refund.get("id"),
            "status": refund.get("status"),
            "amount": refund.get("amount"),
            "currency": refund.get("currency"),
            "payment_intent": refund.get("payment_intent"),
            "stripe_idempotency_key": stripe_idempotency_key,
        }


# --- Gateway ------------------------------------------------------------------


def configure_gateway(database: Path, echo: UpstreamSpec) -> str:
    """Set in-process gateway env before any gateway import. Returns admin key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    signing_seed = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    admin_key = secrets.token_urlsafe(32)
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "STATE_BACKEND": "memory",
            "REDIS_URL": "",
            "ENVIRONMENT": "local",
            "PUBLIC_URL": "http://sandbox",
            "VALID_API_KEYS": admin_key,
            "TRUST_MODE_ENABLED": "true",
            "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
            "ENABLE_PROOF_SURFACES": "false",
            "ENABLE_DEMO_TENANT": "false",
            "MCP_UPSTREAM_ENABLED": "false",
            "TRUST_SIGNING_KEY_ID": "t03-stripe-refund-ed25519",
            "TRUST_SIGNING_PRIVATE_KEY_B64": base64.b64encode(signing_seed).decode(),
            "RATE_LIMIT_PER_MINUTE": "1000000",
            # app.partner_mcp validates these at import time.
            "PARTNER_MCP_BEARER_TOKEN": secrets.token_urlsafe(32),
            "PARTNER_MCP_ALLOWED_HOSTS": echo.host,
        }
    )
    for path in (ROOT / "gateway", ROOT / "sdk" / "python" / "src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return admin_key


def upstream_configuration(spec: UpstreamSpec, bearer_token: str) -> Any:
    """Build one tool's config with the gateway's own validator."""
    from app.core.config import Settings
    from app.services.upstream_mcp import UpstreamMcpConfiguration

    return UpstreamMcpConfiguration.from_settings(
        Settings(
            _env_file=None,
            ENVIRONMENT="local",
            MCP_UPSTREAM_ENABLED=True,
            MCP_UPSTREAM_URL=spec.url,
            MCP_UPSTREAM_TOOL_NAME=spec.tool,
            MCP_UPSTREAM_PUBLIC_TOOL_ID=spec.tool,
            MCP_UPSTREAM_BEARER_TOKEN=bearer_token,
            MCP_UPSTREAM_CREDITS_PER_CALL=CREDITS_PER_CALL,
        )
    )


async def register_upstream_tool(
    spec: UpstreamSpec, mcp_app: Any, bearer_token: str
) -> httpx.AsyncClient:
    """Discover and register one upstream tool exactly as the gateway does."""
    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry
    from app.services.upstream_mcp import UpstreamMcpAdapter

    configuration = upstream_configuration(spec, bearer_token)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_app),
        base_url=spec.origin,
        headers={"Authorization": f"Bearer {bearer_token}"},
        follow_redirects=False,
    )
    adapter = UpstreamMcpAdapter(configuration, http_client=client)
    discovered = await adapter.discover_tool()
    get_service_registry().register_upstream(
        service_id=configuration.public_tool_id,
        name=discovered.name,
        description=discovered.description,
        category=ServiceCategory.PLATFORM_FEE,
        executor=adapter,
        input_schema=discovered.input_schema,
        output_schema=discovered.output_schema,
        credits_per_unit=float(configuration.credits_per_call),
        upstream_tool_name=discovered.name,
        upstream_origin=configuration.origin,
        credits_per_unit_exact=format(configuration.credits_per_call, "f"),
    )
    return client


async def _post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    headers: dict[str, str],
    expected_status: int,
) -> dict[str, Any]:
    response = await client.post(path, json=body, headers=headers)
    if response.status_code != expected_status:
        raise AssertionError(f"{path}: HTTP {response.status_code}")
    return response.json()


def _tool_call(
    tool: str,
    arguments: dict[str, Any],
    *,
    wallet_id: str,
    permit_id: str,
    idempotency_key: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments,
            "mcpContext": {
                "wallet_id": wallet_id,
                "permit_id": permit_id,
                "idempotency_key": idempotency_key,
            },
        },
    }


async def count_debits(wallet_id: str, idempotency_key: str) -> tuple[int, int]:
    """Return (debits for this idempotency key, all debits on the wallet)."""
    from app.db.database import get_session_factory
    from app.db.models import IdempotencyRecordModel, LedgerEntryModel
    from app.services.idempotency import GOVERNED_MCP_IDEMPOTENCY_ENDPOINT
    from sqlalchemy import func, select

    factory = get_session_factory()
    async with factory() as session:
        record_ids = (
            (
                await session.execute(
                    select(IdempotencyRecordModel.record_id).where(
                        IdempotencyRecordModel.wallet_id == wallet_id,
                        IdempotencyRecordModel.endpoint
                        == GOVERNED_MCP_IDEMPOTENCY_ENDPOINT,
                        IdempotencyRecordModel.idempotency_key == idempotency_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        keyed = await session.scalar(
            select(func.count())
            .select_from(LedgerEntryModel)
            .where(
                LedgerEntryModel.wallet_id == wallet_id,
                LedgerEntryModel.action == "debit",
                LedgerEntryModel.operation_key.in_(record_ids),
            )
        )
        total = await session.scalar(
            select(func.count())
            .select_from(LedgerEntryModel)
            .where(
                LedgerEntryModel.wallet_id == wallet_id,
                LedgerEntryModel.action == "debit",
            )
        )
    return int(keyed or 0), int(total or 0)


@dataclass(frozen=True)
class RetryEvidence:
    idempotency_key: str
    first_receipt_id: str
    second_receipt_id: str
    debits_for_key: int
    wallet_debits: int
    stripe_calls_after_first: int
    stripe_calls_after_second: int
    stripe_refunds_before: int
    stripe_refunds_after: int
    refund_id: str
    echo_receipt_id: str

    @property
    def same_receipt(self) -> bool:
        return self.first_receipt_id == self.second_receipt_id

    @property
    def second_dispatch_called_stripe(self) -> bool:
        return self.stripe_calls_after_second != self.stripe_calls_after_first


async def run(
    admin_key: str,
    key: str,
    echo: UpstreamSpec,
    refund_spec: UpstreamSpec,
    payment_intent: str | None,
) -> RetryEvidence:
    from app.db.database import close_db, init_db
    from app.main import app
    from app.partner_mcp import create_partner_mcp_app
    from app.services.service_registry import get_service_registry

    echo_app = create_partner_mcp_app(os.environ)
    echo_token = os.environ["PARTNER_MCP_BEARER_TOKEN"]
    refund_token = secrets.token_urlsafe(32)
    refund_server = StripeRefundServer(
        key=key, tool_name=refund_spec.tool, allowed_host=refund_spec.host
    )
    refund_app = refund_server.build_app()

    if payment_intent is None:
        payment_intent = await create_test_payment_intent(key)

    await init_db()
    registry = get_service_registry()
    upstream_clients: list[httpx.AsyncClient] = []
    try:
        async with (
            echo_app.router.lifespan_context(echo_app),
            refund_app.router.lifespan_context(refund_app),
        ):
            upstream_clients.append(
                await register_upstream_tool(echo, echo_app, echo_token)
            )
            upstream_clients.append(
                await register_upstream_tool(refund_spec, refund_app, refund_token)
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://sandbox"
            ) as client:
                evidence = await _exercise(
                    client,
                    admin_key,
                    key,
                    echo,
                    refund_spec,
                    refund_server,
                    payment_intent,
                )
    finally:
        for upstream_client in upstream_clients:
            await upstream_client.aclose()
        registry.unregister_local(echo.tool)
        registry.unregister_local(refund_spec.tool)
        await close_db()
    return evidence


async def _exercise(
    client: httpx.AsyncClient,
    admin_key: str,
    key: str,
    echo: UpstreamSpec,
    refund_spec: UpstreamSpec,
    refund_server: StripeRefundServer,
    payment_intent: str,
) -> RetryEvidence:
    admin = {"X-API-Key": admin_key}
    run_id = secrets.token_hex(6)
    sponsor = await _post(
        client,
        "/v1/billing/wallets/sponsor",
        {
            "sponsor_name": f"t03-{run_id}",
            "email": f"t03-{run_id}@example.com",
            "initial_credits": 100,
            "require_kyc": False,
        },
        admin,
        201,
    )
    agent = await _post(
        client,
        "/v1/billing/wallets/agent",
        {
            "sponsor_wallet_id": sponsor["wallet_id"],
            "agent_id": f"t03-agent-{run_id}",
            "budget_credits": 10,
            "daily_limit": 10,
        },
        admin,
        201,
    )
    wallet_id = agent["wallet_id"]
    api_key = await _post(
        client,
        "/v1/api-keys",
        {"wallet_id": wallet_id, "key_name": f"t03-{run_id}", "expires_in_days": 1},
        admin,
        201,
    )
    agent_headers = {"X-API-Key": api_key["api_key"]}
    tools = [echo.tool, refund_spec.tool]
    permit = await _post(
        client,
        "/v1/permits",
        {
            "issuer_wallet_id": wallet_id,
            "subject_wallet_id": wallet_id,
            "subject_key_id": api_key["key_id"],
            "allowed_tools": tools,
            "scopes": [*(f"tool:{tool}:invoke" for tool in tools), "billing:charge"],
            "max_credits": 3,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
        {**admin, "Idempotency-Key": f"t03-permit-{run_id}"},
        201,
    )
    permit_id = permit["permit_id"]

    # Tool 1: partner.echo stays reachable behind the same permit.
    echo_reply = await _post(
        client,
        "/mcp/messages",
        _tool_call(
            echo.tool,
            {"message": "tool 1 still routes"},
            wallet_id=wallet_id,
            permit_id=permit_id,
            idempotency_key=f"t03-echo-{run_id}",
            request_id="echo",
        ),
        agent_headers,
        200,
    )
    echo_result = echo_reply.get("result") or {}
    if echo_result.get("isError") is not False:
        raise AssertionError(f"{echo.tool} failed: {echo_reply.get('error')}")

    # Tool 2: two dispatches, same idempotency key, byte-identical body.
    idempotency_key = f"{RETRY_KEY_PREFIX}{run_id}"
    body = _tool_call(
        refund_spec.tool,
        {
            "payment_intent": payment_intent,
            "amount": REFUND_AMOUNT,
            "currency": REFUND_CURRENCY,
        },
        wallet_id=wallet_id,
        permit_id=permit_id,
        idempotency_key=idempotency_key,
        request_id="refund",
    )
    # Count existing refunds so a reused STRIPE_TEST_PAYMENT_INTENT still works.
    refunds_before = len(await list_refunds(key, payment_intent))
    first = await _post(client, "/mcp/messages", body, agent_headers, 200)
    first_result = first.get("result") or {}
    if first_result.get("isError") is not False:
        raise AssertionError(f"first refund failed: {first.get('error')}")
    calls_after_first = len(refund_server.refund_calls)

    second = await _post(client, "/mcp/messages", body, agent_headers, 200)
    second_result = second.get("result") or {}
    if second_result.get("isError") is not False:
        raise AssertionError(f"second refund failed: {second.get('error')}")
    calls_after_second = len(refund_server.refund_calls)

    debits_for_key, wallet_debits = await count_debits(wallet_id, idempotency_key)
    refunds_after = len(await list_refunds(key, payment_intent))
    structured = first_result.get("structuredContent") or {}
    return RetryEvidence(
        idempotency_key=idempotency_key,
        first_receipt_id=first_result["receipt"]["receipt_id"],
        second_receipt_id=second_result["receipt"]["receipt_id"],
        debits_for_key=debits_for_key,
        wallet_debits=wallet_debits,
        stripe_calls_after_first=calls_after_first,
        stripe_calls_after_second=calls_after_second,
        stripe_refunds_before=refunds_before,
        stripe_refunds_after=refunds_after,
        refund_id=str(structured.get("refund_id")),
        echo_receipt_id=echo_result["receipt"]["receipt_id"],
    )


def check(evidence: RetryEvidence) -> list[str]:
    """Return acceptance failures; empty means every property holds."""
    failures = []
    if not evidence.same_receipt:
        failures.append("the two dispatches returned different receipt_ids")
    if evidence.debits_for_key != 1:
        failures.append(
            f"expected exactly one debit for the key, found {evidence.debits_for_key}"
        )
    if evidence.stripe_calls_after_first != 1:
        failures.append("the first dispatch did not call Stripe exactly once")
    if evidence.second_dispatch_called_stripe:
        failures.append("the second dispatch called Stripe")
    new_refunds = evidence.stripe_refunds_after - evidence.stripe_refunds_before
    if new_refunds != 1:
        failures.append(f"Stripe lists {new_refunds} new refunds for the payment")
    return failures


def report(evidence: RetryEvidence) -> None:
    print("Stripe test mode refund retry behind Agent Middleware")
    print(f"  tool 1 ({ECHO_TOOL}) receipt_id: {evidence.echo_receipt_id}")
    print(f"  idempotency key:          {evidence.idempotency_key}")
    print(f"  dispatch 1 receipt_id:    {evidence.first_receipt_id}")
    print(f"  dispatch 2 receipt_id:    {evidence.second_receipt_id}")
    print(f"  same receipt_id:          {evidence.same_receipt}")
    print(f"  debits for this key:      {evidence.debits_for_key}")
    print(f"  Stripe refund calls:      {evidence.stripe_calls_after_second}")
    print(
        "  dispatch 2 called Stripe: "
        f"{'yes' if evidence.second_dispatch_called_stripe else 'no'}"
    )
    print(
        "  Stripe refunds on the PI: "
        f"{evidence.stripe_refunds_before} before, {evidence.stripe_refunds_after} after"
    )
    print(f"  Stripe refund id:         {evidence.refund_id}")


def main(environ: Mapping[str, str] | None = None) -> int:
    import asyncio

    env = os.environ if environ is None else environ
    key = require_test_key(env)
    echo, refund_spec = read_upstreams(env)
    payment_intent = env.get("STRIPE_TEST_PAYMENT_INTENT") or None
    with tempfile.TemporaryDirectory(prefix=".t03-stripe-") as state:
        admin_key = configure_gateway(Path(state) / "gateway.db", echo)
        evidence = asyncio.run(run(admin_key, key, echo, refund_spec, payment_intent))
    report(evidence)
    failures = check(evidence)
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("PASS: same receipt_id, exactly one debit, dispatch 2 never called Stripe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
