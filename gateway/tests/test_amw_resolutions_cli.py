"""Operator CLI delegates reviewed JSON without exposing credentials."""

import importlib
import json
from pathlib import Path

import httpx


def test_cli_posts_request_file(monkeypatch, tmp_path, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    cli = importlib.import_module("amw_resolutions")
    from tests.test_operator_resolution import body

    payload = body()
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(cli, "_read_api_key", lambda: "test-admin")
    calls = []

    def handler(request):
        assert request.headers["x-api-key"] == "test-admin"
        assert request.url.path == "/v1/admin/receipts/receipt-argument/resolution"
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    monkeypatch.setattr(
        cli,
        "_client",
        lambda origin, key: httpx.Client(
            base_url=origin,
            headers={"X-API-Key": key},
            transport=httpx.MockTransport(handler),
        ),
    )
    assert cli.main(["receipt-argument", str(path)]) == 0
    assert calls == [payload]
    assert "test-admin" not in capsys.readouterr().out


def test_cli_refuses_unsafe_destination_before_reading_key(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    cli = importlib.import_module("amw_resolutions")

    def forbidden():
        raise AssertionError("key must not be read")

    monkeypatch.setattr(cli, "_read_api_key", forbidden)
    assert (
        cli.main(["receipt-argument", "request.json", "--base-url", "http://192.0.2.1"])
        == 1
    )
    assert "refusing" in capsys.readouterr().err
