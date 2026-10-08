"""OpenAPI servers must never advertise a host the operator did not set.

An unset PUBLIC_URL used to fall back to a test domain labeled "Public API",
so generated clients pointed at a host nobody configured. The public entry
now appears only with an explicit PUBLIC_URL; local environments always keep
the localhost entry.
"""

from app.main import build_openapi_servers


def _urls(servers):
    return [entry["url"] for entry in servers]


def test_unset_public_url_advertises_no_test_domain_locally():
    servers = build_openapi_servers("", "local")
    assert "https://api.thisisatest.tech" not in _urls(servers)
    assert _urls(servers) == ["http://localhost:8000"]


def test_unset_public_url_advertises_no_servers_in_production():
    assert build_openapi_servers("", "production") == []


def test_explicit_public_url_is_advertised():
    servers = build_openapi_servers("https://gateway.example-buyer.com", "local")
    assert _urls(servers) == [
        "https://gateway.example-buyer.com",
        "http://localhost:8000",
    ]


def test_explicit_public_url_alone_in_production():
    servers = build_openapi_servers("https://gateway.example-buyer.com", "production")
    assert _urls(servers) == ["https://gateway.example-buyer.com"]
