"""Disposable ASGI worker, controlled over an in-memory pipe.

Only this process owns the gateway application. No HTTP transport can reach a
network, and generated credentials and signing material never go into files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from dataclasses import asdict


def serve(connection, database_url: str, signing_key: str) -> None:
    """Run isolated gateway with test configuration and no network access."""
    # Configure before importing the application, without loading dotenv files.
    from app.core import config

    class LocalSettings(config.Settings):
        """Test-only settings that ignore environment and dotenv sources."""

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            # Only explicit test configuration and model defaults. Never load
            # inherited credentials, dotenv files, or file secret sources.
            return (init_settings,)

    def reject_network(*args, **kwargs):
        """Prevent any network socket connections during tests."""
        raise AssertionError("judge worker forbids network connections")

    socket.socket.connect = reject_network
    socket.socket.connect_ex = reject_network
    os.environ["ALLOW_METADATA_CREATE_ALL"] = "true"
    settings = LocalSettings(
        _env_file=None,
        DATABASE_URL=database_url,
        ENVIRONMENT="test",
        STATE_BACKEND="memory",
        REDIS_URL="",
        ENABLE_PROOF_SURFACES=False,
        ENABLE_DEMO_TENANT=True,
        DEMO_ALLOWED_TOOLS="partner.echo",
        DEMO_KEY_MAX_USES=None,
        TRUST_MODE_ENABLED=True,
        ALLOW_LEGACY_UNPERMITTED_MCP=False,
        TRUST_SIGNING_PRIVATE_KEY_B64=signing_key,
        RATE_LIMIT_PER_MINUTE=1000000,
    )
    config.get_settings = lambda: settings
    asyncio.run(_run(connection))


async def _run(connection) -> None:
    """Handle requests from test fixture until stop or error."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.db.database import close_db, get_session_factory, init_db
    from app.db.models import (
        IdempotencyRecordModel,
        LedgerEntryModel,
        McpDispatchAttemptModel,
        PermitModel,
        ReceiptModel,
        WalletModel,
    )
    from app.main import app
    from app.schemas.billing import ServiceCategory
    from app.services.mcp_dispatch_reconciliation import (
        McpDispatchReconciliationService,
    )
    from app.services.service_registry import get_service_registry
    from app.services.upstream_mcp import UpstreamMcpResult

    class Echo:
        """Test executor that echoes messages and supports fault injection."""

        dispatches = 0
        kill_during_dispatch = False

        async def call_tool(
            self, arguments, *, invocation_id, idempotency_key, before_dispatch
        ):
            """Echo the message argument or block for kill fault injection."""
            await before_dispatch()
            self.dispatches += 1
            if self.kill_during_dispatch:
                # The checkpoint committed. Keep the invocation in flight until
                # the parent sends SIGKILL, rather than raising a fake timeout.
                connection.send(
                    {"event": "dispatch_claimed", "dispatches": self.dispatches}
                )
                await asyncio.Event().wait()
            payload = {
                "content": [{"type": "text", "text": arguments["message"]}],
                "isError": False,
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            return UpstreamMcpResult(
                payload=payload,
                canonical_json=canonical,
                response_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                size_bytes=len(canonical.encode()),
                is_error=False,
            )

    echo = Echo()
    get_service_registry().register_upstream(
        service_id="partner.echo",
        name="partner.echo",
        description="Local judge echo executor",
        category=ServiceCategory.AGENT_COMMS,
        executor=echo,
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        credits_per_unit=1,
        upstream_tool_name="partner.echo",
        upstream_origin="https://partner.invalid",
    )
    await init_db()
    transport = ASGITransport(app=app, client=("203.0.113.240", 443))
    async with AsyncClient(
        transport=transport, base_url="http://judge.invalid"
    ) as client:
        connection.send({"event": "ready"})
        try:
            while True:
                command = connection.recv()
                operation = command["operation"]
                if operation == "stop":
                    break
                if operation == "request":
                    echo.kill_during_dispatch = command.pop("kill", False)
                    command.pop("operation")
                    response = await client.request(**command)
                    connection.send(
                        {"status": response.status_code, "body": response.json()}
                    )
                elif operation == "snapshot":
                    async with get_session_factory()() as session:
                        wallet_id = command["wallet_id"]
                        wallet = await session.get(WalletModel, wallet_id)
                        permit = await session.get(PermitModel, command["permit_id"])
                        ledger = (
                            (
                                await session.execute(
                                    select(LedgerEntryModel).where(
                                        LedgerEntryModel.wallet_id == wallet_id
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        attempts = (
                            (await session.execute(select(McpDispatchAttemptModel)))
                            .scalars()
                            .all()
                        )
                        receipts = (
                            (await session.execute(select(ReceiptModel)))
                            .scalars()
                            .all()
                        )
                        records = (
                            (
                                await session.execute(
                                    select(IdempotencyRecordModel).where(
                                        IdempotencyRecordModel.endpoint == "/mcp/invoke"
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        connection.send(
                            {
                                "balance": str(wallet.balance),
                                "spent": str(permit.spent_credits),
                                "permit_status": permit.status,
                                "debits": sum(e.action == "debit" for e in ledger),
                                "refunds": sum(e.action == "refund" for e in ledger),
                                "attempts": [a.state for a in attempts],
                                "receipts": [r.receipt_id for r in receipts],
                                "outcomes": [r.outcome for r in receipts],
                                "completed": [bool(r.response_json) for r in records],
                                "dispatches": echo.dispatches,
                            }
                        )
                elif operation == "reconcile":
                    result = await McpDispatchReconciliationService().reconcile(
                        idle_seconds=0, terminal_idle_seconds=0
                    )
                    connection.send(asdict(result))
                else:
                    raise ValueError("unknown_judge_operation")
        finally:
            await close_db()
