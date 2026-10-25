"""Run the Python offline verifier against the spec test vectors.

Exit 0 when every case produces its expected status. Needs only the SDK and
`cryptography`; no network, database, or gateway.

    pip install -e "./sdk/python[verify]"
    python verifier/check_vectors.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from b2a_sdk.receipt_verifier import (
    key_set_from_document,
    verify_bundle,
    verify_permit_bundle,
)

VECTORS = Path(__file__).resolve().parent.parent / "spec" / "test-vectors"

SUITES = (
    ("receipts.json", verify_bundle),
    ("rotation.json", verify_bundle),
    ("permits.json", verify_permit_bundle),
)


def main() -> int:
    default_keys = key_set_from_document(
        json.loads((VECTORS / "keys.json").read_text())
    )
    failures = 0
    total = 0
    for filename, verify in SUITES:
        cases = json.loads((VECTORS / filename).read_text())["cases"]
        for case in cases:
            keys = (
                key_set_from_document(case["keys"]) if "keys" in case else default_keys
            )
            status = verify(case["bundle"], keys).status.value
            ok = status == case["expect"]
            failures += not ok
            total += 1
            print(
                f"{'ok  ' if ok else 'FAIL'} {filename}:{case['name']}: {status} (expected {case['expect']})"
            )
    print(f"{total - failures}/{total} vectors passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
