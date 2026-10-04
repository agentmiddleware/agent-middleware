"""Synthetic local contract checks; no database or network I/O."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx
import pytest

from app.services.permits import PermitService
from app.services.signing_keys import canonical_json
from b2a_sdk.client import AgentMiddlewareClient
from b2a_sdk.edge_client import LocalPermitValidator, GovernedEdgeSession


def signed_permit(action):
    now = datetime.now(timezone.utc)
    fields = dict(
        action_contract_version=None,
        action_payload_hash=None,
        action_schema_id=None,
        action_schema_version=None,
        action_public_tool_id=None,
        action_upstream_binding_hash=None,
    )
    if action:
        fields.update(
            action_contract_version=1,
            action_payload_hash="a" * 64,
            action_schema_id="partner.write",
            action_schema_version="1",
            action_public_tool_id="partner.write",
            action_upstream_binding_hash="b" * 64,
        )
    model = SimpleNamespace(
        permit_id="qa-permit",
        issuer_wallet_id="qa-wallet",
        subject_wallet_id="qa-wallet",
        subject_key_id=None,
        scopes_json=json.dumps(["tool:partner.write:invoke", "billing:charge"]),
        allowed_tools_json=json.dumps(["partner.write"]),
        max_credits=Decimal("1"),
        expires_at=now + timedelta(hours=1),
        issued_at=now,
        nonce="qa-nonce",
        requires_human_approval=False,
        max_calls_per_tool_json="{}",
        aggregate_value_cap=None,
        forbidden_fields_json="[]",
        recipient_domain=None,
        allow_identical_repeats=False,
        repeat_window_seconds=None,
        key_id="qa-signer",
        **fields,
    )
    payload = PermitService._verification_payload(model)
    encoded = canonical_json(payload).encode("utf-8")
    private = Ed25519PrivateKey.generate()
    signature = private.sign(encoded)
    # Independent crypto confirms server-reconstructed bytes are signed correctly.
    private.public_key().verify(signature, encoded)
    permit = json.loads(canonical_json(PermitService._unsigned_payload(model)))
    permit.update(
        signature=base64.b64encode(signature).decode(),
        key_id="qa-signer",
        spent_credits="0",
        **fields,
    )
    keys = {"qa-signer": private.public_key().public_bytes_raw()}
    return permit, keys


@pytest.mark.parametrize("action", [False, True])
def test_sdk_accepts_server_signed_permits(action):
    permit, keys = signed_permit(action)
    assert LocalPermitValidator(permit, keys).verify_permit() is True


@pytest.mark.asyncio
async def test_governed_session_opens_server_signed_action_permit():
    permit, keys = signed_permit(True)
    doc = {
        "keys": [
            {
                "kid": "qa-signer",
                "alg": "Ed25519",
                "status": "active",
                "public_key_b64": base64.b64encode(keys["qa-signer"]).decode(),
            }
        ]
    }
    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.url.path == "/v1/permits/qa-permit"
        return httpx.Response(200, json=permit)

    async with AgentMiddlewareClient(
        "synthetic-qa-only",
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        await GovernedEdgeSession.open(
            client,
            permit_id="qa-permit",
            wallet_id="qa-wallet",
            trust_keys_document=doc,
        )
    assert seen == ["/v1/permits/qa-permit"]
