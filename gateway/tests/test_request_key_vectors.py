"""Independent gateway derivation of the public Python request key vectors."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from app.services.signing_keys import canonical_json


VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[2] / "spec/request-key-v1/vectors.json"
    ).read_text()
)


@pytest.mark.parametrize("case", VECTORS["vectors"], ids=lambda case: case["name"])
def test_request_key_vector_matches_gateway_canonical_json(case: dict) -> None:
    assert VECTORS["version"] == 1
    assert VECTORS["canonicalization"] == "amw-action-json-v1"
    payload = {
        "v": 1,
        "tool": case["tool"],
        "args": case["canonical_args"],
        "business_id": case["business_id"],
        "scope": case["scope"],
    }
    canonical = canonical_json(payload, ensure_ascii=False, allow_nan=False)
    assert canonical == case["canonical_json"]
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    key = "amwk1_" + base64.b32encode(digest).decode("ascii").rstrip("=")[:52]
    assert key == case["key"]
    assert len(key) == 58 <= 128


def test_request_key_vectors_pin_argument_order_independence() -> None:
    by_name = {case["name"]: case for case in VECTORS["vectors"]}
    assert by_name["refund"]["key"] == by_name["reordered_refund"]["key"]
