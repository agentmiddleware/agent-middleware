"""
Golden-path smoke test for the production-beta product loop.

This mirrors docs/golden-path.md: bootstrap provisioning, wallet-scoped runtime
key, ownership denial, dry-run cost estimation, discovery, and inspection.
"""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_wallet_scoped_agent_golden_path(
    client,
    clean_database,
    enforce_naive_utc_datetime_columns,
):
    bootstrap_headers = {"X-API-Key": "test-key"}

    # Agent-facing discovery surfaces should be reachable.
    for path in ("/.well-known/agent.json", "/llm.txt"):
        resp = await client.get(path, headers=bootstrap_headers)
        assert resp.status_code == 200

    sponsor_resp = await client.post(
        "/v1/billing/wallets/sponsor",
        json={
            "sponsor_name": "Golden Path Sponsor",
            "email": "golden@example.com",
            "initial_credits": 10000,
            "require_kyc": False,
        },
        headers=bootstrap_headers,
    )
    assert sponsor_resp.status_code == 201
    sponsor_wallet_id = sponsor_resp.json()["wallet_id"]

    agent_resp = await client.post(
        "/v1/billing/wallets/agent",
        json={
            "sponsor_wallet_id": sponsor_wallet_id,
            "agent_id": "golden-agent-001",
            "budget_credits": 1000,
            "daily_limit": 250,
        },
        headers=bootstrap_headers,
    )
    assert agent_resp.status_code == 201
    agent_wallet_id = agent_resp.json()["wallet_id"]

    key_resp = await client.post(
        "/v1/api-keys",
        json={
            "wallet_id": agent_wallet_id,
            "key_name": "golden-agent-runtime",
            "expires_in_days": 30,
        },
        headers=bootstrap_headers,
    )
    assert key_resp.status_code == 201
    agent_api_key = key_resp.json()["api_key"]
    agent_key_id = key_resp.json()["key_id"]
    agent_headers = {"X-API-Key": agent_api_key}

    own_wallet_resp = await client.get(
        f"/v1/billing/wallets/{agent_wallet_id}",
        headers=agent_headers,
    )
    assert own_wallet_resp.status_code == 200
    assert own_wallet_resp.json()["wallet_id"] == agent_wallet_id

    sponsor_read_resp = await client.get(
        f"/v1/billing/wallets/{sponsor_wallet_id}",
        headers=agent_headers,
    )
    assert sponsor_read_resp.status_code == 403

    dry_run_resp = await client.post(
        "/v1/billing/dry-run/session",
        json={"wallet_id": agent_wallet_id},
        headers=agent_headers,
    )
    assert dry_run_resp.status_code == 201
    dry_run_session_id = dry_run_resp.json()["session_id"]

    simulate_resp = await client.post(
        "/v1/billing/dry-run/charge",
        json={
            "wallet_id": agent_wallet_id,
            "service": "telemetry_pm",
            "units": 1,
            "description": "Estimate anomaly review cost",
            "dry_run_session_id": dry_run_session_id,
        },
        headers=agent_headers,
    )
    assert simulate_resp.status_code == 200
    simulation = simulate_resp.json()
    assert simulation["dry_run"] is True
    assert simulation["wallet_id"] == agent_wallet_id
    assert simulation["would_succeed"] is True

    registry = get_service_registry()

    def golden_path_echo(message: str = "ok") -> dict:
        return {"message": message}

    registry.register_local(
        service_id="golden-path-echo",
        name="Golden Path Echo",
        description="Echo tool for golden path MCP invocation",
        category=ServiceCategory.AGENT_COMMS,
        func=golden_path_echo,
        credits_per_unit=2.0,
        unit_name="call",
    )
    registry.register_local(
        service_id="golden-path-denied",
        name="Golden Path Denied",
        description="Out-of-scope tool for golden path denial proof",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda: {"message": "should not run"},
        credits_per_unit=2.0,
        unit_name="call",
    )

    try:
        mcp_resp = await client.get("/mcp/tools.json", headers=agent_headers)
        assert mcp_resp.status_code == 200
        mcp_payload = mcp_resp.json()
        assert "tools" in mcp_payload
        assert any(tool["name"] == "golden-path-echo" for tool in mcp_payload["tools"])

        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": agent_wallet_id,
                "subject_wallet_id": agent_wallet_id,
                "subject_key_id": agent_key_id,
                "allowed_tools": ["golden-path-echo"],
                "scopes": ["tool:golden-path-echo:invoke", "billing:charge"],
                "max_credits": 50,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
            headers={**bootstrap_headers, "Idempotency-Key": "golden-permit-1"},
        )
        assert permit_resp.status_code == 201
        permit = permit_resp.json()

        invoke_body = {
            "jsonrpc": "2.0",
            "id": "golden-call-1",
            "method": "tools/call",
            "params": {
                "name": "golden-path-echo",
                "arguments": {"message": "hello"},
                "mcpContext": {
                    "wallet_id": agent_wallet_id,
                    "request_path": "POST /mcp/messages",
                    "permit_id": permit["permit_id"],
                    "idempotency_key": "golden-invoke-1",
                },
            },
        }
        invoke_resp = await client.post(
            "/mcp/messages",
            json=invoke_body,
            headers=agent_headers,
        )
        assert invoke_resp.status_code == 200
        invoke_payload = invoke_resp.json()
        assert "result" in invoke_payload
        assert invoke_payload["result"]["isError"] is False
        receipt = invoke_payload["result"]["receipt"]
        assert receipt["permit_id"] == permit["permit_id"]
        assert receipt["outcome"] == "success"
        assert receipt["ledger_entry_id"]

        verify_receipt_resp = await client.post(
            "/v1/receipts/verify",
            json={"receipt_id": receipt["receipt_id"]},
            headers=agent_headers,
        )
        assert verify_receipt_resp.status_code == 200
        assert verify_receipt_resp.json()["valid"] is True

        replay_resp = await client.post(
            "/mcp/messages",
            json=invoke_body,
            headers=agent_headers,
        )
        assert replay_resp.status_code == 200
        assert (
            replay_resp.json()["result"]["receipt"]["receipt_id"]
            == receipt["receipt_id"]
        )

        denied_resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": "golden-denial-1",
                "method": "tools/call",
                "params": {
                    "name": "golden-path-denied",
                    "arguments": {},
                    "mcpContext": {
                        "wallet_id": agent_wallet_id,
                        "permit_id": permit["permit_id"],
                        "idempotency_key": "golden-denial-1",
                    },
                },
            },
            headers=agent_headers,
        )
        assert denied_resp.status_code == 200
        denied_payload = denied_resp.json()
        assert denied_payload["error"]["message"] == "permit_tool_not_allowed"
        denied_receipt = denied_payload["error"]["data"]["receipt"]
        assert denied_receipt["outcome"] == "denied"
        assert denied_receipt["credits_charged"] == "0"

        audit_chain_resp = await client.post(
            "/v1/audit/verify-chain",
            json={"wallet_id": agent_wallet_id},
            headers=agent_headers,
        )
        assert audit_chain_resp.status_code == 200
        assert audit_chain_resp.json()["valid"] is True
    finally:
        registry.unregister_local("golden-path-echo")
        registry.unregister_local("golden-path-denied")

    ledger_resp = await client.get(
        f"/v1/billing/ledger/{agent_wallet_id}",
        headers=agent_headers,
    )
    assert ledger_resp.status_code == 200
    assert ledger_resp.json()["wallet_id"] == agent_wallet_id
    ledger_entries = ledger_resp.json()["entries"]
    assert any(
        entry["service_category"] == "agent_comms"
        and "golden-path-echo" in entry.get("description", "")
        for entry in ledger_entries
    )
    golden_debits = [
        entry
        for entry in ledger_entries
        if entry["service_category"] == "agent_comms"
        and "golden-path-echo" in entry.get("description", "")
    ]
    assert len(golden_debits) == 1

    audit_resp = await client.get(
        f"/v1/audit/events?wallet_id={agent_wallet_id}&tool=golden-path-echo",
        headers=bootstrap_headers,
    )
    assert audit_resp.status_code == 200
    audit_events = audit_resp.json()["events"]
    assert len(audit_events) == 1
    assert audit_events[0]["policy_decision_id"].startswith("pol-")
    audit_metadata = audit_events[0]["metadata"]
    assert audit_metadata["transport"] == "jsonrpc"
    assert audit_metadata["permit_id"] == permit["permit_id"]
    assert audit_metadata["idempotency_key"] == "golden-invoke-1"
    assert audit_metadata["ledger_entry_id"] == golden_debits[0]["entry_id"]
    assert audit_metadata["request_hash"]

    velocity_resp = await client.get(
        f"/v1/billing/wallets/{agent_wallet_id}/velocity",
        headers=agent_headers,
    )
    assert velocity_resp.status_code == 200
