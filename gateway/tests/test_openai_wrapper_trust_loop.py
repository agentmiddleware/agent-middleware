"""End-to-end proof that the OpenAI wrapper drives the governed loop as one action.

The runner in ``integrations/openai-agent-middleware`` derives the trust plane's
idempotency key from the model's ``tool_call.id``. Against the real
application this must mean: a retried tool call (same id, even from a
resumed process) replays the original signed receipt and the wallet is
debited exactly once. The mock-transport tests in the wrapper pin the wire
shape; this test pins the money.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from openai_b2a import B2AClient, GovernedToolRunner, JsonFileOperationKeyStore
from openai_b2a.runner import function_name_for
from tests.test_trust_helpers import provision_agent_wallet

TOOL = "openai-wrapper-echo"


def _tool_call(call_id: str, text: str = "hi"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=function_name_for(TOOL), arguments=f'{{"text": "{text}"}}'
        ),
    )


@pytest.mark.anyio
async def test_openai_tool_call_retry_is_one_governed_action(
    clean_database, tmp_path
) -> None:
    registry = get_service_registry()
    calls: list[str] = []

    def echo(text: str = "") -> dict[str, str]:
        calls.append(text)
        return {"text": text}

    registry.register_local(
        service_id=TOOL,
        name="OpenAI Wrapper Echo",
        description="OpenAI wrapper governed-loop integration tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=2,
        unit_name="call",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as raw:
            provisioned = await provision_agent_wallet(raw)
            wallet_id = provisioned["agent_wallet_id"]
            api_key = provisioned["agent_headers"]["X-API-Key"]
            store = tmp_path / "operations.json"

            def runner() -> GovernedToolRunner:
                client = B2AClient(
                    api_key=api_key,
                    base_url="http://test",
                    transport=ASGITransport(app=app),
                )
                r = GovernedToolRunner(
                    client,
                    wallet_id=wallet_id,
                    run_id="resp_test_run_1",
                    key_store=JsonFileOperationKeyStore(store),
                    permit_budget=Decimal("10"),
                )
                r.register_tool(TOOL, description="Echo a note")
                return r

            async def balance() -> Decimal:
                resp = await raw.get(
                    f"/v1/billing/wallets/{wallet_id}",
                    headers=provisioned["agent_headers"],
                )
                return Decimal(str(resp.json()["balance"]))

            before = await balance()

            first_process = runner()
            first = await first_process.run(_tool_call("call_openai_1"))
            assert first.receipt.outcome == "success"
            assert first.receipt.credits_charged == Decimal("2")
            assert first.idempotency_key == "oai-call_openai_1"

            # Same process retry.
            again = await first_process.run(_tool_call("call_openai_1"))
            assert again.receipt.receipt_id == first.receipt.receipt_id

            # A resumed process on the same store and run_id: still the same action.
            resumed = runner()
            resumed_result = await resumed.run(_tool_call("call_openai_1"))
            assert resumed_result.receipt.receipt_id == first.receipt.receipt_id

            # A different tool call is a different action under the same permit.
            second = await resumed.run(_tool_call("call_openai_2", "again"))
            assert second.receipt.receipt_id != first.receipt.receipt_id
            assert second.receipt.permit_id == first.receipt.permit_id

            assert calls == ["hi", "again"], (
                "each distinct tool call executed exactly once"
            )
            assert before - await balance() == Decimal("4")

            ledger = await raw.get(
                f"/v1/billing/ledger/{wallet_id}", headers=provisioned["agent_headers"]
            )
            debits = [e for e in ledger.json()["entries"] if TOOL in e["description"]]
            assert len(debits) == 2

            # The signed receipt verifies server-side like any other.
            verify = await raw.post(
                "/v1/receipts/verify",
                json={"receipt_id": first.receipt.receipt_id},
                headers=provisioned["agent_headers"],
            )
            assert verify.status_code == 200, verify.text
            assert verify.json()["valid"] is True
    finally:
        registry.unregister_local(TOOL)
