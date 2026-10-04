"""Run the Python offline verifier against spec/test-vectors/receipts.json.

Exit 0 when every case produces its expected status. Needs only the SDK and
`cryptography`; no network, database, or gateway.

    pip install -e "./sdk/python[verify]"
    python verifier/check_vectors.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

VECTORS = Path(__file__).resolve().parent.parent / "spec" / "test-vectors"


def main() -> int:
    default_keys = key_set_from_document(json.loads((VECTORS / "keys.json").read_text()))
    cases = json.loads((VECTORS / "receipts.json").read_text())["cases"]
    failures = 0
    for case in cases:
        keys = key_set_from_document(case["keys"]) if "keys" in case else default_keys
        status = verify_bundle(case["bundle"], keys).status.value
        ok = status == case["expect"]
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {case['name']}: {status} (expected {case['expect']})")
    print(f"{len(cases) - failures}/{len(cases)} vectors passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
