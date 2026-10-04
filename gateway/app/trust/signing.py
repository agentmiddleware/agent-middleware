"""Trust-plane facade: the cryptographic root of the spine.

Ed25519 signing/verification, canonical JSON, and content hashing underpin
permits, receipts, and the audit chain. Re-exports the canonical implementation
from :mod:`app.services.signing_keys`.
"""

from __future__ import annotations

from app.services.signing_keys import (
    RECEIPT_CANONICALIZATION,
    SigningKeyError,
    SigningKeyService,
    canonical_json,
    get_signing_key_service,
    sha256_hex,
)

__all__ = [
    "RECEIPT_CANONICALIZATION",
    "SigningKeyError",
    "SigningKeyService",
    "canonical_json",
    "get_signing_key_service",
    "sha256_hex",
]
