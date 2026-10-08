"""Behavior tests for the ACTA receipt transcoder.

These cover refusal paths the gateway suite does not pin: unsigned envelope
fields must not skip a signature check, canonical bytes must follow JCS key
order, and the demo must load the key file that sits next to the bundle.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import acta_receipt_interop as interop
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _public_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _make_bundle(private_key: Ed25519PrivateKey, kid: str, **overrides):
    claims = {
        "alg": "Ed25519",
        "created_at": "2026-08-15T00:00:00+00:00",
        "credits_charged": "1",
        "idempotency_record_id": "idm-test",
        "kid": kid,
        "ledger_entry_id": "ledger-test",
        "outcome": "success",
        "permit_id": "permit-test",
        "receipt_id": "rcpt-test",
        "request_hash": "aa" * 32,
        "response_hash": "bb" * 32,
        "tool": "partner.echo",
    }
    claims.update(overrides.pop("claims", {}))
    signing_input = json.dumps(
        claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    bundle = {
        "issuer": "https://example.invalid",
        "kid": kid,
        "signature": base64.b64encode(private_key.sign(signing_input.encode("utf-8"))).decode(
            "ascii"
        ),
        "signing_input": signing_input,
    }
    bundle.update(overrides)
    return bundle


def _trust_document(private_key: Ed25519PrivateKey, kid: str, *, status: str | None = None):
    entry = {
        "alg": "Ed25519",
        "kid": kid,
        "public_key_b64": _public_b64(private_key),
    }
    if status is not None:
        entry["status"] = status
    return {"issuer": "https://example.invalid", "keys": [entry]}


def _write_pair(directory: Path, bundle: dict, keys: dict) -> Path:
    bundle_path = directory / "receipt.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    (directory / "trust-keys.json").write_text(json.dumps(keys), encoding="utf-8")
    return bundle_path


def test_ascii_object_canonical_bytes_stay_stable() -> None:
    """Ordinary keys and values keep the compact sorted form callers already sign."""
    assert interop.jcs_canonicalize({"b": 1, "a": "x", "ok": True, "n": None}) == (
        b'{"a":"x","b":1,"n":null,"ok":true}'
    )


def test_jcs_orders_keys_by_utf16_code_units() -> None:
    """RFC 8785 sorts object keys by UTF-16 code units, not Unicode code points."""
    encoded = interop.jcs_canonicalize({"\uffff": "bmp", "\U0001f600": "emoji"})
    assert encoded.index("\U0001f600".encode()) < encoded.index("\uffff".encode())


def test_lone_surrogate_is_refused() -> None:
    """A lone surrogate has no single UTF-8 form, so it must not be signed."""
    with pytest.raises(interop.TranscodeError, match="surrogate"):
        interop.jcs_canonicalize({"text": "\ud800"})


def test_non_exact_integer_is_refused() -> None:
    """Integers that are not exact JSON numbers would diverge across languages."""
    with pytest.raises(interop.TranscodeError, match="integer"):
        interop.jcs_canonicalize({"n": 2**53 + 1})


def test_tuple_is_not_silently_signed_as_an_array() -> None:
    """A tuple is not a JSON type. Signing it as an array would hide the mismatch."""
    with pytest.raises(interop.TranscodeError, match="unsupported"):
        interop.jcs_canonicalize({"items": (1, 2)})


def test_nested_float_is_refused() -> None:
    """Float refusal applies at every depth, not only the top object."""
    with pytest.raises(interop.TranscodeError, match="float"):
        interop.jcs_canonicalize({"outer": [{"amount": 1.5}]})


def test_zero_credit_string_is_a_real_claim() -> None:
    """A zero charge is stored as the string 0 and is still an economic claim."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a", claims={"credits_charged": "0"})
    verified = interop.verify_portable_bundle(bundle, _public_b64(key))
    assert verified.claims["credits_charged"] == "0"


