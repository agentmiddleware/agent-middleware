"""Real loopback HTTP reproduction against the quickstart server; no production or OpenAI calls.

Supplied by the 2026-09 independent review against c6b0534: a malformed metadata replay key
must be refused before any debit, while a valid key still replays its original receipt.
"""

import copy
import json
from decimal import Decimal
import httpx

# pytest registers an imported fixture under its binding name, so the import
# must keep the fixture's own name; the parameter below then re-uses it.
from tests.test_quickstart_path import quickstart_server  # noqa: F401


def test_live_invalid_key_rejected_before_debit(quickstart_server):  # noqa: F811
    with httpx.Client(
        base_url=quickstart_server, trust_env=False, timeout=20
    ) as client:
        client.get("/mcp/tools.json").raise_for_status()
        provision = client.post(
            "/v1/dev-keys/self-provision", json={"agent_id": "review-key-probe"}
        )
        provision.raise_for_status()
        p = provision.json()
        headers = {
            "X-API-Key": p["api_key"],
            "Accept": "application/json, text/event-stream",
        }
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "partner.notes.write",
                "arguments": {"text": "review-invalid-key-note"},
                "_meta": {"io.agentmiddleware/idempotency_key": 123},
            },
        }
        control = copy.deepcopy(body)
        control["params"]["_meta"]["io.agentmiddleware/idempotency_key"] = (
            "review-valid-key"
        )
        a = client.post("/mcp", json=control, headers=headers).json()
        b = client.post("/mcp", json=control, headers=headers).json()
        assert "result" in a, a
        assert (
            a["result"]["receipt"]["receipt_id"] == b["result"]["receipt"]["receipt_id"]
        )

        def balance():
            return Decimal(
                str(
                    client.get(
                        f"/v1/billing/wallets/{p['wallet_id']}", headers=headers
                    ).json()["balance"]
                )
            )

        before = balance()
        replies = [
            client.post("/mcp", json=body, headers=headers).json() for _ in range(2)
        ]
        after = balance()
        receipts = [r.get("result", {}).get("receipt", {}) for r in replies]
        observation = {
            "valid_key_replays": True,
            "balance_before": str(before),
            "balance_after": str(after),
            "additional_debit": str(before - after),
            "receipt_ids": [r.get("receipt_id") for r in receipts],
            "charges": [r.get("credits_charged") for r in receipts],
        }
        print(json.dumps(observation, sort_keys=True))
        assert before == after, f"Malformed key caused extra debits: {observation}"
