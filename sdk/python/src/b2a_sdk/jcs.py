"""RFC 8785 JSON Canonicalization Scheme (JCS) for receipt signing inputs.

KEEP IN SYNC across these identical copies:
  - gateway/app/services/jcs.py
  - sdk/python/src/b2a_sdk/jcs.py
  - spec/test-vectors/jcs.py
  - site/proof/jcs.py

Derived from the Trail of Bits RFC 8785 Python package (Apache-2.0) and
Anders Rundgren's cyberphone/json-canonicalization reference (Apache-2.0).
Numbers follow ECMAScript Number::toString / ECMA-262 section 7.1.12.1 as
required by RFC 8785 section 3.2.2.3. Object keys sort by UTF-16BE code
units (section 3.2.3). Strings are UTF-8 with only the RFC 8785 escapes.
Integers outside ±(2**53 - 1) are rejected unless they survive an
IEEE-754 round-trip as a float (so json.loads of an exact power of two such
as 2**53 still re-canonicalizes). Non-exact ints and NaN / Infinity are
rejected (Appendix B Note 1).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from io import BytesIO
from typing import IO, Any

_INT_MAX = 2**53 - 1
_INT_MIN = -(2**53) + 1

_ESCAPE = re.compile(r'[\x00-\x1f\\"\b\f\n\r\t]')
_ESCAPE_DCT = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
for _i in range(0x20):
    _ESCAPE_DCT.setdefault(chr(_i), f"\\u{_i:04x}")


class CanonicalizationError(ValueError):
    """Input cannot be represented under RFC 8785 / I-JSON."""


def _serialize_str(s: str, sink: IO[bytes]) -> None:
    def _replace(match: re.Match[str]) -> str:
        return _ESCAPE_DCT[match.group(0)]

    sink.write(b'"')
    try:
        sink.write(_ESCAPE.sub(_replace, s).encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("input contains non-UTF-8 codepoints") from exc
    sink.write(b'"')


def _serialize_float(f: float, sink: IO[bytes]) -> None:
    if math.isnan(f) or math.isinf(f):
        raise CanonicalizationError(f"{f} is not representable in JCS")
    if f == 0:
        sink.write(b"0")
        return
    if f < 0:
        sink.write(b"-")
        _serialize_float(-f, sink)
        return

    # Rundgren / trailofbits path: start from Python's shortest decimal, then
    # apply ECMAScript exponent / trailing-zero layout rules.
    stringified = str(f)
    exponent_str = ""
    exponent_value = 0
    q = stringified.find("e")
    if q > 0:
        exponent_str = stringified[q:]
        if len(exponent_str) > 2 and exponent_str[2:3] == "0":
            exponent_str = exponent_str[:2] + exponent_str[3:]
        stringified = stringified[0:q]
        exponent_value = int(exponent_str[1:])

    first = stringified
    dot = ""
    last = ""
    q = stringified.find(".")
    if q > 0:
        dot = "."
        first = stringified[:q]
        last = stringified[q + 1 :]

    if last == "0":
        dot = ""
        last = ""

    if 0 < exponent_value < 21:
        first += last
        last = ""
        dot = ""
        exponent_str = ""
        pad = exponent_value - len(first)
        while pad >= 0:
            pad -= 1
            first += "0"
    elif -7 < exponent_value < 0:
        last = first + last
        first = "0"
        dot = "."
        exponent_str = ""
        q = exponent_value
        while q < -1:
            q += 1
            last = "0" + last

    sink.write(f"{first}{dot}{last}{exponent_str}".encode())


def dump(obj: Any, sink: IO[bytes]) -> None:
    if obj is None:
        sink.write(b"null")
    elif isinstance(obj, bool):
        sink.write(b"true" if obj else b"false")
    elif isinstance(obj, int):
        # Safe integers serialize as decimal text. Integers outside
        # ±(2**53 - 1) are only accepted when they survive an IEEE-754
        # round-trip (RFC 8785 §3.1); otherwise reject (Appendix B Note 1).
        if _INT_MIN <= obj <= _INT_MAX:
            sink.write(str(obj).encode("utf-8"))
        else:
            try:
                as_float = float(obj)
            except OverflowError as exc:
                msg = f"{obj} outside JSON safe-integer range"
                raise CanonicalizationError(msg) from exc
            if as_float != obj or math.isnan(as_float) or math.isinf(as_float):
                raise CanonicalizationError(f"{obj} outside JSON safe-integer range")
            _serialize_float(as_float, sink)
    elif isinstance(obj, str):
        _serialize_str(obj, sink)
    elif isinstance(obj, float):
        _serialize_float(obj, sink)
    elif isinstance(obj, (list, tuple)):
        if not obj:
            sink.write(b"[]")
            return
        sink.write(b"[")
        for idx, elem in enumerate(obj):
            if idx:
                sink.write(b",")
            dump(elem, sink)
        sink.write(b"]")
    elif isinstance(obj, Mapping):
        if not obj:
            sink.write(b"{}")
            return
        try:
            items = sorted(obj.items(), key=lambda kv: kv[0].encode("utf-16be"))
        except AttributeError as exc:
            raise CanonicalizationError("object keys must be strings") from exc
        sink.write(b"{")
        for idx, (key, value) in enumerate(items):
            if idx:
                sink.write(b",")
            _serialize_str(key, sink)
            sink.write(b":")
            dump(value, sink)
        sink.write(b"}")
    else:
        raise CanonicalizationError(f"unsupported type: {type(obj)!r}")


def dumps(obj: Any) -> bytes:
    sink = BytesIO()
    dump(obj, sink)
    return sink.getvalue()


def dumps_text(obj: Any) -> str:
    """Return the JCS form as a Unicode string (UTF-8 code points)."""
    return dumps(obj).decode("utf-8")


__all__ = ["CanonicalizationError", "dump", "dumps", "dumps_text"]
