"""Check the homepage quickstart without minting keys or calling the gateway."""

import json
import re
import shlex
import socket
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from amw.cli import main

SITE = Path(__file__).resolve().parent


def test_homepage_demo_key_fetch_origin_is_allowed_by_csp():
    script = (SITE / "home-key.js").read_text()
    fetch = re.search(r"""\bfetch\s*\(\s*(["'])(https?://[^"']+)\1""", script)
    assert fetch is not None
    url = urlsplit(fetch[2])
    origin = f"{url.scheme}://{url.netloc}"

    config = json.loads((SITE / "vercel.json").read_text())
    csp = next(
        header["value"]
        for rule in config["headers"]
        if rule["source"] == "/(.*)"
        for header in rule["headers"]
        if header["key"].lower() == "content-security-policy"
    )
    directives = [directive.split() for directive in csp.split(";")]
    connect_src = next(
        parts[1:] for parts in directives if parts and parts[0] == "connect-src"
    )
    assert origin in connect_src


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


def test_homepage_demo_key_quickstart_order_and_package():
    html = (SITE / "index.html").read_text()
    section = re.search(
        r'<section[^>]+id="pip-quickstart".*?</section>', html, re.DOTALL
    )[0]
    block = re.search(
        r'<pre id="home-hero-python"[^>]*><code>(.*?)</code>', section, re.DOTALL
    )[1]
    commands = unescape(block).splitlines()
    assert len(commands) == 3
    assert commands[0].startswith("AMW_DEMO_KEY=$(")
    assert commands[0].endswith(") &&")
    curl, extraction = commands[0][len("AMW_DEMO_KEY=$(") : -len(") &&")].split(
        " | ", 1
    )
    assert shlex.split(curl) == [
        "curl",
        "-X",
        "POST",
        "https://api.agentmiddleware.dev/v1/demo/keys",
        "-fsS",
    ]
    assert shlex.split(extraction) == [
        "jq",
        "-er",
        ".api_key | strings | select(length > 0)",
    ]
    assert commands[1] == 'export AMW_API_KEY="$AMW_DEMO_KEY" &&'
    assert shlex.split(commands[2]) == ["pip", "install", "amw-sdk"]
    assert not re.search(r"pip install (?:['\"])?agent-middleware\b", html)
    assert "python examples/" not in section
    assert "does not yet include a retry example" in section
    assert "hidden" not in section
    assert 'aria-label="Copy quickstart commands"' in section
    assert 'href="#safe-demo"' in html

    doc = (SITE.parent / "docs" / "three-minute-quickstart.md").read_text()
    assert unescape(block) in doc
    assert "does not yet include a retry example" in doc
    assert "python examples/" not in doc
    assert not re.search(r"pip install (?:['\"])?agent-middleware\b", doc)


def test_homepage_tabs_badges_and_evidence_links():
    html = (SITE / "index.html").read_text()
    hero = re.search(r'<header class="hero-v2".*?</header>', html, re.DOTALL)[0]
    tabs = re.findall(r'<a[^>]*role="tab"[^>]*>(.*?)</a>', hero)
    assert tabs == ["Quickstart (Demo Key)"]
    assert 'aria-selected="true" aria-controls="home-quickstart-panel"' in hero
    assert 'id="home-quickstart-panel" role="tabpanel"' in hero
    assert "Competition Demo" not in hero
    assert "<video" not in hero
    badges = re.search(r'<ul class="hero-v2-badges".*?</ul>', hero, re.DOTALL)[0]
    assert re.findall(r"<li>(.*?)</li>", badges) == [
        "Offline verifiable",
        "At most one debit per accepted idempotency key",
        "Demo key, no email",
    ]
    for url in (
        "https://agentmiddleware.dev/proof",
        "https://api.agentmiddleware.dev/health/dependencies",
        "https://api.agentmiddleware.dev/openapi.json",
    ):
        assert f'href="{url}"' in hero


def test_homepage_primary_cta_is_quickstart_not_clone():
    html = (SITE / "index.html").read_text()
    primary_actions = re.findall(
        r'<(?:a|button)[^>]*class="[^"]*hero-v2-action-primary[^>]*>.*?</(?:a|button)>',
        html,
        re.DOTALL,
    )
    assert len(primary_actions) == 1
    assert 'href="#pip-quickstart"' in primary_actions[0]
    assert "clone" not in primary_actions[0].lower()
    assert "git clone" not in html
    assert re.search(
        r'<a class="hero-v2-action hero-v2-action-secondary" '
        r'href="https://github.com/agentmiddleware/agent-middleware"[^>]*>Clone the source',
        html,
    )


def test_homepage_and_quickstart_avoid_unsupported_guarantee():
    for path in (
        SITE / "index.html",
        SITE.parent / "docs" / "three-minute-quickstart.md",
    ):
        copy = unescape(path.read_text())
        assert not re.search(r"exactly[\s\-‐-―]+once", copy, re.IGNORECASE)
