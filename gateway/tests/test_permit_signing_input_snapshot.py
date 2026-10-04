"""Golden snapshot of the exact bytes a permit signature covers.

Refactor gate. The permit signing input is the authority half of the
one-tool loop: any change to the field set, key ordering, Decimal/datetime
normalization, or the payload_hash derivation silently invalidates every
permit already issued and breaks the SDK's LocalPermitValidator.

This test freezes representative permits to byte-exact canonical JSON, and
freezes the Ed25519 signature over those bytes under a fixed test seed.

If this test fails, the change is either a bug or an intentional permit
format change. An intentional change requires: (1) a permit schema version
bump, (2) an entry in CHANGELOG.md, (3) regenerating the golden files with
``UPDATE_PERMIT_GOLDEN=1 pytest tests/test_permit_signing_input_snapshot.py``.
Never regenerate to make a refactor PR green.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from app.db.database import get_session_factory
from app.db.models import PermitModel
from app.main import app
from app.schemas.trust import PermitCreateRequest
from app.services.permits import PermitService
from app.services.signing_keys import canonical_json, sha256_hex
from b2a_sdk.edge_client import LocalPermitValidator
from b2a_sdk.receipt_verifier import canonical_json as sdk_canonical_json
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "permit_signing_golden"
_TEST_SEED = bytes(range(32))
_FIXED_ISSUED_AT = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_EXPIRES_AT = datetime(2026, 9, 16, 13, 0, 0, tzinfo=timezone.utc)


def _base_model(**overrides: object) -> PermitModel:
    fields: dict[str, object] = dict(
        permit_id="pmt_golden_0001",
        issuer_wallet_id="agt-golden-issuer",
        subject_wallet_id="agt-golden-subject",
        subject_key_id="key_golden_0001",
        scopes_json=json.dumps(["tool:golden-path-echo:invoke", "billing:charge"]),
        allowed_tools_json=json.dumps(["golden-path-echo"]),
        max_credits=Decimal("50.00000000"),
        spent_credits=Decimal("0"),
        expires_at=_FIXED_EXPIRES_AT,
        nonce="nonce_golden_0001",
        status="active",
        requires_human_approval=False,
        signature="",
        key_id="sk_golden_0001",
        issued_at=_FIXED_ISSUED_AT,
        revoked_at=None,
        max_calls_per_tool_json=None,
        aggregate_value_cap=None,
        forbidden_fields_json=None,
        recipient_domain=None,
        tool_call_counts_json=None,
    )
    fields.update(overrides)
    return PermitModel(**fields)  # type: ignore[arg-type]


CASES = {
    # Minimal current-format permit: no additive fields.
    "active_minimal": _base_model(),
    # requires_human_approval is signed only when true.
    "active_approval": _base_model(
        permit_id="pmt_golden_0002",
        requires_human_approval=True,
    ),
    # All v2 constraints present; each is additive.
    "active_v2_constraints": _base_model(
        permit_id="pmt_golden_0003",
        max_calls_per_tool_json=json.dumps({"golden-path-echo": 3}),
        aggregate_value_cap=Decimal("10.00"),
        forbidden_fields_json=json.dumps(["secret_token"]),
        recipient_domain="partner.example",
    ),
    # subject_key_id is signed even when null.
    "active_unbound_key": _base_model(
        permit_id="pmt_golden_0004",
        subject_key_id=None,
    ),
    # Revocation is not in the signature: stored status may be revoked
    # while the signed payload still says active.
    "revoked_still_signs_active": _base_model(
        permit_id="pmt_golden_0005",
        status="revoked",
        revoked_at=_FIXED_EXPIRES_AT,
    ),
}


def _api_shaped(model: PermitModel) -> dict[str, object]:
    """Shape a model as GET /v1/permits/{id} so the SDK can reconstruct."""
    return {
        "permit_id": model.permit_id,
        "issuer_wallet_id": model.issuer_wallet_id,
        "subject_wallet_id": model.subject_wallet_id,
        "subject_key_id": model.subject_key_id,
        "scopes": json.loads(model.scopes_json),
        "allowed_tools": json.loads(model.allowed_tools_json),
        "max_credits": model.max_credits,
        "spent_credits": model.spent_credits,
        "expires_at": model.expires_at,
        "nonce": model.nonce,
        "status": model.status,
        "requires_human_approval": model.requires_human_approval,
        "signature": model.signature,
        "key_id": model.key_id,
        "issued_at": model.issued_at,
        "revoked_at": model.revoked_at,
        "max_calls_per_tool": json.loads(model.max_calls_per_tool_json or "{}"),
        "aggregate_value_cap": model.aggregate_value_cap,
        "forbidden_fields": json.loads(model.forbidden_fields_json or "[]"),
        "recipient_domain": model.recipient_domain,
    }


def _signing_input(name: str) -> str:
    return canonical_json(PermitService._verification_payload(CASES[name]))


def _signature_b64(signing_input: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(_TEST_SEED)
    return base64.b64encode(key.sign(signing_input.encode())).decode()


@pytest.mark.parametrize("name", sorted(CASES))
def test_permit_signing_input_is_byte_stable(name: str) -> None:
    signing_input = _signing_input(name)
    signature = _signature_b64(signing_input)
    golden_path = GOLDEN_DIR / f"{name}.json"
    actual = {
        "signing_input": signing_input,
        "signing_input_sha256": sha256_hex(signing_input),
        "signature_b64_over_test_seed": signature,
    }

    if os.environ.get("UPDATE_PERMIT_GOLDEN") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"golden regenerated: {golden_path}")

    assert golden_path.exists(), (
        f"missing golden {golden_path}; run with UPDATE_PERMIT_GOLDEN=1 once"
    )
    expected = json.loads(golden_path.read_text())
    assert actual["signing_input"] == expected["signing_input"], (
        "permit signing input changed — this invalidates every issued permit "
        "and the SDK LocalPermitValidator. See module docstring before "
        "regenerating."
    )
    assert actual == expected


def test_golden_fixtures_match_cases() -> None:
    fixture_names = {path.stem for path in GOLDEN_DIR.glob("*.json")}
    assert fixture_names == set(CASES)


def test_payload_hash_is_over_payload_without_itself() -> None:
    payload = PermitService._verification_payload(CASES["active_minimal"])
    stripped = {k: v for k, v in payload.items() if k != "payload_hash"}
    assert payload["payload_hash"] == sha256_hex(stripped)


def test_revoked_permit_payload_status_stays_active() -> None:
    payload = PermitService._verification_payload(CASES["revoked_still_signs_active"])
    assert payload["status"] == "active"
    assert "revoked_at" not in payload
    assert "spent_credits" not in payload


def test_additive_fields_absent_from_minimal_payload() -> None:
    payload = PermitService._verification_payload(CASES["active_minimal"])
    for field in (
        "requires_human_approval",
        "max_calls_per_tool",
        "aggregate_value_cap",
        "forbidden_fields",
        "recipient_domain",
    ):
        assert field not in payload


def test_approval_and_v2_fields_are_signed_when_set() -> None:
    approval = PermitService._verification_payload(CASES["active_approval"])
    assert approval["requires_human_approval"] is True
    v2 = PermitService._verification_payload(CASES["active_v2_constraints"])
    assert v2["max_calls_per_tool"] == {"golden-path-echo": 3}
    assert v2["forbidden_fields"] == ["secret_token"]
    assert v2["recipient_domain"] == "partner.example"
    assert "aggregate_value_cap" in v2


@pytest.mark.parametrize("name", sorted(CASES))
def test_unsigned_payload_is_verification_without_folded_fields(name: str) -> None:
    """create signs _unsigned_payload; verify folds alg/kid/hash onto it."""
    model = CASES[name]
    unsigned = PermitService._unsigned_payload(model)
    full = PermitService._verification_payload(model)
    for folded in ("alg", "kid", "payload_hash"):
        assert folded not in unsigned
    reconstructed = dict(unsigned)
    reconstructed["alg"] = "Ed25519"
    reconstructed["kid"] = model.key_id
    reconstructed["payload_hash"] = sha256_hex(reconstructed)
    assert reconstructed == full


@pytest.mark.parametrize("name", sorted(CASES))
def test_sdk_local_validator_matches_server_bytes(name: str) -> None:
    """LocalPermitValidator must reconstruct the same canonical bytes."""
    sdk_payload = LocalPermitValidator.permit_signing_payload(_api_shaped(CASES[name]))
    assert sdk_canonical_json(sdk_payload) == _signing_input(name)


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_created_permit_verifies_against_snapshot_payload(
    client: AsyncClient,
    clean_database,
) -> None:
    """create_permit bytes must match _verification_payload reconstruction."""
    provisioned = await provision_agent_wallet(client)
    permit = await create_tool_permit(
        client,
        wallet_id=provisioned["agent_wallet_id"],
        key_id=provisioned["key_id"],
        tool_name="golden-permit-echo",
        idem_key="permit-golden-signing",
    )
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit["permit_id"])
        assert model is not None
        assert await PermitService().verify_signature(model, session=session)
        unsigned = PermitService._unsigned_payload(model)
        reconstructed = PermitService._verification_payload(model)
        assert reconstructed["permit_id"] == permit["permit_id"]
        assert reconstructed["kid"] == model.key_id
        assert "requires_human_approval" not in reconstructed
        assert "alg" not in unsigned
        assert {
            k: v
            for k, v in reconstructed.items()
            if k not in {"alg", "kid", "payload_hash"}
        } == unsigned


@pytest.mark.anyio
async def test_created_v2_permit_signs_additive_fields(
    client: AsyncClient,
    clean_database,
) -> None:
    """v2 constraints must be in the bytes create signs and verify reconstructs."""
    provisioned = await provision_agent_wallet(client)
    permit = await PermitService().create_permit(
        PermitCreateRequest(
            issuer_wallet_id=provisioned["agent_wallet_id"],
            subject_wallet_id=provisioned["agent_wallet_id"],
            subject_key_id=provisioned["key_id"],
            allowed_tools=["golden-v2-echo"],
            scopes=["tool:golden-v2-echo:invoke", "billing:charge"],
            max_credits=Decimal("10"),
            aggregate_value_cap=Decimal("5"),
            forbidden_fields=["secret_token"],
            recipient_domain="partner.example",
            max_calls_per_tool={"golden-v2-echo": 3},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
    )
    factory = get_session_factory()
    async with factory() as session:
        model = await session.get(PermitModel, permit.permit_id)
        assert model is not None
        assert await PermitService().verify_signature(model, session=session)
        payload = PermitService._verification_payload(model)
        assert payload["max_calls_per_tool"] == {"golden-v2-echo": 3}
        assert payload["forbidden_fields"] == ["secret_token"]
        assert payload["recipient_domain"] == "partner.example"
        assert "aggregate_value_cap" in payload
