"""Redact invariant-attack evidence before publishing it as a CI artifact."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_SENSITIVE_FIELDS = {
    "api_key",
    "a_key",
    "b_key",
    "key_prefix",
    "sponsor_wallet_id",
    "victim_wallet",
    "wallet",
    "wallet_id",
}
_CREDENTIAL_RE = re.compile(r"(?<![A-Za-z0-9])(?:b2a|amw)_[A-Za-z0-9_-]{12,}")
_WALLET_RE = re.compile(r"(?<![A-Za-z0-9])(?:agt|spn)-[A-Za-z0-9_-]{6,}")


def redact_evidence(value: Any) -> Any:
    """Recursively remove credential material and tenant wallet identifiers."""

    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if (
                normalized in _SENSITIVE_FIELDS
                or normalized.endswith("_api_key")
                or normalized.endswith("_wallet")
                or normalized.endswith("_wallet_id")
            ):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_evidence(item)
        return redacted
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, str):
        without_credentials = _CREDENTIAL_RE.sub("<redacted-credential>", value)
        return _WALLET_RE.sub("<redacted-wallet>", without_credentials)
    return value


def contains_full_credential(value: Any) -> bool:
    """Return True if any serialized evidence value still contains a credential."""

    return bool(_CREDENTIAL_RE.search(json.dumps(value, sort_keys=True)))


def contains_wallet_identifier(value: Any) -> bool:
    """Return True if a tenant wallet identifier remains in published evidence."""

    return bool(_WALLET_RE.search(json.dumps(value, sort_keys=True)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    redacted = redact_evidence(source)
    if contains_full_credential(redacted) or contains_wallet_identifier(redacted):
        raise SystemExit("redaction failed: credential or wallet identifier remains")
    args.output.write_text(
        json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
