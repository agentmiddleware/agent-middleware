"""Chromium proof that redirected child documents keep their own origin."""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import awi_playwright_bridge
from app.services.awi_playwright_bridge import AWIPlaywrightBridge, BridgeSession


@pytest.fixture
def redirect_servers():
    state = {
        "redirect_case": "allowed",
        "iframe_url": "",
        "popup_url": "",
        "hits": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"].append((self.server.server_port, self.path))
            redirect_paths = {
                "/entry": {
                    "allowed": "/target",
                    "blocked": "/blocked",
                    "blocked_later": "/hop",
                }[state["redirect_case"]],
                "/hop": "/blocked",
            }
            if self.path in redirect_paths:
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{servers[1].server_port}"
                    f"{redirect_paths[self.path]}",
                )
                body = b""
            else:
                self.send_response(200)
                if self.path == "/parent-iframe":
                    body = f'<iframe src="{state["iframe_url"]}"></iframe>'.encode()
                elif self.path == "/parent-popup":
                    body = (
                        f'<button id="open" onclick="window.child = window.open('
                        f"'{state['popup_url']}')\">open</button>"
                    ).encode()
                else:
                    body = f"<p>{self.path}</p>".encode()
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in servers
    ]
    for thread in threads:
        thread.start()
    try:
        yield state, servers
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("child_kind", "oopif"),
    [("iframe", False), ("iframe", True), ("popup", False)],
)
@pytest.mark.parametrize("redirect_case", ["allowed", "blocked", "blocked_later"])
async def test_redirected_child_document_origin(
    redirect_servers, monkeypatch, child_kind, oopif, redirect_case
):
    playwright_api = pytest.importorskip("playwright.async_api")
    state, (source, target) = redirect_servers
    parent_url = f"http://localhost:{source.server_port}/parent-{child_kind}"
    entry_host = "127.0.0.1" if oopif else "localhost"
    entry_url = f"http://{entry_host}:{source.server_port}/entry"
    bootstrap_url = f"http://127.0.0.1:{source.server_port}/bootstrap"
    target_url = f"http://127.0.0.1:{target.server_port}/target"
    blocked_url = f"http://127.0.0.1:{target.server_port}/blocked"
    hop_url = f"http://127.0.0.1:{target.server_port}/hop"
    state["iframe_url"] = bootstrap_url if oopif else entry_url
    state["popup_url"] = entry_url
    state["redirect_case"] = redirect_case
    original_guard = awi_playwright_bridge.browser_request_block_reason
    checked_urls = []

    async def guard(url):
        checked_urls.append(url)
        if url in {parent_url, bootstrap_url, entry_url, hop_url, target_url}:
            return None
        return await original_guard(url)

    monkeypatch.setattr(awi_playwright_bridge, "browser_request_block_reason", guard)

    async with playwright_api.async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Chromium is not installed")
        browser = await playwright.chromium.launch(
            headless=True, args=["--site-per-process"] if oopif else []
        )
        try:
            bridge = AWIPlaywrightBridge()
            bridge._browser = browser
            bridge._playwright_initialized = True
            session = BridgeSession(session_id=str(uuid4()))
            await bridge._create_browser_context(session, None, None)
            page = session._page
            failed = asyncio.Event()
            session._context.on(
                "requestfailed",
                lambda request: (
                    failed.set() if request.url in {entry_url, blocked_url} else None
                ),
            )

            await page.goto(parent_url, wait_until="domcontentloaded", timeout=5000)
            if child_kind == "popup":
                async with page.expect_popup() as popup_info:
                    await page.locator("#open").click()
                child = await popup_info.value
            else:
                child = page.frames[1]

            if oopif:
                await child.wait_for_url(bootstrap_url, timeout=5000)
                cdp = await browser.new_browser_cdp_session()
                targets = await cdp.send("Target.getTargets")
                assert any(
                    target_info["type"] == "iframe"
                    for target_info in targets["targetInfos"]
                ), "the cross-site child must run as an OOPIF"
                await page.evaluate(
                    "url => document.querySelector('iframe').src = url", entry_url
                )

            if redirect_case != "allowed":
                await asyncio.wait_for(failed.wait(), timeout=5)
                assert (target.server_port, "/blocked") not in state["hits"]
                assert blocked_url in checked_urls
                if redirect_case == "blocked_later":
                    assert hop_url in checked_urls
                assert child.url != blocked_url
            else:
                await child.wait_for_url(target_url, timeout=5000)
                assert child.url == target_url
                assert await child.evaluate("location.origin") == (
                    f"http://127.0.0.1:{target.server_port}"
                )
                assert await child.locator("body").inner_text() == "/target"
                assert (
                    await page.evaluate(
                        "() => { try { return window."
                        + ("child" if child_kind == "popup" else "frames[0]")
                        + ".document.body.innerText } catch (e) { return e.name } }"
                    )
                    == "SecurityError"
                )
                assert (target.server_port, "/target") in state["hits"]
            assert state["hits"].count((source.server_port, "/entry")) == 1
        finally:
            await browser.close()
