"""Ed25519-signed receipts, one JSON line per tool call decision."""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import canonical_json, sha256_hex


def load_or_create_key(path: str | Path) -> Ed25519PrivateKey:
    p = Path(path)
    if p.exists():
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        assert isinstance(key, Ed25519PrivateKey)
        return key
    key = Ed25519PrivateKey.generate()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return key


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class ReceiptSigner:
    def __init__(self, key: Ed25519PrivateKey, log_path: str | Path):
        self._key = key
        self._log = Path(log_path)
        pub = _raw_public(key.public_key())
        self.public_key_b64 = base64.b64encode(pub).decode()
        self.kid = "amw-goose-" + sha256_hex(self.public_key_b64)[:16]

    def issue(
        self,
        *,
        tool: str,
        args_hash: str,
        decision: str,
        outcome: str,
        **extra: Any,
    ) -> dict[str, Any]:
        body = {
            "receipt_id": str(uuid.uuid4()),
            "tool": tool,
            "args_hash": args_hash,
            "decision": decision,
            "outcome": outcome,
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "alg": "Ed25519",
            "kid": self.kid,
            **{k: v for k, v in extra.items() if v is not None},
        }
        sig = self._key.sign(canonical_json(body).encode())
        receipt = {"body": body, "signature": base64.b64encode(sig).decode()}
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("a") as f:
            f.write(json.dumps(receipt) + "\n")
        return receipt


def verify(receipt: dict[str, Any], public_key_b64: str) -> bool:
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        pub.verify(
            base64.b64decode(receipt["signature"]),
            canonical_json(receipt["body"]).encode(),
        )
    except (InvalidSignature, ValueError, KeyError):
        return False
    return True
