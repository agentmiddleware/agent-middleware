"""Published receipt vectors for the offline verifier.

The TypeScript SDK loads ``spec/test-vectors`` in its own tests. This module
does the same for Python, so the SDK CI job checks that corpus.
``verifier/`` has no ``pyproject.toml``, so project detection never enrolls
it and no workflow runs ``verifier/check_vectors.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

# sdk/python/tests/<this file> -> repository root is parents[3].
VECTORS = Path(__file__).resolve().parents[3] / "spec" / "test-vectors"


def _load_cases() -> list[dict]:
    path = VECTORS / "receipts.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"published receipt vectors not found at {path}; SDK tests must load spec/test-vectors"
        )
    cases = json.loads(path.read_text())["cases"]
    if not isinstance(cases, list) or not cases:
        raise AssertionError(f"{path} contains no cases")
    names = [case["name"] for case in cases]
    if len(names) != len(set(names)):
        raise AssertionError("receipt vector case names must be unique")
    return cases


_CASES = _load_cases()
_DEFAULT_KEYS = key_set_from_document(json.loads((VECTORS / "keys.json").read_text()))


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_published_receipt_vector(case):
    """Each committed vector must produce the status the corpus names."""
    pytest.importorskip("cryptography")
    keys = key_set_from_document(case["keys"]) if "keys" in case else _DEFAULT_KEYS
    status = verify_bundle(case["bundle"], keys).status.value
    assert status == case["expect"]
