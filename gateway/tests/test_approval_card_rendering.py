"""Approval document styling must preserve the complete, escaped decision."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from html import escape
from html.parser import HTMLParser

import pytest

from app.services.approval_card import (
    ApprovalCardView,
    render_email_html,
    render_page_html,
    render_text_summary,
)


class _Document(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


@pytest.fixture
def view() -> ApprovalCardView:
    return ApprovalCardView(
        request_id="request-123",
        subject_wallet_id="agent-wallet-123",
        issuer_wallet_id="issuer-wallet-456",
        allowed_tools=["partner.notes.write"],
        scopes=["notes:write"],
        max_credits=Decimal("20"),
        permit_expires_at=datetime(2026, 9, 11, 12),
        justification="Write the approved note.",
        requested_at=datetime(2026, 9, 10, 12),
        decide_by=datetime(2026, 9, 10, 13),
        status="pending",
        approval_url="https://sentinel.example/decide?request=123&token=opaque",
    )


@pytest.mark.parametrize("render", [render_email_html, render_page_html])
def test_font_stacks_remain_inside_the_inline_style_attribute(view, render):
    document = _Document(render(view))
    styled = [(tag, attrs) for tag, attrs in document.elements if "style" in attrs]
    assert styled
    for _, attrs in styled:
        # Unescaped quotes in font-family used to turn font names and the
        # remaining declarations into stray HTML attributes.
        assert set(attrs) <= {
            "style",
            "href",
            "role",
            "cellpadding",
            "cellspacing",
            "border",
        }
        assert attrs["style"].endswith(";")

    heading = next(attrs["style"] for tag, attrs in styled if tag == "h1")
    body = next(attrs["style"] for tag, attrs in styled if tag == "blockquote")
    value = next(attrs["style"] for tag, attrs in styled if tag == "td")
    assert "Georgia" in heading and "serif;" in heading
    assert "Arial" in body and "sans-serif;" in body
    assert "Consolas" in value and "monospace;" in value


@pytest.mark.parametrize("render", [render_email_html, render_page_html])
@pytest.mark.parametrize("status", ["pending", "approved", "rejected", "expired"])
def test_hostile_terms_stay_text_and_only_pending_cards_link_to_decision(
    view, render, status
):
    hostile = '<img src=x onerror="alert(1)"> & "quoted"'
    url = 'https://sentinel.example/decide?request=123&token=opaque#" onclick="alert(1)'
    card = replace(
        view,
        request_id=hostile,
        subject_wallet_id=hostile,
        issuer_wallet_id=hostile,
        allowed_tools=[hostile],
        scopes=[hostile],
        justification=hostile,
        status=status,
        decided_by=hostile,
        permit_id=hostile,
        reason=hostile,
        extra_notes=[hostile],
        approval_url=url,
    )
    source = render(card)
    document = _Document(source)
    assert escape(hostile) in source
    assert not any(tag in {"img", "script"} for tag, _ in document.elements)
    assert not any(
        name.startswith("on") for _, attrs in document.elements for name in attrs
    )
    links = [attrs["href"] for tag, attrs in document.elements if tag == "a"]
    assert links == ([url] if status == "pending" else [])


_UNSAFE_APPROVAL_URLS = [
    "javascript:alert(1)",
    " JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "jav&#x09;ascript:alert(1)",
    "\x00javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "http://sentinel.example/decide?request=123",
    "//sentinel.example/decide?request=123",
    "https:alert(1)",
    "https://[::1",
    "/v1/permit-requests/request-123/card",
]


@pytest.mark.parametrize("render", [render_email_html, render_page_html])
@pytest.mark.parametrize("url", _UNSAFE_APPROVAL_URLS)
def test_non_https_approval_url_is_never_rendered_as_a_link(view, render, url):
    # Escaping keeps a hostile URL inside the href attribute, but a
    # javascript:/data: scheme is still executable once clicked, and the
    # emailed card has no CSP to fall back on. Only an absolute https link
    # becomes the action; anything else drops to the no-link instructions.
    source = render(replace(view, approval_url=url))
    document = _Document(source)
    assert not [attrs for tag, attrs in document.elements if tag == "a"]
    assert "Review &amp; decide" not in source
    assert "Approve or reject from the Sentinel" in source


@pytest.mark.parametrize("url", _UNSAFE_APPROVAL_URLS)
def test_non_https_approval_url_is_left_out_of_the_text_summary(view, url):
    text = render_text_summary(replace(view, approval_url=url))
    assert url.strip() not in text
    assert "Review & decide" not in text


def test_https_approval_url_is_the_text_summary_action(view):
    text = render_text_summary(view)
    assert f"Review & decide: {view.approval_url}" in text
