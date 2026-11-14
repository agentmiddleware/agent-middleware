"""Every documentation link the discovery manifest advertises must resolve.

Regression for #347: authentication.dev_self_provision.docs and the AWI
adoption guide pointed at paths this gateway answered with 404.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

ADMIN = {"X-API-Key": "test-key"}


def _doc_links(value: Any) -> Iterator[str]:
    """Relative markdown or /docs/ paths anywhere in a JSON document."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _doc_links(item)
    elif isinstance(value, list):
        for item in value:
            yield from _doc_links(item)
    elif isinstance(value, str) and value.startswith("/") and " " not in value:
        path = value.split("#", 1)[0]
        if path.endswith(".md") or path.startswith("/docs/"):
            yield path


@pytest.fixture
def posture(monkeypatch):
    def _apply(**env: str) -> None:
        for name, val in env.items():
            monkeypatch.setenv(name, val)
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()


async def _manifest_links(client: AsyncClient) -> set[str]:
    links: set[str] = set()
    manifest = await client.get("/.well-known/agent.json")
    assert manifest.status_code == 200
    links.update(_doc_links(manifest.json()))
    for path in ("/v1/discover", "/v1/discover/awi"):
        resp = await client.get(path, headers=ADMIN)
        if resp.status_code == 200:
            links.update(_doc_links(resp.json()))
    return links


@pytest.mark.anyio
@pytest.mark.parametrize("proof_surfaces", ["false", "true"])
async def test_every_advertised_doc_link_resolves(
    clean_database, posture, proof_surfaces
):
    posture(
        ENABLE_PROOF_SURFACES=proof_surfaces,
        ENABLE_DEV_KEY_SELF_PROVISION="true",
        ENVIRONMENT="development",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        links = await _manifest_links(client)
        assert "/docs/static-dev-api-keys.md" in links
        if proof_surfaces == "true":
            assert "/docs/awi-adoption-guide.md" in links
        broken = {}
        uncacheable = []
        for link in sorted(links):
            resp = await client.get(link)
            if resp.status_code != 200:
                broken[link] = resp.status_code
            elif "no-store" in resp.headers.get("cache-control", ""):
                uncacheable.append(link)
        assert broken == {}
        # Advertised docs are public discovery: agents may cache them.
        assert uncacheable == []


@pytest.mark.anyio
async def test_awi_guide_is_not_served_without_proof_surfaces(clean_database, posture):
    posture(ENABLE_PROOF_SURFACES="false")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/docs/awi-adoption-guide.md")
    assert resp.status_code == 404
