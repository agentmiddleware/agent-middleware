#!/usr/bin/env python3
"""Submit an operator resolution from a reviewed JSON request file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx

from amw_approvals import (
    BASE_URL_ENV,
    DEFAULT_BASE_URL,
    _client,
    _read_api_key,
    _refusal_reason,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt_id")
    parser.add_argument("request_file", type=Path)
    parser.add_argument(
        "--base-url", default=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)
    if _refusal_reason(args.base_url):
        print("error: refusing to send the admin key to an unsafe URL", file=sys.stderr)
        return 1
    try:
        key = _read_api_key()
        if not key:
            raise ValueError("missing admin key")
        payload = json.loads(args.request_file.read_text())
        with _client(args.base_url, key) as client:
            response = client.post(
                f"/v1/admin/receipts/{quote(args.receipt_id, safe='')}/resolution",
                json=payload,
            )
        if response.is_error:
            print(f"resolution failed: HTTP {response.status_code}", file=sys.stderr)
            return 1
        print(json.dumps(response.json(), indent=2))
        return 0
    except (OSError, ValueError, httpx.HTTPError):
        print(
            "resolution request failed; check configuration and retry the same file",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
