"""Receipts must be checkable by someone who does not trust this plane.

Every test here verifies through :mod:`b2a_sdk.receipt_verifier`, which holds
no reference to the app, the database, or the signing service. If a test in
this file passes, an outside party holding the same two JSON documents can
reach the same conclusion.
"""

from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db.database import get_session_factory
from app.main import app
from app.schemas.billing import ServiceCategory
from app.services.service_registry import get_service_registry
from b2a_sdk.receipt_verifier import (
    VerificationStatus,
    key_set_from_document,
    verify_bundle,
)
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


_PUBLIC_VERIFIER_STATE_TABLES = (
    "wallets",
    "permits",
    "ledger_entries",
    "receipts",
    "mcp_dispatch_attempts",
    "signing_keys",
    "control_plane_audit_events",
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _invoke_governed_tool(client, provisioned, tool_name="portable-echo"):
    """Run one real governed invoke and return its receipt.

    Idempotency keys are derived from the tool name so a test can invoke
    twice without the second call colliding with the first.
    """
    registry = get_service_registry()
    registry.register_local(
        service_id=tool_name,
        name="Portable Echo",
        description="Receipt portability test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=lambda message="ok": {"message": message},
        credits_per_unit=2.0,
        unit_name="call",
    )
    try:
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            idem_key=f"permit-create-{tool_name}",
        )
        resp = await client.post(
            "/mcp/messages",
            json={
                "jsonrpc": "2.0",
                "id": f"portable-call-{tool_name}",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": {"message": "hi"},
                    "mcpContext": {
                        "wallet_id": provisioned["agent_wallet_id"],
                        "permit_id": permit["permit_id"],
                        "idempotency_key": f"portable-invoke-{tool_name}",
                    },
                },
            },
            headers=provisioned["agent_headers"],
        )
        assert resp.status_code == 200, resp.text
        return permit, resp.json()["result"]["receipt"]
    finally:
        registry.unregister_local(tool_name)


async def _portable_bundle_and_keys(client, provisioned, receipt_id):
    """Fetch the bundle as the receipt's owner, and the keys as a stranger."""
    bundle_resp = await client.get(
        f"/v1/receipts/{receipt_id}/portable",
        headers=provisioned["agent_headers"],
    )
    assert bundle_resp.status_code == 200, bundle_resp.text

    # Deliberately unauthenticated: this is the whole point of the endpoint.
    keys_resp = await client.get("/.well-known/trust-keys.json")
    assert keys_resp.status_code == 200, keys_resp.text

    return bundle_resp.json(), keys_resp.json()


