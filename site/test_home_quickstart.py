"""Exercise the homepage demo against the actual published proof files."""

import json
import re
import shlex
import socket
from pathlib import Path

from amw.cli import main

SITE = Path(__file__).resolve().parent


def test_homepage_demo_verifies_offline_and_rejects_tampering(
    monkeypatch, tmp_path, capsys
):
    html = (SITE / "index.html").read_text()
    block = re.search(
        r'<pre id="home-safe-demo"[^>]*><code>(.*?)</code>', html, re.DOTALL
    )
    assert block is not None
    commands = [shlex.split(line) for line in block[1].splitlines()]
    assert commands[0] == ["pip", "install", "amw-sdk[verify]"]
    assert commands[-1][0:2] == ["amw", "verify"]
    for command, name in zip(
        commands[1:3], ["receipt.json", "trust-keys.json"], strict=True
    ):
        assert command == [
            "curl",
            "-fsSLo",
            name,
            f"https://www.agentmiddleware.dev/proof/{name}",
        ]
        (tmp_path / name).write_bytes((SITE / "proof" / name).read_bytes())

    def no_network(*args, **kwargs):
        raise AssertionError("The demo verifier must not access the network")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AMW_API_KEY", raising=False)
    monkeypatch.delenv("AMW_WALLET_ID", raising=False)
    args = commands[-1][1:]
    assert main(args) == 0
    assert capsys.readouterr().out.startswith("VERIFIED  ")
    bundle = json.loads((tmp_path / "receipt.json").read_text())
    claims = json.loads(bundle["signing_input"])
    claims["tool"] += ".tampered"
    bundle["signing_input"] = json.dumps(claims, separators=(",", ":"), sort_keys=True)
    (tmp_path / "receipt.json").write_text(json.dumps(bundle))
    assert main(args) != 0


def test_homepage_install_is_visible_without_javascript():
    html = (SITE / "index.html").read_text()
    section = re.search(
        r'<section[^>]+id="pip-quickstart".*?</section>', html, re.DOTALL
    )[0]
    assert "pip install amw-sdk\n" in section
    assert 'python -c "import amw; print(amw.__version__)"' in section
    assert "hidden" not in section
    assert 'href="#safe-demo"' in html
