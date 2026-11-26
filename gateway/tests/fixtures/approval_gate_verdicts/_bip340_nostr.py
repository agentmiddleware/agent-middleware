"""Verification-only subset of the pinned upstream BIP-340/NIP-01 helper.

Copyright (c) 2026 babyblueviper1. MIT license, see LICENSE.
Source and local changes are recorded in README.md. Test fixtures only.
"""

import hashlib
import json

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * pow(2 * y1, _P - 2, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return x3, y3


def _point_mul(point, k):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _lift_x(x: int):
    """The point on secp256k1 with the given x and even y, or None if x is not on the curve."""
    if x >= _P:
        return None
    c = (pow(x, 3, _P) + 7) % _P
    y = pow(c, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != c:
        return None
    return x, y if y % 2 == 0 else _P - y


def schnorr_verify(msg32: bytes, pubkey32: bytes, sig64: bytes) -> bool:
    """BIP-340 verify. msg = 32-byte message (here the event id), pubkey = 32-byte x-only, sig = 64 bytes."""
    if len(msg32) != 32 or len(pubkey32) != 32 or len(sig64) != 64:
        return False
    P = _lift_x(int.from_bytes(pubkey32, "big"))  # noqa: N806
    if P is None:
        return False
    r = int.from_bytes(sig64[0:32], "big")
    s = int.from_bytes(sig64[32:64], "big")
    if r >= _P or s >= _N:
        return False
    e = (
        int.from_bytes(
            _tagged_hash("BIP0340/challenge", sig64[0:32] + pubkey32 + msg32), "big"
        )
        % _N
    )
    R = _point_add(_point_mul(_G, s), _point_mul(P, _N - e))  # noqa: N806
    if R is None or R[1] % 2 != 0 or R[0] != r:
        return False
    return True


# NIP-01 event id
def nostr_event_id(event: dict) -> str:
    """Recompute the canonical NIP-01 event id from the signed fields."""
    serial = json.dumps(
        [
            0,
            str(event["pubkey"]).lower(),
            int(event["created_at"]),
            int(event["kind"]),
            event.get("tags", []) or [],
            str(event["content"]),
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()
