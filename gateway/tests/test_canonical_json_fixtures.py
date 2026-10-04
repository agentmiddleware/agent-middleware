"""Regression fixtures pinning `awi-canonical-json/1`'s divergence from RFC 8785.

The signing input is canonicalized by `app.services.signing_keys.canonical_json`
before it is hashed or Ed25519-signed, so its exact byte output is part of the
receipt contract: a silent change here invalidates every previously issued
signature.

RFC 8785 (JCS) defines no `Decimal` and no datetime type — it covers only the
standard JSON types, and recommends applications carry such values as strings.
This canonicalizer takes that route, which is why the competitive record
describes the two as *related but not identical* and forbids calling the signing
inputs interchangeable (`docs/market-research-2026-08.md` §9.1). These fixtures
assert the two rules that produce the divergence so the claim stays testable
rather than asserted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.signing_keys import canonical_json


def test_decimal_normalizes_to_a_fixed_point_string_not_a_json_number():
    """Decimals become quoted fixed-point strings; floats stay bare numbers.

    This is the divergence from JCS, which would serialize a number per
    ECMAScript. Carrying money as a string is what keeps the signed amount free
    of binary floating-point rounding.
    """
    assert canonical_json({"a": Decimal("1.2300")}) == '{"a":"1.23"}'
    assert canonical_json({"a": Decimal("1.23")}) == '{"a":"1.23"}'

    # A float of the same value is *not* string-wrapped — the types are not
    # interchangeable in the signing input.
    assert canonical_json({"a": 1.23}) == '{"a":1.23}'


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
