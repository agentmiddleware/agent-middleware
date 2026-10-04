"""Guard against key-distribution reference drift.

The Hard Run Report (P1-B) found surfaces that pointed agents and humans at
``/.well-known/jwks.json`` for offline verification while that path returned
404. P1-B is resolved two ways: the surfaces below now name
``/.well-known/trust-keys.json``, and ``/.well-known/jwks.json`` is served as a
standard RFC 7517 JWK Set view of the same keys (``get_jwks_json`` in
``app/routers/well_known.py``; its contract is pinned by
``tests/test_receipt_portability.py::test_jwks_endpoint_needs_no_credential``).

``trust-keys.json`` stays the canonical anchor these surfaces must name: it
carries each key's status and its issuance/retirement timestamps, which a bare
JWK Set omits (``_published_key`` in the same router). Mentioning
``jwks.json`` alongside it is fine; dropping the canonical anchor is not.

This test pins the trust-plane surfaces that tell an outside party where to
fetch the signing keys. It intentionally does NOT scan the whole tree: the Hard
Run Report itself quotes the old 404 as its finding, which is correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_KEY_ENDPOINT = "/.well-known/trust-keys.json"
JWKS_KEY_ENDPOINT = "/.well-known/jwks.json"

# Surfaces that direct a verifier to the published key document. Each must name
# the canonical endpoint.
KEY_DISTRIBUTION_SOURCES = (
    "app/services/quotes.py",
    "docs/signed-quotes.md",
    "docs/agent-accountability.md",
    "../sdk/python/src/b2a_sdk/verify_cli.py",
)


@pytest.mark.parametrize("relative_path", KEY_DISTRIBUTION_SOURCES)
def test_source_points_at_the_served_key_endpoint(relative_path: str) -> None:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert CANONICAL_KEY_ENDPOINT in text, (
        f"{relative_path} should name the served key endpoint "
        f"{CANONICAL_KEY_ENDPOINT} so an outside party can verify offline."
    )


@pytest.mark.parametrize("path", [CANONICAL_KEY_ENDPOINT, JWKS_KEY_ENDPOINT])
def test_key_endpoints_are_mounted_for_get(path: str) -> None:
    """Both key documents must be routed, so neither reference can 404 again.

    The served responses themselves are covered in test_receipt_portability;
    this keeps the reference guard above honest about which paths exist.
    """
    from app.main import app

    from tests.conftest import iter_routes

    get_paths = {
        route.path
        for route in iter_routes(app.routes)
        if "GET" in (getattr(route, "methods", None) or ())
    }
    assert path in get_paths, f"{path} is not mounted as a GET route"