async def _public_verifier_state_counts() -> dict[str, int]:
    factory = get_session_factory()
    async with factory() as session:
        return {
            table: int(
                (await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar()
                or 0
            )
            for table in _PUBLIC_VERIFIER_STATE_TABLES
        }


@pytest.mark.anyio
async def test_receipt_verifies_offline_without_credentials(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    permit, receipt = await _invoke_governed_tool(client, provisioned)
    bundle, key_document = await _portable_bundle_and_keys(
        client, provisioned, receipt["receipt_id"]
    )

    result = verify_bundle(bundle, key_set_from_document(key_document))

    assert result.ok, result.reason
    assert result.status is VerificationStatus.VERIFIED
    assert result.receipt_id == receipt["receipt_id"]

    # The claims an outside party can rely on are the ones inside the
    # signature — the four the accountability argument rests on, plus the
    # idempotency key that makes a retry checkable.
    claims = result.claims
    assert claims["permit_id"] == permit["permit_id"]
    assert claims["tool"] == "portable-echo"
    assert claims["outcome"] == "success"
    assert claims["idempotency_record_id"] == receipt["idempotency_record_id"]

    # Credits are signed in normalized decimal form ("2"), while the API
    # response renders scale ("2.00000000"). Same number, different text — a
    # verifier must compare numerically or it will report false mismatches.
    assert Decimal(claims["credits_charged"]) == Decimal(
        str(receipt["credits_charged"])
    )
    assert Decimal(claims["credits_authorized"]) == Decimal(
        str(receipt["credits_authorized"])
    )


@pytest.mark.anyio
async def test_trust_keys_endpoint_needs_no_credential(client, clean_database):
    """A verifier with no account here must still be able to get the key."""
    from app.services.signing_keys import get_signing_key_service

    await get_signing_key_service().ensure_active_key()
    resp = await client.get("/.well-known/trust-keys.json")

    assert resp.status_code == 200
    document = resp.json()
    assert document["alg"] == "Ed25519"
    assert document["canonicalization"] == "awi-canonical-json/1"
    assert document["keys"], "no signing key published"

    entry = document["keys"][0]
    assert entry["status"] != "disabled"
    # Raw form and JWK mirror must describe the same key material.
    raw = base64.b64decode(entry["public_key_b64"], validate=True)
    assert len(raw) == 32
    jwk_x = entry["jwk"]["x"]
    padded = jwk_x + "=" * (-len(jwk_x) % 4)
    assert base64.urlsafe_b64decode(padded) == raw


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["/.well-known/trust-keys.json", "/.well-known/jwks.json"],
)
async def test_public_key_discovery_does_not_create_signing_authority(
    client, clean_database, path
):
    """An anonymous verifier must never provision the plane's signing key."""
    before = await _public_verifier_state_counts()

    resp = await client.get(path)

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"] == "trust_keys_unavailable"
    assert await _public_verifier_state_counts() == before


@pytest.mark.anyio
async def test_parallel_public_key_discovery_is_state_isolated(client, clean_database):
    """Parallel anonymous reads must preserve all trust-critical state."""
    before = await _public_verifier_state_counts()
    paths = ["/.well-known/trust-keys.json", "/.well-known/jwks.json"] * 8

    responses = await asyncio.gather(*(client.get(path) for path in paths))

    assert all(response.status_code == 503 for response in responses)
    assert await _public_verifier_state_counts() == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["/.well-known/trust-keys.json", "/.well-known/jwks.json"],
)
async def test_public_key_discovery_does_not_reactivate_retired_key(
    client, clean_database, path
):
    from app.services.signing_keys import get_signing_key_service

    service = get_signing_key_service()
    key = await service.ensure_active_key()
    await service.retire_key_metadata(key.key_id)

    resp = await client.get(path)

    assert resp.status_code == 200, resp.text
    persisted = await service.get_public_key(key.key_id)
    assert persisted is not None
    assert persisted.status == "retired"
    assert persisted.retired_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    ["store_unreachable", "no_publishable_keys"],
    ids=["unreachable_store", "empty_key_set"],
)
async def test_unusable_key_set_is_refused_rather_than_served_empty(
    client, clean_database, monkeypatch, failure
):
    """Never answer 200 with a key set a verifier cannot use.

    An empty `keys` list is not neutral: a verifier looking up the receipt's
    kid finds nothing and can report the receipt as unverifiable, which reads
    as a forgery. Both an unreachable store and a reachable-but-empty one are
    "cannot tell", so both must answer 503.
    """
    from app.services import signing_keys as signing_keys_module

    service = signing_keys_module.get_signing_key_service()

    async def unreachable():
        raise RuntimeError("DATABASE_URL not configured")

    async def empty():
        return []

    monkeypatch.setattr(
        service,
        "list_public_keys",
        unreachable if failure == "store_unreachable" else empty,
    )

    resp = await client.get("/.well-known/trust-keys.json")

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"] == "trust_keys_unavailable"


@pytest.mark.anyio
async def test_jwks_endpoint_needs_no_credential(client, clean_database):
    """The standard JWKS view must be fetchable by a party with no account.

    Verifiers that auto-discover keys expect a bare RFC 7517 JWK Set at
    /.well-known/jwks.json; the trust plane's docs and marketing site both
    point there, so it must resolve rather than 404.
    """
    from app.services.signing_keys import get_signing_key_service

    await get_signing_key_service().ensure_active_key()
    resp = await client.get("/.well-known/jwks.json")

    assert resp.status_code == 200, resp.text
    document = resp.json()
    # RFC 7517 shape: a bare {"keys": [...]} set, no envelope metadata.
    assert set(document) == {"keys"}
    assert document["keys"], "no signing key published"

    for jwk in document["keys"]:
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert jwk["alg"] == "EdDSA"
        assert jwk["use"] == "sig"
        # `x` is the raw 32-byte Ed25519 public key as unpadded base64url.
        x = jwk["x"]
        padded = x + "=" * (-len(x) % 4)
        assert len(base64.urlsafe_b64decode(padded)) == 32