def test_lying_envelope_alg_does_not_skip_signature_check() -> None:
    """An unsigned alg field must not hide a bad signature behind a verifier limit."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["alg"] = "ES256"
    bundle["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    with pytest.raises(interop.TranscodeError, match="does not verify"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_lying_envelope_alg_on_a_valid_signature_is_a_mismatch() -> None:
    """A valid Ed25519 receipt whose envelope names another alg is a bad bundle."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["alg"] = "ES256"
    with pytest.raises(interop.TranscodeError, match="does not match the signed alg"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_garbage_signature_is_transcode_error() -> None:
    """Corrupt base64 is a refusal, not a raw decoder traceback."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["signature"] = "not base64!!!"
    with pytest.raises(interop.TranscodeError, match="base64"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_non_json_signing_input_is_transcode_error() -> None:
    """Bytes that verify but are not a JSON object cannot be transcoded."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["signing_input"] = "not-json"
    bundle["signature"] = base64.b64encode(key.sign(b"not-json")).decode("ascii")
    with pytest.raises(interop.TranscodeError, match="JSON"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_surrogate_signing_input_is_transcode_error() -> None:
    """signing_input that is not Unicode text is refused before verification."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    bundle["signing_input"] = "\ud800"
    with pytest.raises(interop.TranscodeError, match="Unicode"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_payload_hash_mismatch_is_refused() -> None:
    """A signed payload whose payload_hash does not match is not transcoded."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a", claims={"payload_hash": "00" * 32})
    with pytest.raises(interop.TranscodeError, match="payload_hash"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_matching_payload_hash_is_accepted() -> None:
    """The hash check uses the trust-plane canonical form, then accepts the bundle."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    claims = json.loads(bundle["signing_input"])
    canonical = json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    claims["payload_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    signing_input = json.dumps(
        claims, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    bundle["signing_input"] = signing_input
    bundle["signature"] = base64.b64encode(key.sign(signing_input.encode("utf-8"))).decode(
        "ascii"
    )
    verified = interop.verify_portable_bundle(bundle, _public_b64(key))
    assert verified.claims["payload_hash"] == claims["payload_hash"]


def test_signed_non_ed25519_alg_is_refused() -> None:
    """A payload that declares another algorithm is not reported as Ed25519 success."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a", claims={"alg": "ES256"})
    with pytest.raises(interop.TranscodeError, match="Ed25519"):
        interop.verify_portable_bundle(bundle, _public_b64(key))


def test_blank_public_key_is_transcode_error() -> None:
    """An empty key string is a refusal, not a cryptography ValueError."""
    key = Ed25519PrivateKey.generate()
    bundle = _make_bundle(key, "kid-a")
    with pytest.raises(interop.TranscodeError, match="public key"):
        interop.verify_portable_bundle(bundle, "")


def test_sign_refuses_a_body_that_already_has_a_signature() -> None:
    """Signing must not overwrite a signature field that was included in the bytes."""
    key = Ed25519PrivateKey.generate()
    body = {
        "issuer_id": "test-kid",
        "signature": {"alg": "Ed25519", "kid": "test-kid", "sig": "00"},
        "type": interop.ACTA_TYPE,
    }
    with pytest.raises(interop.TranscodeError, match="signature"):
        interop.sign_acta_receipt(body, key, "test-kid")


def test_short_outer_signature_is_refused_as_malformed() -> None:
    """A truncated hex signature is not reported as a cryptographic mismatch."""
    key = Ed25519PrivateKey.generate()
    inner = Ed25519PrivateKey.generate()
    bundle = _make_bundle(inner, "kid-a")
    verified = interop.verify_portable_bundle(bundle, _public_b64(inner))
    signed = interop.sign_acta_receipt(
        interop.to_acta_receipt(verified, issuer_id="test-kid"),
        key,
        kid="test-kid",
    )
    signed["signature"]["sig"] = "aa"
    with pytest.raises(interop.TranscodeError, match="64"):
        interop.verify_acta_receipt(signed, key.public_key())


def test_demo_reads_trust_keys_beside_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key snapshot ships next to the bundle, not at a fixed working directory."""
    monkeypatch.chdir(tmp_path)
    key = Ed25519PrivateKey.generate()
    bundle_path = _write_pair(
        tmp_path,
        _make_bundle(key, "kid-a"),
        _trust_document(key, "kid-a"),
    )
    assert interop._demo(str(bundle_path)) == 0


def test_demo_refuses_a_disabled_trust_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled key in the snapshot must not validate the inner receipt."""
    monkeypatch.chdir(tmp_path)
    key = Ed25519PrivateKey.generate()
    bundle_path = _write_pair(
        tmp_path,
        _make_bundle(key, "kid-a"),
        _trust_document(key, "kid-a", status="disabled"),
    )
    with pytest.raises(interop.TranscodeError, match="disabled"):
        interop._demo(str(bundle_path))
