"""Opt-in action authority proofs: actual workers and independent partner store."""

import asyncio
import hashlib
import json
import os
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from tests import test_mcp_postgres_multiprocess as stress
from tests.test_action_migrations import FIELDS
from tests.test_action_permits import _action_request
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet
from tests.support.action_database_guard import (
    require_action_database_url,
    require_action_test_environment,
)

# Reject at collection, before the shared autouse session fixture connects.
if os.environ.get("RUN_MCP_MULTIPROCESS_TESTS") == "1":
    require_action_database_url(os.environ.get("DATABASE_URL", ""))
    require_action_test_environment(os.environ.get("ENVIRONMENT"))

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def harness(tmp_path_factory):
    url = stress._require_explicit_isolation()
    require_action_database_url(url)
    require_action_test_environment(os.environ.get("ENVIRONMENT"))
    from app.db.database import get_engine

    async with get_engine().connect() as conn:
        await stress._assert_migrated_empty_database(conn)
        for table in ("permits", "receipts"):
            cols = await conn.run_sync(lambda c: inspect(c).get_columns(table))
            assert set(FIELDS) <= {c["name"] for c in cols}, "stale 042 schema"
    async for value in stress.stress_harness.__wrapped__(tmp_path_factory):
        yield value


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def runtime(harness):
    partner = await stress._start_remote_partner(
        harness, app_module="tests.support.action_partner_app:app"
    )
    workers = []
    try:
        for index in range(2):
            workers.append(
                await stress._start_worker(
                    harness,
                    name=f"action-{index}",
                    remote_partner=partner,
                    action_mode=True,
                )
            )
        yield harness, partner, workers
    finally:
        for worker in workers:
            stress._stop_worker(worker)
        stress._stop_partner(partner)


async def issue(worker, wallets, token):
    async with httpx.AsyncClient(base_url=worker.base_url, timeout=20) as client:
        response = await client.post(
            "/v1/action-permits",
            json=_action_request(
                issuer_wallet_id=wallets["sponsor_wallet_id"],
                subject_wallet_id=wallets["agent_wallet_id"],
                subject_key_id=wallets["key_id"],
                tool_name=stress.REMOTE_STRESS_TOOL,
                arguments={"call_token": token},
                max_credits=2,
            ).model_dump(mode="json"),
            headers={**BOOTSTRAP_HEADERS, "Idempotency-Key": uuid.uuid4().hex},
        )
    assert response.status_code == 201, response.text
    return response.json()["permit_id"]


async def seed(runtime, prefix="naive:"):
    _, _, workers = runtime
    async with httpx.AsyncClient(base_url=workers[0].base_url, timeout=20) as client:
        wallets = await provision_agent_wallet(client)
    token = prefix + uuid.uuid4().hex
    return wallets, await issue(workers[0], wallets, token), token


async def invoke(worker, seeded, surface="legacy", *, token=None):
    wallets, permit_id, call_token = seeded
    key = uuid.uuid4().hex
    headers = {**wallets["agent_headers"], "Idempotency-Key": key}
    context = {
        "wallet_id": wallets["agent_wallet_id"],
        "permit_id": permit_id,
        "idempotency_key": key,
    }
    params = {
        "name": stress.REMOTE_STRESS_TOOL,
        "arguments": {"call_token": token or call_token},
        "mcpContext": context,
    }
    path = "/mcp/messages"
    body = {"jsonrpc": "2.0", "id": key, "method": "tools/call", "params": params}
    if surface == "standard":
        path = "/mcp"
        headers["Accept"] = "application/json, text/event-stream"
        params["_meta"] = {"io.agentmiddleware/permit_id": permit_id}
    elif surface == "rest":
        path = f"/mcp/tools/{stress.REMOTE_STRESS_TOOL}/invoke"
        body = {
            "name": stress.REMOTE_STRESS_TOOL,
            "arguments": params["arguments"],
            "mcp_context": context,
        }
    async with httpx.AsyncClient(base_url=worker.base_url, timeout=30) as client:
        return await client.post(path, json=body, headers=headers)


async def counters(runtime, token):
    harness, partner, _ = runtime
    async with httpx.AsyncClient(base_url=partner.base_url) as client:
        response = await client.get(
            "/__action/counters",
            params={"call_token": token},
            headers={stress.CONTROL_HEADER: harness.control_token},
        )
    assert response.status_code == 200, response.text
    return response.json()


async def rows(seeded):
    from app.db.database import get_session_factory

    wallets, permit_id, _ = seeded
    async with get_session_factory()() as session:

        async def query(sql, **params):
            return [
                dict(row)
                for row in (await session.execute(text(sql), params)).mappings()
            ]

        return {
            "owners": await query(
                "SELECT * FROM idempotency_records WHERE wallet_id=:w AND endpoint='/mcp/action/v1'",
                w=wallets["agent_wallet_id"],
            ),
            "attempts": await query(
                "SELECT * FROM mcp_dispatch_attempts WHERE permit_id=:p", p=permit_id
            ),
            "debits": await query(
                "SELECT * FROM ledger_entries WHERE wallet_id=:w AND action='debit'",
                w=wallets["agent_wallet_id"],
            ),
            "receipts": await query(
                "SELECT * FROM receipts WHERE permit_id=:p AND idempotency_record_id IS NOT NULL",
                p=permit_id,
            ),
            "permit": (
                await query("SELECT * FROM permits WHERE permit_id=:p", p=permit_id)
            )[0],
        }