@pytest.mark.anyio
async def test_jwks_and_trust_keys_publish_the_same_key_material(
    client, clean_database
):
    """jwks.json and trust-keys.json are two envelopes over one key set.

    If they could drift, a verifier that trusted one would reach a different
    conclusion than a verifier that trusted the other for the same receipt.
    """
    from app.services.signing_keys import get_signing_key_service

    await get_signing_key_service().ensure_active_key()
    jwks_resp = await client.get("/.well-known/jwks.json")
    trust_resp = await client.get("/.well-known/trust-keys.json")
    assert jwks_resp.status_code == 200, jwks_resp.text
    assert trust_resp.status_code == 200, trust_resp.text

    jwks_by_kid = {k["kid"]: k for k in jwks_resp.json()["keys"]}
    trust_by_kid = {
        k["jwk"]["kid"]: k["jwk"] for k in trust_resp.json()["keys"] if "jwk" in k
    }

    assert trust_by_kid, "trust-keys published no JWK mirror"
    assert set(jwks_by_kid) == set(trust_by_kid)
    for kid, jwk in trust_by_kid.items():
        assert jwks_by_kid[kid] == jwk


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    ["store_unreachable", "no_publishable_keys"],
    ids=["unreachable_store", "empty_key_set"],
)
async def test_jwks_refuses_rather_than_serving_an_empty_set(
    client, clean_database, monkeypatch, failure
):
    """An unusable JWKS answers 503, exactly like trust-keys.json.

    A 200 with an empty `keys` list would let a verifier looking up a
    receipt's kid conclude the receipt is a forgery, so "no usable key" must
    stay in the retryable "cannot tell" bucket.
    """
    from app.services import signing_keys as signing_keys_module

    service = signing_keys_module.get_signing_key_service()

    async def unreachable():
        raise RuntimeError("DATABASE_URL not configured")

    async def empty():
        return []

    monkeypatch.setattr(
        service,
        "list_public_keys",
        unreachable if failure == "store_unreachable" else empty,
    )

    resp = await client.get("/.well-known/jwks.json")

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"] == "trust_keys_unavailable"


@pytest.mark.anyio
async def test_tampering_with_a_signed_field_fails_verification(client, clean_database):
    provisioned = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, provisioned)
    bundle, key_document = await _portable_bundle_and_keys(
        client, provisioned, receipt["receipt_id"]
    )
    key_set = key_set_from_document(key_document)

    payload = json.loads(bundle["signing_input"])
    assert payload["credits_charged"] != "0.01"
    payload["credits_charged"] = "0.01"
    forged = dict(bundle)
    forged["signing_input"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    result = verify_bundle(forged, key_set)

    assert not result.ok
    assert result.is_tampered
    assert result.status is VerificationStatus.INVALID


@pytest.mark.anyio
async def test_signature_from_another_receipt_does_not_transfer(client, clean_database):
    """A valid signature must not validate a different receipt's bytes."""
    provisioned = await provision_agent_wallet(client)
    _, first = await _invoke_governed_tool(client, provisioned, "portable-echo")
    _, second = await _invoke_governed_tool(client, provisioned, "portable-echo-2")

    first_bundle, key_document = await _portable_bundle_and_keys(
        client, provisioned, first["receipt_id"]
    )
    second_bundle, _ = await _portable_bundle_and_keys(
        client, provisioned, second["receipt_id"]
    )
    assert first_bundle["signature"] != second_bundle["signature"]

    spliced = dict(first_bundle)
    spliced["signature"] = second_bundle["signature"]

    result = verify_bundle(spliced, key_set_from_document(key_document))

    assert result.status is VerificationStatus.INVALID


@pytest.mark.anyio
async def test_envelope_cannot_misrepresent_the_signed_receipt(client, clean_database):
    """Unauthenticated envelope fields must never override signed ones."""
    provisioned = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, provisioned)
    bundle, key_document = await _portable_bundle_and_keys(
        client, provisioned, receipt["receipt_id"]
    )

    relabelled = dict(bundle)
    relabelled["receipt_id"] = "rcpt-someone-elses"

    result = verify_bundle(relabelled, key_set_from_document(key_document))

    assert result.status is VerificationStatus.MISMATCH
    assert "receipt_id" in (result.reason or "")


