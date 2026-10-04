"""Generate static development/training API keys for local testing.

    python scripts/generate_static_dev_keys.py [--count 2]

Set the printed values as STATIC_DEV_API_KEYS (comma-separated) in your
local .env. These keys are deliberately static:

  - They are NOT part of the rotation runbook (docs/api-key-rotation.md).
    Nothing rotates, expires, or revokes them, so recorded trainings,
    notebooks, demo scripts, and local fixtures keep working indefinitely.
  - They only authenticate in local-compatible environments (local, dev,
    test, ci). A production-like deployment refuses to boot when
    STATIC_DEV_API_KEYS is set (app.core.trust_mode), and the auth path
    independently ignores them there, so a leaked dev key is worthless
    against production.
  - The amw_dev_ prefix keeps them greppable and visually distinct from
    rotated amw_live_ bootstrap keys. Entries without the prefix are
    ignored by the auth path, so a live key pasted into the wrong
    variable never authenticates.

Key material is generated fresh — never hand-write keys, and never promote
a dev key into VALID_API_KEYS. Full guidance: docs/static-dev-api-keys.md.
"""

from __future__ import annotations

import argparse
import secrets
import sys

KEY_PREFIX = "amw_dev_"
KEY_ENTROPY_BYTES = 32


def generate(count: int) -> int:
    for _ in range(count):
        print(f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_ENTROPY_BYTES)}")
    print(
        "\nSet these as STATIC_DEV_API_KEYS (comma-separated) in your local "
        ".env. They authenticate only in local-compatible environments and "
        "are exempt from rotation — see docs/static-dev-api-keys.md.",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2)
    args = parser.parse_args()
    return generate(args.count)


if __name__ == "__main__":
    sys.exit(main())
