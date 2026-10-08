"""Spec conformance: every example in spec/ validates against its schema,
and the schemas match what gateway/app actually emits.

Covers spec/permit-v0.1.md, spec/receipt-v0.1.md, spec/key-set-v0.1.md and
spec/test-vectors/. Pure unit tests: no database, no network, no sleeps.
The fixed Ed25519 test seed bytes(range(32)) is public test material only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = ROOT / "spec"
VECTORS = SPEC / "test-vectors"
RECEIPT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "receipt_signing_golden"
PERMIT_GOLDEN = ROOT / "gateway" / "tests" / "fixtures" / "permit_signing_golden"

sys.path.insert(0, str(ROOT / "sdk" / "python" / "src"))

from b2a_sdk.receipt_verifier import (  # noqa: E402
    CANONICALIZATION,
    key_set_from_document,
    verify_bundle,
)

from app.schemas.trust import PortableReceiptResponse  # noqa: E402
from app.services.permits import PermitService  # noqa: E402
from app.services.receipts import ReceiptService  # noqa: E402
from app.services.signing_keys import canonical_json as gateway_canonical_json  # noqa: E402

TEST_SEED = bytes(range(32))
TEST_KID = "sk_golden_0001"
TEST_PUBKEY = Ed25519PrivateKey.from_private_bytes(TEST_SEED).public_key()

# Members named by the field tables in spec/permit-v0.1.md.
PERMIT_REQUIRED = {
    "permit_id",
    "issuer_wallet_id",
    "subject_wallet_id",
    "subject_key_id",
    "scopes",
    "allowed_tools",
    "max_credits",
    "issued_at",
    "expires_at",
    "nonce",
    "status",
    "alg",
    "kid",
    "payload_hash",
}
# Optional members named in spec/permit-v0.1.md (additive).
PERMIT_OPTIONAL = {
    "requires_human_approval",
    "max_calls_per_tool",
    "aggregate_value_cap",
    "forbidden_fields",
    "recipient_domain",
    "allow_identical_repeats",
    "repeat_window_seconds",
    # action_* binding: all six together, or none.
    "action_contract_version",
    "action_payload_hash",
    "action_schema_id",
    "action_schema_version",
    "action_public_tool_id",
    "action_upstream_binding_hash",
}
# Members named by the signed-payload table in spec/receipt-v0.1.md.
RECEIPT_DOCUMENTED = {
    "receipt_id",
    "permit_id",
    "wallet_id",
    "key_id",
    "tool",
    "idempotency_record_id",
    "dispatch_attempt_id",
    "request_hash",
    "response_hash",
    "outcome",
    "credits_authorized",
    "credits_charged",
    "ledger_entry_id",
    "audit_event_id",
    "created_at",
    "alg",
    "kid",
    "payload_hash",
}
# Additive receipt members named in spec/receipt-v0.1.md ("approval ids,
# action bindings, constraint denials") plus reason_code, which the gateway
# signs when present.
RECEIPT_ADDITIVE = {
    "reason_code",
    "approval_id",
    "constraints_evaluated",
    "action_contract_version",
    "action_payload_hash",
    "action_schema_id",
    "action_schema_version",
    "action_public_tool_id",
    "action_upstream_binding_hash",
}
# Always-signed receipt members: present even when the value is null.
RECEIPT_ALWAYS_SIGNED = RECEIPT_DOCUMENTED - {
    "idempotency_record_id",
    "dispatch_attempt_id",
}
# Portable bundle members from spec/receipt-v0.1.md ("Portable bundle").
PORTABLE_MEMBERS = {
    "receipt_id",
    "kid",
    "alg",
    "canonicalization",
    "signing_input",
    "signature",
}
# Outcomes table in spec/receipt-v0.1.md.
SPEC_OUTCOMES = {
    "success",
    "denied",
    "insufficient_funds",
    "failed_refunded",
    "failed_unrefunded",
    "delivery_uncertain",
    "response_rejected",
}


def _code_block(path: Path) -> str:
    text = path.read_text()
    match = re.search(r"```json\n(.*)\n```", text, re.S)
    assert match, f"no json code block in {path}"
    return match.group(1).strip()


def _recompute_payload_hash(payload: dict) -> bool:
    stated = payload.get("payload_hash")
    inner = {k: v for k, v in payload.items() if k != "payload_hash"}
    # Gateway canonicalization: normalizes Decimal and datetime values the
    # way the signer did before hashing.
    canonical = gateway_canonical_json(inner)
    return hashlib.sha256(canonical.encode()).hexdigest() == stated


def _permit_model(**overrides):
    from app.db.models import PermitModel

    fields = dict(
        permit_id="pmt_spec_0001",
        issuer_wallet_id="agt-spec-issuer",
        subject_wallet_id="agt-spec-subject",
        subject_key_id="key_spec_0001",
        scopes_json=json.dumps(["tool:echo:invoke", "billing:charge"]),
        allowed_tools_json=json.dumps(["echo"]),
        max_credits=Decimal("50"),
        expires_at=datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc),
        nonce="nonce_spec_0001",
        status="active",
        signature="",
        key_id=TEST_KID,
        issued_at=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return PermitModel(**fields)


def _receipt_model(**overrides):
    from app.db.models import ReceiptModel

    fields = dict(
        receipt_id="rcpt_spec_0001",
        idempotency_record_id="idem_spec_0001",
        dispatch_attempt_id="disp_spec_0001",
        permit_id="pmt_spec_0001",
        wallet_id="agt-spec-0001",
        key_id="key_spec_0001",
        tool="echo",
        request_hash="a" * 64,
        response_hash="b" * 64,
        ledger_entry_id="led_spec_0001",
        credits_authorized=Decimal("2.5"),
        credits_charged=Decimal("2.5"),
        outcome="success",
        reason_code=None,
        audit_event_id="aud_spec_0001",
        approval_id=None,
        constraints_evaluated_json=None,
        created_at=datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc),
        signature="",
        signature_key_id=TEST_KID,
    )
    fields.update(overrides)
    return ReceiptModel(**fields)


class TestPermitExample:
    def test_example_has_exactly_the_documented_required_members(self):
        payload = json.loads(_code_block(SPEC / "permit-v0.1.md"))
        assert set(payload) == PERMIT_REQUIRED
        assert isinstance(payload["scopes"], list) and payload["scopes"]
        assert isinstance(payload["allowed_tools"], list) and payload["allowed_tools"]
        assert re.fullmatch(r"\d+(\.\d+)?", payload["max_credits"])
        assert payload["status"] == "active"
        assert payload["alg"] == "Ed25519"
        assert isinstance(payload["kid"], str) and payload["kid"]
        # Validity window parses as ISO-8601 and is ordered.
        issued = datetime.fromisoformat(payload["issued_at"])
        expires = datetime.fromisoformat(payload["expires_at"])
        assert issued < expires
        # subject_key_id may be null (unbound permit); here it is bound.
        assert isinstance(payload["subject_key_id"], str)

    def test_example_payload_hash_recomputes(self):
        payload = json.loads(_code_block(SPEC / "permit-v0.1.md"))
        assert _recompute_payload_hash(payload)

    def test_example_is_canonical_byte_order(self):
        raw = _code_block(SPEC / "permit-v0.1.md")
        assert raw == gateway_canonical_json(json.loads(raw))

    def test_example_matches_gateway_golden_bytes(self):
        raw = _code_block(SPEC / "permit-v0.1.md")
        golden = json.loads(
            (PERMIT_GOLDEN / "active_minimal.json").read_text()
        )["signing_input"]
        assert raw == golden

    def test_example_signature_verifies_under_test_seed(self):
        raw = _code_block(SPEC / "permit-v0.1.md")
        golden = json.loads((PERMIT_GOLDEN / "active_minimal.json").read_text())
        signature = base64.b64decode(golden["signature_b64_over_test_seed"])
        TEST_PUBKEY.verify(signature, raw.encode())

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"subject_key_id": None},
            {
                "requires_human_approval": True,
                "max_calls_per_tool_json": json.dumps({"echo": 3}),
                "forbidden_fields_json": json.dumps(["secret_token"]),
                "recipient_domain": "partner.example",
            },
            {
                "action_contract_version": 1,
                "action_payload_hash": "c" * 64,
                "action_schema_id": "fixture-payment",
                "action_schema_version": "1",
                "action_public_tool_id": "echo",
                "action_upstream_binding_hash": "d" * 64,
            },
        ],
        ids=["minimal", "unbound_key", "v2_constraints", "action_binding"],
    )
    def test_gateway_permit_payload_matches_spec_schema(self, overrides):
        payload = PermitService._verification_payload(_permit_model(**overrides))
        assert PERMIT_REQUIRED <= set(payload)
        assert set(payload) <= PERMIT_REQUIRED | PERMIT_OPTIONAL
        assert _recompute_payload_hash(payload)

    def test_gateway_never_signs_mutable_spend(self):
        payload = PermitService._verification_payload(
            _permit_model(spent_credits=Decimal("12.5"))
        )
        assert "spent_credits" not in payload


class TestReceiptDocBundle:
    def test_doc_bundle_shape(self):
        bundle = json.loads(_code_block(SPEC / "receipt-v0.1.md"))
        assert set(bundle) == PORTABLE_MEMBERS
        assert bundle["alg"] == "Ed25519"
        assert bundle["canonicalization"] == "awi-canonical-json/1" == CANONICALIZATION

    def test_gateway_portable_response_covers_spec_bundle(self):
        fields = set(PortableReceiptResponse.model_fields)
        from app.services.signing_keys import RECEIPT_CANONICALIZATION

        assert PORTABLE_MEMBERS <= fields
        assert PortableReceiptResponse.model_fields["alg"].default == "Ed25519"
        assert (
            PortableReceiptResponse.model_fields["canonicalization"].default
            == "awi-canonical-json/1"
            == CANONICALIZATION
            == RECEIPT_CANONICALIZATION
        )

    def test_sdk_accepts_gateway_shaped_bundle(self):
        vectors = json.loads((VECTORS / "receipts.json").read_text())
        keys = key_set_from_document(json.loads((VECTORS / "keys.json").read_text()))
        valid = next(c for c in vectors["cases"] if c["expect"] == "verified")
        gateway_shaped = dict(
            valid["bundle"],
            schema_version="1.0",
            issuer="https://example.test",
            keys_url="/.well-known/trust-keys.json",
        )
        assert verify_bundle(gateway_shaped, keys).status.value == "verified"
        mismatched = verify_bundle(
            gateway_shaped, keys, expected_issuer="https://other.test"
        )
        assert mismatched.status.value == "mismatch"


class TestKeySetDoc:
    def test_doc_example_has_documented_shape(self):
        # The doc example is a shape illustration with angle-bracket
        # placeholders, not usable key material (see the verifier test
        # below, which requires it to yield zero keys).
        doc = json.loads(_code_block(SPEC / "key-set-v0.1.md"))
        assert set(doc) == {"keys"}
        (entry,) = doc["keys"]
        assert {"kid", "alg", "status", "public_key_b64", "jwk"} <= set(entry)
        assert entry["alg"] == "Ed25519"
        assert entry["status"] == "active"
        assert set(entry["jwk"]) == {"kty", "crv", "x"}
        assert entry["jwk"]["kty"] == "OKP"
        assert entry["jwk"]["crv"] == "Ed25519"
        assert entry["public_key_b64"].startswith("<")
        assert entry["jwk"]["x"].startswith("<")

    def test_doc_example_placeholders_agree_with_each_other(self):
        # Both encodings of the placeholder describe the same 32-byte key:
        # the raw form names the length, the JWK form mirrors it.
        doc = json.loads(_code_block(SPEC / "key-set-v0.1.md"))
        (entry,) = doc["keys"]
        assert "32-byte" in entry["public_key_b64"]

    def test_doc_example_loads_through_offline_verifier(self):
        # The doc example carries angle-bracket placeholders, not key
        # material. The offline verifier must yield no keys for it: a
        # placeholder that parsed as a usable key would be worse than none.
        doc = json.loads(_code_block(SPEC / "key-set-v0.1.md"))
        assert key_set_from_document(doc) == {}

    def test_vectors_keys_match_seed_and_doc_shape(self):
        doc = json.loads((VECTORS / "keys.json").read_text())
        (entry,) = doc["keys"]
        assert {"kid", "alg", "status", "public_key_b64", "jwk"} <= set(entry)
        assert entry["kid"] == TEST_KID
        assert entry["status"] == "active"
        expected_pub = Ed25519PrivateKey.from_private_bytes(TEST_SEED).public_key()
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        raw_pub = expected_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert base64.b64decode(entry["public_key_b64"]) == raw_pub
        keys = key_set_from_document(doc)
        assert set(keys) == {TEST_KID}
        assert keys[TEST_KID] == raw_pub

    def test_gateway_trust_keys_shape_loads_offline(self):
        from app.routers.well_known import _published_key

        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        active = SimpleNamespace(
            key_id=TEST_KID,
            alg="Ed25519",
            public_key_b64=base64.b64encode(b"1" * 32).decode(),
            status="active",
            created_at=now,
            activated_at=now,
            retired_at=None,
        )
        retired = SimpleNamespace(**{**vars(active), "status": "retired"})
        disabled = SimpleNamespace(**{**vars(active), "status": "disabled"})
        jwk_only = {
            "kid": "sk_jwk_only",
            "alg": "Ed25519",
            "status": "active",
            "jwk": {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": base64.urlsafe_b64encode(b"2" * 32).decode().rstrip("="),
            },
        }
        doc = {"keys": [_published_key(active), _published_key(retired),
                        _published_key(disabled), jwk_only]}
        # Every gateway entry carries the members the spec doc names.
        for entry in doc["keys"][:3]:
            assert {"kid", "alg", "status", "public_key_b64", "jwk"} <= set(entry)
            assert {"kty", "crv", "x"} <= set(entry["jwk"])
        keys = key_set_from_document(doc)
        # Retired keys stay verifiable, disabled keys are dropped, and the
        # JWK mirror is accepted when the raw form is absent.
        assert set(keys) == {TEST_KID, "sk_jwk_only"}


class TestReceiptVectors:
    def _load(self):
        keys = key_set_from_document(json.loads((VECTORS / "keys.json").read_text()))
        cases = json.loads((VECTORS / "receipts.json").read_text())["cases"]
        return keys, cases

    def test_every_vector_produces_its_expected_status(self):
        keys, cases = self._load()
        assert cases
        failures = []
        for case in cases:
            case_keys = (
                key_set_from_document(case["keys"]) if "keys" in case else keys
            )
            actual = verify_bundle(case["bundle"], case_keys).status.value
            if actual != case["expect"]:
                failures.append((case["name"], actual, case["expect"]))
        assert not failures

    def test_vector_bundles_match_spec_portable_shape(self):
        _, cases = self._load()
        for case in cases:
            if case["expect"] == "malformed":
                continue
            assert PORTABLE_MEMBERS <= set(case["bundle"]), case["name"]
            assert case["bundle"]["canonicalization"] == CANONICALIZATION, case["name"]

    def test_valid_vectors_have_documented_payloads(self):
        keys, cases = self._load()
        for case in cases:
            if case["expect"] != "verified":
                continue
            payload = json.loads(case["bundle"]["signing_input"])
            assert RECEIPT_ALWAYS_SIGNED <= set(payload), case["name"]
            assert set(payload) <= RECEIPT_DOCUMENTED | RECEIPT_ADDITIVE, case["name"]
            assert _recompute_payload_hash(payload), case["name"]
            assert payload["outcome"] in SPEC_OUTCOMES, case["name"]
            assert re.fullmatch(r"\d+(\.\d+)?", payload["credits_authorized"])
            assert re.fullmatch(r"\d+(\.\d+)?", payload["credits_charged"])
            datetime.fromisoformat(payload["created_at"])
            assert payload["kid"] == case["bundle"]["kid"] == TEST_KID

    def test_vectors_cover_every_gateway_golden_fixture(self):
        _, cases = self._load()
        names = {c["name"] for c in cases}
        goldens = sorted(p.stem for p in RECEIPT_GOLDEN.glob("*.json"))
        assert goldens
        for stem in goldens:
            assert f"{stem}.valid" in names
            assert f"{stem}.bad_signature" in names
            fixture = json.loads((RECEIPT_GOLDEN / f"{stem}.json").read_text())
            signature = base64.b64decode(fixture["signature_b64_over_test_seed"])
            TEST_PUBKEY.verify(signature, fixture["signing_input"].encode())

    def test_idempotency_linkage_is_signature_bound(self):
        # Replay protection rests on idempotency_record_id being inside the
        # signed bytes: moving a receipt to another idempotency record must
        # break the signature, not produce a second valid receipt.
        _, cases = self._load()
        linked = next(
            c for c in cases
            if c["expect"] == "verified"
            and "idempotency_record_id" in json.loads(c["bundle"]["signing_input"])
        )
        payload = json.loads(linked["bundle"]["signing_input"])
        payload["idempotency_record_id"] = "idem_rewritten_by_attacker"
        forged_input = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        forged = dict(linked["bundle"], signing_input=forged_input)
        keys = key_set_from_document(json.loads((VECTORS / "keys.json").read_text()))
        assert verify_bundle(forged, keys).status.value == "invalid"

    def test_denied_receipts_move_no_money(self):
        _, cases = self._load()
        denied = [
            c for c in cases
            if c["expect"] == "verified"
            and json.loads(c["bundle"]["signing_input"])["outcome"]
            in ("denied", "insufficient_funds")
        ]
        assert denied, "vectors must pin the no-charge-on-denial invariant"
        for case in denied:
            payload = json.loads(case["bundle"]["signing_input"])
            assert payload["credits_charged"] == "0", case["name"]
            assert payload["ledger_entry_id"] is None, case["name"]


class TestGatewayReceiptPayloadParity:
    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"idempotency_record_id": None, "dispatch_attempt_id": None},
            {"approval_id": "apr_spec_0001"},
            {
                "reason_code": "permit_denied",
                "outcome": "denied",
                "credits_charged": Decimal("0"),
                "ledger_entry_id": None,
                "response_hash": None,
            },
            {"constraints_evaluated_json": json.dumps({"max_calls": "ok"})},
            {
                "action_contract_version": 1,
                "action_payload_hash": "c" * 64,
                "action_schema_id": "fixture-payment",
                "action_schema_version": "1",
                "action_public_tool_id": "echo",
                "action_upstream_binding_hash": "d" * 64,
            },
        ],
        ids=[
            "linked",
            "legacy",
            "approval",
            "denial",
            "constraints",
            "action_binding",
        ],
    )
    def test_gateway_receipt_payload_matches_spec_schema(self, overrides):
        for include_linkage in (True, False):
            payload = ReceiptService._verification_payload(
                _receipt_model(**overrides), include_linkage=include_linkage
            )
            assert RECEIPT_ALWAYS_SIGNED <= set(payload)
            assert set(payload) <= RECEIPT_DOCUMENTED | RECEIPT_ADDITIVE
            assert _recompute_payload_hash(payload)

    def test_linkage_members_sign_only_when_set(self):
        linked = ReceiptService._verification_payload(
            _receipt_model(), include_linkage=True
        )
        assert linked["idempotency_record_id"] == "idem_spec_0001"
        assert linked["dispatch_attempt_id"] == "disp_spec_0001"
        legacy = ReceiptService._verification_payload(
            _receipt_model(), include_linkage=False
        )
        assert "idempotency_record_id" not in legacy
        assert "dispatch_attempt_id" not in legacy

    def test_gateway_and_sdk_canonical_json_agree(self):
        from b2a_sdk.receipt_verifier import canonical_json as sdk_canonical_json

        payloads = [
            {"amount": "2.50", "note": "caf\u00e9 \u2603 plain"},
            {"nested": {"b": [1, None, True], "a": "x"}, "empty": {}},
            {"unicode_keys": "\u00e9"},
        ]
        for payload in payloads:
            assert gateway_canonical_json(payload) == sdk_canonical_json(payload)
        assert gateway_canonical_json({"v": Decimal("2.5000")}) == '{"v":"2.5"}'
        assert (
            gateway_canonical_json(
                {"t": datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc)}
            )
            == '{"t":"2026-09-14T17:00:00+00:00"}'
        )

    def test_offline_verifier_ignores_unusable_key_material(self):
        # Entries that cannot verify (bad base64, wrong length, unknown
        # algorithm) are skipped, never trusted; a document with no key
        # list is a caller error.
        from b2a_sdk.receipt_verifier import VerificationError

        with pytest.raises(VerificationError):
            key_set_from_document({"keys": "not-a-list"})
        with pytest.raises(VerificationError):
            key_set_from_document({"no_keys": []})
        assert (
            key_set_from_document(
                {
                    "keys": [
                        {
                            "kid": "bad",
                            "alg": "Ed25519",
                            "status": "active",
                            "public_key_b64": "!!!not-base64!!!",
                        }
                    ]
                }
            )
            == {}
        )