@pytest.mark.anyio
async def test_unknown_key_is_not_reported_as_tampering(client, clean_database):
    """ "I don't have the key" and "this is forged" must not look alike."""
    provisioned = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, provisioned)
    bundle, _ = await _portable_bundle_and_keys(
        client, provisioned, receipt["receipt_id"]
    )

    result = verify_bundle(bundle, {})

    assert not result.ok
    assert result.status is VerificationStatus.UNKNOWN_KEY
    # An operator paging on `is_tampered` must not be woken by a stale cache.
    assert not result.is_tampered


@pytest.mark.anyio
async def test_issuer_mismatch_is_rejected_when_expected_issuer_given(
    client, clean_database
):
    provisioned = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, provisioned)
    bundle, key_document = await _portable_bundle_and_keys(
        client, provisioned, receipt["receipt_id"]
    )
    key_set = key_set_from_document(key_document)

    assert verify_bundle(bundle, key_set).ok
    result = verify_bundle(
        bundle, key_set, expected_issuer="https://not-this-plane.example"
    )

    assert result.status is VerificationStatus.MISMATCH


@pytest.mark.anyio
async def test_portable_export_refuses_a_receipt_that_does_not_verify(
    client, clean_database
):
    """Never hand out a bundle that would fail in the holder's verifier."""
    provisioned = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, provisioned)

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("UPDATE receipts SET tool = :tool WHERE receipt_id = :rid"),
            {"tool": "some-other-tool", "rid": receipt["receipt_id"]},
        )
        await session.commit()

    resp = await client.get(
        f"/v1/receipts/{receipt['receipt_id']}/portable",
        headers=provisioned["agent_headers"],
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "receipt_signature_invalid"


@pytest.mark.anyio
async def test_portable_export_still_requires_authorization(client, clean_database):
    """Verifying is open; reading someone else's receipt is not."""
    provisioned = await provision_agent_wallet(client)
    _, receipt = await _invoke_governed_tool(client, provisioned)

    resp = await client.get(f"/v1/receipts/{receipt['receipt_id']}/portable")

    assert resp.status_code in (401, 403), resp.text


# --- Crafted-signature cases -------------------------------------------------
# These sign payloads with a throwaway key to reach checks that a well-behaved
# plane never produces: a valid signature over an internally inconsistent
# payload. A verifier that trusted the signature alone would accept them.


def _local_signer():
    """Return (kid, key_set, sign) for a throwaway Ed25519 key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_key = Ed25519PrivateKey.generate()
    raw_public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(signing_input: str) -> str:
        return base64.b64encode(
            private_key.sign(signing_input.encode("utf-8"))
        ).decode()

    return "local-test-key", {"local-test-key": raw_public}, sign


def _signed_bundle(payload, *, kid, sign, **overrides):
    signing_input = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    bundle = {
        "schema_version": "1.0",
        "receipt_id": payload.get("receipt_id"),
        "issuer": "https://api.example.com",
        "alg": "Ed25519",
        "kid": kid,
        "canonicalization": "awi-canonical-json/1",
        "signing_input": signing_input,
        "signature": sign(signing_input),
        "keys_url": "/.well-known/trust-keys.json",
    }
    bundle.update(overrides)
    return bundle


def _hashed_payload(payload):
    from app.services.signing_keys import sha256_hex

    body = {k: v for k, v in payload.items() if k != "payload_hash"}
    return {**body, "payload_hash": sha256_hex(body)}


def test_validly_signed_payload_with_a_wrong_payload_hash_is_rejected():
    kid, key_set, sign = _local_signer()
    payload = _hashed_payload(
        {"receipt_id": "rcpt-x", "kid": kid, "tool": "search", "outcome": "success"}
    )
    payload["payload_hash"] = "f" * 64

    result = verify_bundle(_signed_bundle(payload, kid=kid, sign=sign), key_set)

    assert result.status is VerificationStatus.MISMATCH
    assert "payload_hash" in (result.reason or "")


def test_signature_under_a_key_the_payload_does_not_name_is_rejected():
    """The signed payload must agree about which key signed it."""
    kid, key_set, sign = _local_signer()
    payload = _hashed_payload(
        {"receipt_id": "rcpt-x", "kid": "a-different-key", "tool": "search"}
    )

    result = verify_bundle(_signed_bundle(payload, kid=kid, sign=sign), key_set)

    assert result.status is VerificationStatus.MISMATCH
    assert "kid" in (result.reason or "")


def test_signed_payload_without_a_receipt_id_is_malformed():
    kid, key_set, sign = _local_signer()
    payload = _hashed_payload({"kid": kid, "tool": "search"})

    bundle = _signed_bundle(payload, kid=kid, sign=sign)
    bundle.pop("receipt_id", None)
    result = verify_bundle(bundle, key_set)

    assert result.status is VerificationStatus.MALFORMED


def test_a_correctly_signed_payload_verifies():
    """Guards the crafted-bundle helper itself against silent breakage."""
    kid, key_set, sign = _local_signer()
    payload = _hashed_payload({"receipt_id": "rcpt-x", "kid": kid, "tool": "search"})

    result = verify_bundle(_signed_bundle(payload, kid=kid, sign=sign), key_set)

    assert result.ok, result.reason
    assert result.claims["tool"] == "search"


def test_retired_keys_verify_while_disabled_keys_are_unavailable():
    kid, key_set, sign = _local_signer()
    payload = _hashed_payload({"receipt_id": "rcpt-x", "kid": kid, "tool": "search"})
    bundle = _signed_bundle(payload, kid=kid, sign=sign)
    encoded_key = base64.b64encode(key_set[kid]).decode()

    retired = key_set_from_document(
        {
            "keys": [
                {
                    "kid": kid,
                    "alg": "Ed25519",
                    "status": "retired",
                    "public_key_b64": encoded_key,
                }
            ]
        }
    )
    disabled = key_set_from_document(
        {
            "keys": [
                {
                    "kid": kid,
                    "alg": "Ed25519",
                    "status": "disabled",
                    "public_key_b64": encoded_key,
                }
            ]
        }
    )

    assert verify_bundle(bundle, retired).status is VerificationStatus.VERIFIED
    assert verify_bundle(bundle, disabled).status is VerificationStatus.UNKNOWN_KEY


# --- CLI ---------------------------------------------------------------------


def _write_cli_inputs(tmp_path, bundle, key_set):
    bundle_path = tmp_path / "bundle.json"
    keys_path = tmp_path / "trust-keys.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    keys_path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "kid": kid,
                        "alg": "Ed25519",
                        "status": "active",
                        "public_key_b64": base64.b64encode(raw).decode(),
                    }
                    for kid, raw in key_set.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return str(bundle_path), str(keys_path)


def test_cli_exit_codes_separate_forged_from_undetermined(tmp_path, capsys):
    from b2a_sdk.verify_cli import main

    kid, key_set, sign = _local_signer()
    payload = _hashed_payload({"receipt_id": "rcpt-x", "kid": kid, "tool": "search"})
    bundle = _signed_bundle(payload, kid=kid, sign=sign)
    bundle_path, keys_path = _write_cli_inputs(tmp_path, bundle, key_set)

    assert main(["--bundle", bundle_path, "--keys", keys_path]) == 0
    assert "VERIFIED" in capsys.readouterr().out

    # A tampered bundle is a verdict on the receipt: exit 1.
    forged = dict(bundle)
    forged["signing_input"] = bundle["signing_input"].replace("search", "delete")
    forged_path, _ = _write_cli_inputs(tmp_path, forged, key_set)
    assert main(["--bundle", forged_path, "--keys", keys_path]) == 1

    # No key for this kid is a verdict on the verifier: exit 2, so an operator
    # can tell a stale key cache from an actual forgery.
    empty_keys = tmp_path / "empty-keys.json"
    empty_keys.write_text(json.dumps({"keys": []}), encoding="utf-8")
    assert main(["--bundle", bundle_path, "--keys", str(empty_keys)]) == 2


def test_cli_emits_machine_readable_output(tmp_path, capsys):
    from b2a_sdk.verify_cli import main

    kid, key_set, sign = _local_signer()
    payload = _hashed_payload({"receipt_id": "rcpt-x", "kid": kid, "tool": "search"})
    bundle_path, keys_path = _write_cli_inputs(
        tmp_path, _signed_bundle(payload, kid=kid, sign=sign), key_set
    )

    assert main(["--bundle", bundle_path, "--keys", keys_path, "--json"]) == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ok"] is True
    assert emitted["status"] == "verified"
    assert emitted["claims"]["tool"] == "search"


@pytest.mark.parametrize(
    "overrides,label",
    [
        ({"alg": "RS256"}, "algorithm"),
        ({"canonicalization": "awi-canonical-json/2"}, "canonicalization"),
    ],
)
def test_envelope_cannot_downgrade_a_genuine_receipt_to_unsupported(overrides, label):
    """An edited envelope must not let an attacker choose the verdict.

    ``alg`` and ``canonicalization`` were read from the unauthenticated
    envelope and short-circuited to UNSUPPORTED *before* the signature was
    checked. So one edited byte on a genuine, fully-verifiable receipt
    produced "this verifier is too old" -- a statement about the verifier's
    own capability, chosen by whoever edited the bytes, carrying
    ``is_tampered = False``.

    That is the integrity/capability inversion the six-state taxonomy exists
    to prevent, and it is a deniability primitive: anyone on the transport
    path could convert a VERIFIED receipt into something an operator resolves
    by upgrading rather than by escalating.

    The bundle here is genuinely signed and verifies untouched, so the only
    thing claiming otherwise is the envelope.
    """
    kid, key_set, sign = _local_signer()
    payload = _hashed_payload(
        {"receipt_id": "rcpt-downgrade", "kid": kid, "alg": "Ed25519"}
    )

    baseline = verify_bundle(_signed_bundle(payload, kid=kid, sign=sign), key_set)
    assert baseline.status is VerificationStatus.VERIFIED, (
        "fixture must verify untouched, or the test proves nothing"
    )

    result = verify_bundle(
        _signed_bundle(payload, kid=kid, sign=sign, **overrides), key_set
    )

    assert result.status is not VerificationStatus.UNSUPPORTED, (
        f"an edited envelope {label} downgraded a genuine receipt to "
        "UNSUPPORTED, which reports a tampered bundle as a verifier limitation"
    )
    assert result.status is VerificationStatus.MISMATCH
    # The envelope disagreeing with signed content means the bundle must not
    # be accepted. Deliberately is_rejected rather than is_tampered: MISMATCH
    # also covers caller-expectation failures like expected_issuer, and
    # calling those forgery would be the same category error in reverse.
    assert result.is_rejected
    assert not result.is_tampered


def test_envelope_kid_relabelling_is_a_mismatch_not_a_signature_failure():
    """Selecting the key from the signed payload makes relabelling inert.

    Key selection used to read the envelope's ``kid``, so relabelling a bundle
    to a *different published* key made the verifier fetch the wrong key and
    report INVALID -- indistinguishable from a forged payload, even though the
    signed bytes were untouched. Selecting from the signed payload means the
    envelope cannot steer key selection at all.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    kid, key_set, sign = _local_signer()
    # A genuinely different published key. _local_signer() hardcodes its kid,
    # so calling it twice yields one kid with the second key silently
    # overwriting the first -- which makes the signature fail for the wrong
    # reason and proves nothing about relabelling.
    other_kid = "local-test-key-2"
    other_public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    key_set = {**key_set, other_kid: other_public}

    payload = _hashed_payload(
        {"receipt_id": "rcpt-relabel", "kid": kid, "alg": "Ed25519"}
    )
    # Sign under `kid`, then relabel the envelope to the other published key.
    # Built and mutated rather than passed through **overrides, because
    # _signed_bundle takes `kid` as a named parameter.
    bundle = _signed_bundle(payload, kid=kid, sign=sign)
    bundle["kid"] = other_kid

    result = verify_bundle(bundle, key_set)

    assert result.status is VerificationStatus.MISMATCH
    assert result.is_rejected
    # Reported against the key the signed bytes actually name.
    assert result.key_id == kid