def assert_chain(snapshot, *, debits=1, state="succeeded", charged=2):
    assert (
        len(snapshot["owners"])
        == len(snapshot["attempts"])
        == len(snapshot["receipts"])
        == 1
    )
    assert len(snapshot["debits"]) == debits
    owner, attempt, receipt = (
        snapshot[k][0] for k in ("owners", "attempts", "receipts")
    )
    assert attempt["state"] == state
    assert (
        owner["record_id"]
        == attempt["idempotency_record_id"]
        == receipt["idempotency_record_id"]
    )
    assert receipt["dispatch_attempt_id"] == attempt["attempt_id"]
    assert receipt["action_payload_hash"] == snapshot["permit"]["action_payload_hash"]
    assert receipt["action_contract_version"] == 1
    assert Decimal(receipt["credits_charged"]) == charged
    assert Decimal(snapshot["permit"]["spent_credits"]) == charged
    assert json.loads(snapshot["permit"]["tool_call_counts_json"]) == {
        stress.REMOTE_STRESS_TOOL: int(charged > 0)
    }
    assert (attempt["budget_released_at"] is not None) == (state == "returned_error")
    assert (attempt["debit_refunded_at"] is not None) == (debits == 1 and charged == 0)
    if debits:
        debit = snapshot["debits"][0]
        assert debit["operation_key"] == owner["record_id"]
        assert (
            owner["ledger_entry_id"]
            == receipt["ledger_entry_id"]
            == attempt["ledger_entry_id"]
            == debit["entry_id"]
        )
    assert owner["response_json"] is not None


async def test_twenty_fresh_keys_two_workers_and_restart(runtime):
    harness, partner, workers = runtime
    seeded = await seed(runtime, "key:")
    responses = await asyncio.gather(
        *(
            invoke(workers[i % 2], seeded, ("standard", "legacy", "rest")[i % 3])
            for i in range(20)
        )
    )
    for response in responses:
        assert response.status_code < 400 and (
            "error" not in response.json() or "idempotency_in_progress" in response.text
        ), response.text
    before = await rows(seeded)
    assert_chain(before)
    assert Decimal(before["permit"]["spent_credits"]) == 2
    assert json.loads(before["permit"]["tool_call_counts_json"]) == {
        stress.REMOTE_STRESS_TOOL: 1
    }
    effects = await counters(runtime, seeded[2])
    assert effects["requests"] == effects["effects"] == 1
    assert len(effects["native_keys"]) == 1
    restarted = await stress._start_worker(
        harness, name="restarted", remote_partner=partner, action_mode=True
    )
    try:
        response = await invoke(restarted, seeded, "standard")
        assert "error" not in response.json(), response.text
        assert await rows(seeded) == before
        assert await counters(runtime, seeded[2]) == effects
        # Complete arguments are immutable; this is hash difference, not semantic proof.
        response = await invoke(restarted, seeded, token=seeded[2] + "-changed")
        assert "action_payload_mismatch" in response.text
        assert await rows(seeded) == before
    finally:
        stress._stop_worker(restarted)
    print(
        "two-workers: 20 fresh keys; owners=debits=receipts=requests=effects=1; restart retained owner"
    )


