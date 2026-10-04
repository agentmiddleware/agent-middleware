"""Exit-code coverage for scripts/agent_self_credential_proof.py.

The proof drives a self-provisioned key through seven invariants over HTTP.
These tests run it against an in-process fake of the trust plane
(``httpx.MockTransport``) that signs real Ed25519 receipts, then vary only the
scope-denial step: the run may report "ALL INVARIANTS HELD" and exit 0 only
when every invariant actually ran and held.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import sys

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts import agent_self_credential_proof as proof

WALLET = "agt-proof"
KEY_ID = "key_proof"
KID = "test-ed25519"


class FakeTrustPlane:
    """Answers the proof's requests the way a healthy local instance does."""

    def __init__(self, scope_denial: str) -> None:
        self.scope_denial = scope_denial
        self.signing_key = Ed25519PrivateKey.generate()
        self.debits: list[dict] = []
        self.signed: dict[str, str] = {}
        self.by_key: dict[str, tuple[str, str]] = {}
        self.in_flight: set[str] = set()
        self._ids = itertools.count(1)

    def _sign(self, tool: str, outcome: str, charged: str) -> dict:
        receipt_id = f"rcpt-{next(self._ids)}"
        fields = {
            "receipt_id": receipt_id,
            "wallet_id": WALLET,
            "tool": tool,
            "outcome": outcome,
            "credits_charged": charged,
        }
        self.signed[receipt_id] = json.dumps(
            fields, sort_keys=True, separators=(",", ":")
        )
        return {"receipt_id": receipt_id, "credits_charged": charged}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/dev-keys/self-provision":
            return httpx.Response(
                201,
                json={"wallet_id": WALLET, "key_id": KEY_ID, "api_key": "b2a_x"},
            )
        if path == "/v1/permits":
            return httpx.Response(201, json={"permit_id": "permit-proof"})
        if path == f"/v1/billing/ledger/{WALLET}":
            return httpx.Response(200, json={"entries": list(self.debits)})
        if path == "/.well-known/trust-keys.json":
            public = self.signing_key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kid": KID,
                            "public_key_b64": base64.b64encode(public).decode(),
                        }
                    ]
                },
            )
        if path.startswith("/v1/receipts/") and path.endswith("/portable"):
            signing_input = self.signed[path.split("/")[3]]
            signature = self.signing_key.sign(signing_input.encode())
            return httpx.Response(
                200,
                json={
                    "kid": KID,
                    "signing_input": signing_input,
                    "signature": base64.b64encode(signature).decode(),
                },
            )
        if path == f"/mcp/tools/{proof.ALLOWED_TOOL}/invoke":
            return await self._invoke(json.loads(request.content))
        if path == f"/mcp/tools/{proof.BLOCKED_TOOL}/invoke":
            return self._blocked_tool()
        return httpx.Response(404, json={"detail": "not found"})

    async def _invoke(self, body: dict) -> httpx.Response:
        context = body["mcp_context"]
        if not context.get("permit_id"):
            return httpx.Response(403, json={"detail": {"error": "permit_required"}})
        key = context["idempotency_key"]
        text = body["arguments"]["text"]
        if key in self.in_flight:
            return httpx.Response(
                409, json={"detail": {"error": "idempotency_in_progress"}}
            )
        if key in self.by_key:
            cached_text, receipt_id = self.by_key[key]
            if cached_text != text:
                return httpx.Response(
                    400, json={"detail": {"error": "idempotency_key_reused"}}
                )
            return httpx.Response(200, json={"receipt": {"receipt_id": receipt_id}})
        self.in_flight.add(key)
        await asyncio.sleep(0.01)  # concurrent same-key callers see it in flight
        self.debits.append({"action": "debit", "amount": "-2"})
        receipt = self._sign(proof.ALLOWED_TOOL, "success", "2")
        self.by_key[key] = (text, receipt["receipt_id"])
        self.in_flight.discard(key)
        return httpx.Response(200, json={"receipt": receipt, "isError": False})

    def _blocked_tool(self) -> httpx.Response:
        if self.scope_denial == "tool-not-registered":
            return httpx.Response(
                404, json={"detail": f"Tool not found: {proof.BLOCKED_TOOL}"}
            )
        if self.scope_denial == "unstructured-detail":
            return httpx.Response(403, json={"detail": "permit_tool_not_allowed"})
        detail: dict = {"error": "permit_tool_not_allowed"}
        if self.scope_denial == "signed-denial":
            detail["receipt"] = self._sign(proof.BLOCKED_TOOL, "denied", "0")
        return httpx.Response(403, json={"detail": detail})


def _run_proof(
    monkeypatch: pytest.MonkeyPatch, scope_denial: str
) -> tuple[int, FakeTrustPlane]:
    fake = FakeTrustPlane(scope_denial)
    real_async_client = httpx.AsyncClient

    def mock_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(
            *args, transport=httpx.MockTransport(fake.handle), **kwargs
        )

    monkeypatch.setattr(proof.httpx, "AsyncClient", mock_client)
    monkeypatch.setattr(proof, "failures", [])
    monkeypatch.setattr(proof, "skipped", [], raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent_self_credential_proof.py", "--base-url", "http://127.0.0.1:8000"],
    )
    return proof.main(), fake


def test_signed_scope_denial_lets_every_invariant_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, fake = _run_proof(monkeypatch, "signed-denial")

    out = capsys.readouterr().out
    assert code == 0, out
    assert "ALL INVARIANTS HELD" in out
    assert "[FAIL]" not in out
    # Three receipts verified: first invoke, the burst winner, and the denial.
    assert out.count("signature valid") == len(fake.signed) == 3


def test_unregistered_scope_denial_tool_is_not_reported_as_held(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _fake = _run_proof(monkeypatch, "tool-not-registered")

    out = capsys.readouterr().out
    assert code == 3, out
    assert "ALL INVARIANTS HELD" not in out
    assert "SKIPPED: scope-denial" in out


@pytest.mark.parametrize(
    ("scope_denial", "failed_check"),
    [
        ("unsigned-denial", "signed denial receipt issued"),
        ("unstructured-detail", "denial detail is structured"),
    ],
)
def test_scope_denial_without_signed_structured_evidence_fails(
    scope_denial: str,
    failed_check: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _fake = _run_proof(monkeypatch, scope_denial)

    out = capsys.readouterr().out
    assert code == 1, out
    assert "ALL INVARIANTS HELD" not in out
    assert f"[FAIL] {failed_check}" in out
