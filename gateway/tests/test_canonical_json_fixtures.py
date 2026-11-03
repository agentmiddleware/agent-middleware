"""Regression fixtures for `awi-canonical-json/1` (RFC 8785 + typed pre-map).

The signing input is canonicalized by `app.services.signing_keys.canonical_json`
before it is hashed or Ed25519-signed, so its exact byte output is part of the
receipt contract: a silent change here invalidates every previously issued
signature.

JSON-native values are emitted per RFC 8785 (JCS): ECMAScript number
serialization, UTF-16BE key order, UTF-8 strings. `Decimal` and `datetime` are
not JSON types, so they are mapped to strings first (RFC 8785 Appendix D/E).
These fixtures pin both layers.
"""


from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.signing_keys import canonical_json


def test_decimal_normalizes_to_a_fixed_point_string_not_a_json_number():
    """Decimals become quoted fixed-point strings; floats stay bare JCS numbers.

    Carrying money as a string keeps the signed amount free of binary
    floating-point rounding (RFC 8785 Appendix D).
    """
    assert canonical_json({"a": Decimal("1.2300")}) == '{"a":"1.23"}'
    assert canonical_json({"a": Decimal("1.23")}) == '{"a":"1.23"}'

    # A float of the same value is *not* string-wrapped — the types are not
    # interchangeable in the signing input.
    assert canonical_json({"a": 1.23}) == '{"a":1.23}'


def test_rfc8785_number_serialization_matches_ecmascript():
    """Official-style RFC 8785 / ES Number::toString forms (Appendix B)."""
    assert canonical_json({"a": 1.0}) == '{"a":1}'
    assert canonical_json({"a": 0.0}) == '{"a":0}'
    assert canonical_json({"a": 4e-05}) == '{"a":0.00004}'
    assert canonical_json({"a": 1e-7}) == '{"a":1e-7}'
    assert canonical_json({"a": 9007199254740992.0}) == '{"a":9007199254740992}'


def test_rfc8785_rejects_unsafe_integers_and_nonfinite():
    import pytest
    from app.services.jcs import CanonicalizationError

    # 2**53 is exactly representable as a double, so an int of that value
    # round-trips through the float serializer (needed after json.loads).
    assert canonical_json({"a": 9007199254740992}) == '{"a":9007199254740992}'
    with pytest.raises(CanonicalizationError):
        canonical_json({"a": 9007199254740993})
    with pytest.raises(CanonicalizationError):
        canonical_json({"a": float("nan")})


def test_decimal_normalization_collapses_exponents_and_trailing_zeros():
    """`normalize()` then fixed-point formatting, so no `1E+3` reaches a signature."""
    assert canonical_json({"a": Decimal("100")}) == '{"a":"100"}'
    assert canonical_json({"a": Decimal("1E+3")}) == '{"a":"1000"}'
    assert canonical_json({"a": Decimal("0.000")}) == '{"a":"0"}'


def test_naive_datetime_is_assumed_utc_and_coerced_to_iso_8601():
    """A tz-naive value is stamped UTC rather than rejected or left ambiguous."""
    assert (
        canonical_json({"t": datetime(2026, 8, 26, 12, 30, 0)})
        == '{"t":"2026-08-26T12:30:00+00:00"}'
    )


def test_aware_datetime_is_converted_to_utc_before_serialization():
    """Two instants that are equal must canonicalize identically regardless of offset."""
    aware = datetime(2026, 8, 26, 12, 30, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert canonical_json({"t": aware}) == '{"t":"2026-08-26T19:30:00+00:00"}'

    same_instant_utc = datetime(2026, 8, 26, 19, 30, 0, tzinfo=timezone.utc)
    assert canonical_json({"t": aware}) == canonical_json({"t": same_instant_utc})


def test_normalization_reaches_nested_containers():
    """The rules apply at every depth, since signing inputs are nested payloads."""
    payload = {
        "outer": {
            "cost": Decimal("2.500"),
            "issued_at": datetime(2026, 8, 26, 0, 0, 0),
        },
        "entries": [{"amount": Decimal("0.10")}],
    }
    assert canonical_json(payload) == (
        '{"entries":[{"amount":"0.1"}],'
        '"outer":{"cost":"2.5","issued_at":"2026-08-26T00:00:00+00:00"}}'
    )


def test_rfc8785_emits_utf8_strings():
    assert canonical_json({"k": "café"}) == '{"k":"café"}'
