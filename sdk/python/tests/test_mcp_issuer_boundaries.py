"""Focused boundaries for the legacy MCP CLI and issuer key lookup."""

from __future__ import annotations

import httpx
import pytest

from b2a_sdk import mcp, verify_cli
from b2a_sdk.receipt_verifier import VerificationError


def test_issuer_key_fetch_uses_origin_without_following_redirects(monkeypatch):
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return httpx.Response(200, json={"keys": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert verify_cli._fetch_key_document("https://api.example.com/ignored", 5) == {"keys": []}
    assert seen["url"] == "https://api.example.com/.well-known/trust-keys.json"
    assert seen["kwargs"] == {"timeout": 5, "follow_redirects": False}


@pytest.mark.parametrize(
    "issuer",
    [
        "https://api.example.com@evil.example",
        "https://api.example.com?next=evil",
        "https://api.example.com#fragment",
        "ftp://api.example.com",
        "https://[::1",
        "https://api.example.com:not-a-port",
    ],
)
def test_invalid_issuer_never_fetches_keys(monkeypatch, issuer: str):
    def fake_get(*_args, **_kwargs):
        raise AssertionError("request was sent")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(VerificationError, match="issuer"):
        verify_cli._fetch_key_document(issuer, 5)


def test_manifest_category_is_encoded_and_redirects_are_off(monkeypatch):
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return httpx.Response(200, json={"tools": []}, request=httpx.Request("GET", url))

    assert mcp.httpx is not None
    monkeypatch.setattr(mcp.httpx, "get", fake_get)
    assert mcp.generate_manifest(api_url="https://api.example.com", category="a&b=c") == {
        "tools": []
    }
    assert seen["url"] == "https://api.example.com/mcp/tools.json?category=a%26b%3Dc"
    assert seen["kwargs"] == {"timeout": 30.0, "follow_redirects": False}


def test_list_tools_uses_real_line_breaks(monkeypatch, capsys):
    monkeypatch.setattr(
        mcp,
        "generate_manifest",
        lambda **_kwargs: {"tools": [{"name": "partner.search", "description": "Find partners"}]},
    )
    mcp.list_tools()
    output = capsys.readouterr().out
    assert "\\n" not in output
    assert "Available MCP Tools (1)" in output.splitlines()
    assert "  partner.search" in output.splitlines()
