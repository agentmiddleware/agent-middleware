"""Focused tests for app.services.awi_playwright_bridge.

Covers the bridge's main public behavior without a real browser or
network: selector quoting, credential redaction, scroll clamping,
action translation errors, preview redaction, session guards and
execution results. The bridge is used in CDP_DIRECT mode (or with an
injected pageless session) so no Playwright browser is started.
"""

import json

import pytest

from app.services.awi_playwright_bridge import (
    _clamp_scroll,
    _is_sensitive_field_name,
    _redact_input_values,
    _selector_literal,
    AWIPlaywrightBridge,
    BridgeSession,
    DOMElement,
    TranslationMode,
)


@pytest.fixture
def bridge():
    return AWIPlaywrightBridge(mode=TranslationMode.CDP_DIRECT)


def _inject_session(bridge, session_id="test-session"):
    session = BridgeSession(
        session_id=session_id,
        current_url="https://example.com",
    )
    bridge._sessions[session_id] = session
    return session


def test_selector_literal_keeps_engine_chain_inside_one_literal():
    dangerous = 'x">> button'
    literal = _selector_literal(dangerous)
    assert literal == json.dumps(dangerous)
    assert literal.startswith('"') and literal.endswith('"')
    assert '\\"' in literal


def test_is_sensitive_field_name():
    assert _is_sensitive_field_name("password") is True
    assert _is_sensitive_field_name("user_password", "email") is True
    assert _is_sensitive_field_name("api_secret") is True
    assert _is_sensitive_field_name("email") is False
    assert _is_sensitive_field_name("username") is False
    assert _is_sensitive_field_name(None, "") is False


def test_redact_input_values_hides_credentials_only():
    inputs = [
        {"name": "email", "type": "email", "value": "a@example.com"},
        {"name": "password", "type": "password", "value": "s3cret"},
        {"name": "password", "type": "password", "value": ""},
    ]
    redacted = _redact_input_values(inputs)
    assert redacted[0]["value"] == "a@example.com"
    assert redacted[1]["value"] == "[REDACTED]"
    assert redacted[2]["value"] == ""
    assert inputs[1]["value"] == "s3cret"


def test_clamp_scroll_bounds():
    assert _clamp_scroll(300) == 300
    assert _clamp_scroll(-300) == -300
    assert _clamp_scroll(10**9) == 20000
    assert _clamp_scroll(-(10**9)) == -20000


async def test_translate_scroll_clamps_huge_amount(bridge):
    session = _inject_session(bridge)
    commands = await bridge.translate_action(
        session.session_id, "scroll", {"direction": "down", "amount": 10**9}
    )
    assert len(commands) == 1
    assert commands[0].target == "window.scrollBy(0, 20000)"


async def test_translate_scroll_rejects_non_integer_amount(bridge):
    session = _inject_session(bridge)
    with pytest.raises(ValueError, match="scroll amount must be an integer"):
        await bridge.translate_action(session.session_id, "scroll", {"amount": "a lot"})


async def test_translate_action_unknown_session(bridge):
    with pytest.raises(ValueError, match="not found"):
        await bridge.translate_action("no-such-session", "scroll", {})


async def test_translate_action_unsupported(bridge):
    session = _inject_session(bridge)
    with pytest.raises(ValueError, match="Unsupported AWI action"):
        await bridge.translate_action(session.session_id, "fly_to_moon", {})


async def test_preview_action_redacts_password(bridge, monkeypatch):
    session = _inject_session(bridge)

    def _element(css_selector):
        return DOMElement(
            tag="input",
            text_content="",
            attributes={},
            xpath="",
            css_selector=css_selector,
        )

    async def fake_find(_session, semantic_type):
        if semantic_type == "email_input":
            return _element("#email")
        if semantic_type == "password_input":
            return _element("#pw")
        if semantic_type == "login_button":
            return _element("#login")
        return None

    monkeypatch.setattr(bridge, "_find_semantic_element", fake_find)
    preview = await bridge.preview_action(
        session.session_id,
        "login",
        {"email": "a@example.com", "password": "s3cret"},
    )
    by_target = {c["target"]: c["value"] for c in preview["commands"]}
    assert by_target["#email"] == "a@example.com"
    assert by_target["#pw"] == "[REDACTED]"


async def test_create_session_blocks_disallowed_scheme(bridge):
    with pytest.raises(ValueError, match="scheme_not_allowed"):
        await bridge.create_session("file:///etc/passwd")
    assert await bridge.list_sessions() == []


async def test_execute_commands_unknown_session(bridge):
    result = await bridge.execute_commands("no-such-session", [])
    assert result.success is False
    assert result.commands_executed == 0
    assert "not found" in (result.error or "")


def test_dom_element_id_is_stable_and_short():
    kwargs = dict(
        tag="button",
        text_content="Add to cart",
        attributes={},
        xpath="",
        css_selector=".add-to-cart",
    )
    first = DOMElement(**kwargs).element_id
    assert first == DOMElement(**kwargs).element_id
    assert len(first) == 12