@pytest.mark.parametrize(
    "fault,effects,debits,state,charged",
    [
        ("before_owner_commit", 0, 0, None, 0),
        ("after_action_prepare", 0, 0, "returned_error", 0),
        ("after_debit_commit", 0, 1, "returned_error", 0),
        ("after_dispatch_claim", 0, 1, "delivery_uncertain", 2),
        ("partner_commit_lost_ack", 1, 1, "delivery_uncertain", 2),
        ("after_receipt_commit", 1, 1, "succeeded", 2),
    ],
)
async def test_kill_boundary_matrix(runtime, fault, effects, debits, state, charged):
    harness, partner, workers = runtime
    seeded = await seed(runtime)
    worker = await stress._start_worker(
        harness,
        name=f"kill-{fault}",
        fault_point=fault,
        remote_partner=partner,
        action_mode=True,
    )
    hold = (
        harness.temp_root
        / "held-partner-responses"
        / hashlib.sha256(seeded[2].encode()).hexdigest()
    )
    if fault == "partner_commit_lost_ack":
        hold.parent.mkdir(exist_ok=True)
        hold.touch()
    pending = asyncio.create_task(invoke(worker, seeded))
    try:
        if fault == "partner_commit_lost_ack":

            async def committed():
                while (await counters(runtime, seeded[2]))["effects"] != 1:
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(committed(), 15)
        else:
            marker = await stress._wait_for_marker(worker)
            if fault == "before_owner_commit":
                assert marker["context"]["flushed"] is True
        before = await rows(seeded)
        assert (await counters(runtime, seeded[2]))["effects"] == effects
        assert len(before["debits"]) == debits
        if state is None:
            assert before["owners"] == before["attempts"] == []
        else:
            assert len(before["owners"]) == len(before["attempts"]) == 1
        stress._kill_worker(worker)
        await asyncio.gather(pending, return_exceptions=True)
        # Three rounds, each issuing simultaneous recovery to independent workers.
        for _ in range(3):
            reports = await asyncio.gather(
                *(stress._reconcile(harness, w) for w in workers)
            )
            assert all(r["dispatch_failed_attempts"] == 0 for r in reports), reports
        after = await rows(seeded)
        if state:
            assert_chain(after, debits=debits, state=state, charged=charged)
            assert before["owners"][0]["record_id"] == after["owners"][0]["record_id"]
        for i in range(6):
            await invoke(workers[i % 2], seeded, ("standard", "legacy", "rest")[i % 3])
        final = await rows(seeded)
        if state is None:
            assert_chain(final)
            assert (await counters(runtime, seeded[2]))["effects"] == 1
        else:
            assert final == after
            counts = await counters(runtime, seeded[2])
            assert counts["effects"] == counts["requests"] == effects
        print(
            f"kill={fault} effects-at-kill={effects} debits-at-kill={debits} final={state or 'safe-new-admission'}"
        )
    finally:
        stress._stop_worker(worker)
        if hold.exists():
            hold.unlink()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def direct_partner(runtime, token, key, *, bearer=None):
    _, partner, _ = runtime
    async with httpx.AsyncClient(base_url=partner.base_url, timeout=10) as client:
        return await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {bearer or partner.bearer_token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": key,
                "method": "tools/call",
                "params": {
                    "name": "partner.write",
                    "arguments": {"call_token": token},
                    "_meta": {
                        "io.agentmiddleware/invocation_id": key,
                        "io.agentmiddleware/idempotency_key": key,
                    },
                },
            },
        )


async def test_native_controls_and_credential_refusal(runtime):
    for prefix, keys, expected in (
        ("naive:", ["fresh-a", "fresh-b"], 2),
        ("key:", ["retained", "retained"], 1),
    ):
        token = prefix + uuid.uuid4().hex
        for key in keys:
            response = await direct_partner(runtime, token, key)
            assert response.status_code == 200 and not response.json().get(
                "result", {}
            ).get("isError"), response.text
        counts = await counters(runtime, token)
        assert counts["requests"] == 2 and counts["effects"] == expected
    token = "forbidden:" + uuid.uuid4().hex
    denied = await direct_partner(
        runtime, token, "old-worker", bearer="unprivileged-old-fixture-credential"
    )
    assert denied.status_code == 401
    assert (await counters(runtime, token))["effects"] == 0
    print(
        "native controls: naive fresh retries=2 effects; retained native key=1 effect; wrong credential=0 effects"
    )


async def test_separate_permits_native_business_identity_has_two_debits(runtime):
    seeded = await seed(runtime, "native:")
    _, _, workers = runtime
    first = await invoke(workers[0], seeded)
    assert "error" not in first.json(), first.text
    second_permit = await issue(workers[1], seeded[0], seeded[2])
    second = (seeded[0], second_permit, seeded[2])
    response = await invoke(workers[1], second)
    assert "error" not in response.json(), response.text
    snapshot = await rows(second)
    assert len(snapshot["owners"]) == len(snapshot["debits"]) == 2
    assert len({d["operation_key"] for d in snapshot["debits"]}) == 2
    counts = await counters(runtime, seeded[2])
    assert counts["requests"] == 2 and counts["effects"] == 1
    assert len(counts["native_keys"]) == 2
    print(
        "cross-permit boundary: owners=debits=requests=2; authoritative native business identity effects=1"
    )


async def test_partner_counts_beyond_display_cap(runtime):
    _, partner, _ = runtime
    token = "uncapped:" + uuid.uuid4().hex
    async with httpx.AsyncClient(base_url=partner.base_url, timeout=20) as client:
        for i in range(1001):
            response = await client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {partner.bearer_token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "tools/call",
                    "params": {
                        "name": "partner.write",
                        "arguments": {"call_token": token},
                        "_meta": {
                            "io.agentmiddleware/invocation_id": str(i),
                            "io.agentmiddleware/idempotency_key": str(i),
                        },
                    },
                },
            )
            assert response.status_code == 200 and not response.json()["result"].get(
                "isError"
            ), response.text
    counts = await counters(runtime, token)
    assert counts["requests"] == counts["effects"] == 1001
    assert len(counts["native_keys"]) == 1001
    print("independent partner control: requests=effects=1001; counters uncapped")
