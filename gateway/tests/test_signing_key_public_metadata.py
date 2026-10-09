from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _signing_key_docs() -> dict[str, str]:
    """Return the OpenAPI GET descriptions for both signing-key endpoints."""
    spec = app.openapi()
    return {
        path: spec["paths"][path]["get"]["description"]
        for path in ("/v1/signing-keys/active", "/v1/signing-keys/{key_id}")
    }


@pytest.mark.anyio
async def test_signing_key_endpoints_document_public_no_wallet_scoping():
    """The audit requires these endpoints to be documented as public metadata.

    Any authenticated key holder can read any signing key's metadata, so the
    contract must state that wallet scoping is intentionally absent and point
    readers at the unauthenticated well-known publication.
    """
    for path, description in _signing_key_docs().items():
        assert "not wallet scoped" in description, path
        assert "/.well-known/trust-keys.json" in description, path
        assert "private" in description, path


@pytest.mark.anyio
async def test_any_authenticated_key_reads_signing_key_metadata(
    client,
    clean_database,
):
    """Any authenticated credential reads the same public key metadata.

    This pins the documented contract: a wallet-scoped agent key sees the
    same active-key metadata as the bootstrap key, and the response carries
    no private material.
    """
    provisioned = await provision_agent_wallet(client)

    bootstrap_resp = await client.get(
        "/v1/signing-keys/active", headers=BOOTSTRAP_HEADERS
    )
    agent_resp = await client.get(
        "/v1/signing-keys/active", headers=provisioned["agent_headers"]
    )
    assert bootstrap_resp.status_code == 200
    assert agent_resp.status_code == 200
    assert agent_resp.json()["key_id"] == bootstrap_resp.json()["key_id"]

    body = agent_resp.json()
    assert body["public_key_b64"]
    assert "private_key" not in agent_resp.text
    assert "private_key_b64" not in agent_resp.text

    by_id_resp = await client.get(
        f"/v1/signing-keys/{body['key_id']}",
        headers=provisioned["agent_headers"],
    )
    assert by_id_resp.status_code == 200
    assert by_id_resp.json() == body
