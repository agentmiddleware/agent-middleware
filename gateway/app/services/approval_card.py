"""The approver's view of a permit request — one card, two surfaces.

An agent asking for a 20-credit, 4-tool permit is a decision a human makes in
a few seconds, from whatever surface reached them first. Both surfaces render
from :func:`card_fragment_html`, so the email and the hosted page cannot drift
into showing different terms for the same decision:

    render_email_html()  -> table-shelled, inline-styled body for a mail client
    render_page_html()   -> full document served at the request's card URL

What the card shows is exactly what the middleware will mint if approved (the
frozen terms on the ``permit_requests`` row), never what the polling agent
sends later. The decision itself is not taken here — approve/reject lives in
Sentinel, whose magic link is rendered as the primary action when the create
response carried an https one. Without a link the card is still the full
disclosure of what is pending, which is what makes the Sentinel prompt legible.

Styling matches the site's receipt aesthetic (see ``site/styles.css``) with
literal values rather than CSS variables, because mail clients drop
``:root`` and custom properties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html import escape
from urllib.parse import urlsplit

# site/styles.css tokens, resolved. Mail clients need literals, not var():
# email shell/blockquote = --paper-shade, hosted shell = --ink,
# card face = --paper, text = --paper-ink,
# muted = --paper-dim, accents = the on-paper brass link tone and the
# .button-primary brass/ink pair, alert = --danger-on-paper (the plain
# --danger terracotta falls below 4.5:1 on the paper card face).
_PAPER = "#e8e3d2"
_PAPER_LIGHT = "#f6f3e9"
_SHELL = "#0b1020"
_INK = "#121629"
_INK_MUTED = "#4c526b"
_INK_FAINT = "#4c526b"
_LINE = "rgba(18, 22, 41, 0.17)"
_BRASS_DARK = "#8a6414"
_BUTTON_BRASS = "#f0b43c"
_BUTTON_INK = "#0b1020"
_SIGNAL = "#b0281a"
# Preserve the site's type roles with mail-client-safe fallbacks. Single
# quotes keep each font stack inside its double-quoted HTML style attribute.
_MONO = "'IBM Plex Mono', 'SFMono-Regular', Consolas, monospace"
_BODY = "'Public Sans', 'Segoe UI', Arial, sans-serif"
_DISPLAY = "'Instrument Serif', Georgia, 'Times New Roman', serif"


@dataclass(frozen=True)
class ApprovalCardView:
    """Everything the approver needs, and nothing the agent controls after."""

    request_id: str
    subject_wallet_id: str
    issuer_wallet_id: str
    allowed_tools: list[str]
    scopes: list[str]
    max_credits: Decimal
    permit_expires_at: datetime
    justification: str
    requested_at: datetime
    decide_by: datetime
    status: str
    requires_human_approval: bool = False
    simulated: bool = False
    approval_url: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    permit_id: str | None = None
    reason: str | None = None
    extra_notes: list[str] = field(default_factory=list)


def safe_approval_url(url: str | None) -> str | None:
    """Return ``url`` only when it is an absolute https link, else None.

    Escaping keeps a URL inside its ``href`` attribute, but a
    ``javascript:`` or ``data:`` scheme still runs when clicked, and the
    emailed card has no CSP behind it. Control characters anywhere and
    surrounding whitespace are refused outright because browsers strip
    them before reading the scheme (tabs and newlines even mid-scheme), so
    the scheme checked here is the one a client would follow.
    """
    if not isinstance(url, str) or not url or url != url.strip():
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.netloc:
        return None
    return url


def _stamp(value: datetime) -> str:
    """Render a naive-UTC column value as an unambiguous UTC timestamp."""
    return value.strftime("%Y-%m-%d %H:%M:%SZ")


def _row(label: str, value: str, *, mono: bool = True) -> str:
    """Render one label/value row of the card's terms table."""
    value_font = _MONO if mono else _BODY
    return (
        f'<tr><td style="width:36%;padding:6px 0;vertical-align:top;font-family:{_MONO};'
        f"font-size:11px;letter-spacing:0.08em;text-transform:uppercase;"
        f'color:{_INK_FAINT};">{escape(label)}</td>'
        f'<td style="padding:6px 0 6px 20px;vertical-align:top;'
        f"font-family:{value_font};font-size:13px;color:{_INK};"
        f'word-break:break-word;">{value}</td></tr>'
    )


def _banner(text: str, *, tone: str) -> str:
    """Render a status strip above the terms; ``alert`` gets the danger tone."""
    color = _SIGNAL if tone == "alert" else _INK_MUTED
    return (
        f'<p style="margin:0 0 16px;padding:8px 12px;border:1px solid {color};'
        "border-radius:6px;"
        f"font-family:{_MONO};font-size:11px;letter-spacing:0.06em;"
        f'text-transform:uppercase;color:{color};">{escape(text)}</p>'
    )


def card_fragment_html(view: ApprovalCardView) -> str:
    """The card itself: identical markup in the email and on the page."""
    tools = (
        "".join(
            f'<div style="padding:2px 0;">{escape(tool)}</div>'
            for tool in view.allowed_tools
        )
        or '<div style="padding:2px 0;">(none)</div>'
    )
    scopes = ", ".join(escape(scope) for scope in view.scopes) or "(none)"

    banners = ""
    if view.simulated:
        banners += _banner(
            "Simulated approval — not valid for production authority", tone="alert"
        )
    if view.status != "pending":
        decided = f" by {view.decided_by}" if view.decided_by else ""
        banners += _banner(f"Already {view.status}{decided}", tone="muted")
    for note in view.extra_notes:
        banners += _banner(note, tone="muted")

    rows = [
        _row("Agent wallet", escape(view.subject_wallet_id)),
        _row("Issuer wallet", escape(view.issuer_wallet_id)),
        _row(
            "Budget",
            f'<strong style="font-size:18px;">{escape(str(view.max_credits))}'
            f"</strong> credits",
        ),
        _row(
            f"Tools ({len(view.allowed_tools)})",
            f'<div style="font-family:{_MONO};">{tools}</div>',
        ),
        _row("Scopes", scopes),
        _row("Permit expires", _stamp(view.permit_expires_at)),
        _row(
            "Per-call approval",
            "required" if view.requires_human_approval else "not required",
        ),
        _row("Requested", _stamp(view.requested_at)),
        _row("Decide by", _stamp(view.decide_by)),
        _row("Request id", escape(view.request_id)),
    ]
    if view.permit_id:
        rows.append(_row("Permit minted", escape(view.permit_id)))
    if view.reason:
        rows.append(_row("Reason", escape(view.reason), mono=False))

    action = ""
    approval_url = safe_approval_url(view.approval_url)
    if approval_url and view.status == "pending":
        action = (
            f'<p style="margin:20px 0 0;"><a href="{escape(approval_url, quote=True)}" '
            f'style="display:inline-block;padding:12px 20px;background:{_BUTTON_BRASS};'
            "border-radius:6px;"
            f"color:{_BUTTON_INK};text-decoration:none;font-family:{_BODY};"
            'font-size:15px;font-weight:600;line-height:1.6;">'
            "Review &amp; decide</a></p>"
            f'<p style="margin:10px 0 0;font-family:{_BODY};font-size:12px;'
            f'color:{_INK_MUTED};">Approving mints exactly these terms. '
            "Rejecting mints nothing.</p>"
        )
    elif view.status == "pending":
        action = (
            f'<p style="margin:20px 0 0;font-family:{_BODY};font-size:12px;'
            f'color:{_INK_MUTED};">Approve or reject from the Sentinel '
            "notification sent to you. Approving mints exactly these terms.</p>"
        )

    return (
        f'<div style="box-sizing:border-box;width:100%;max-width:616px;'
        f"margin:0 auto;padding:28px;font-family:{_BODY};line-height:1.65;"
        f'background:{_PAPER_LIGHT};border:1px solid {_LINE};border-radius:10px;">'
        f'<p style="margin:0 0 4px;font-family:{_MONO};font-size:11px;'
        f'letter-spacing:0.18em;text-transform:uppercase;color:{_BRASS_DARK};">'
        "Permit request</p>"
        f'<h1 style="margin:0 0 20px;font-family:{_DISPLAY};font-size:32px;'
        f"font-weight:400;line-height:1.1;letter-spacing:-0.012em;"
        f'color:{_INK};">An agent is asking for authority</h1>'
        f"{banners}"
        f'<blockquote style="margin:0 0 20px;padding:12px 16px;'
        f"border-left:3px solid {_BRASS_DARK};border-radius:6px;"
        f"background:{_PAPER};font-family:{_BODY};"
        f'font-size:14px;color:{_INK};">{escape(view.justification)}</blockquote>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;table-layout:fixed;">'
        f"{''.join(rows)}"
        "</table>"
        f"{action}"
        "</div>"
    )


def render_email_html(view: ApprovalCardView) -> str:
    """Mail-client-safe shell around the shared card."""
    return (
        '<!doctype html><html><body style="margin:0;padding:24px 12px;'
        f'background:{_PAPER};">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;"><tr><td align="center">'
        f"{card_fragment_html(view)}"
        f'<p style="margin:18px 0 0;font-family:{_MONO};font-size:11px;'
        f'color:{_INK_FAINT};">Agent Middleware API — trust plane</p>'
        "</td></tr></table></body></html>"
    )


def render_page_html(view: ApprovalCardView) -> str:
    """Full document served at the request's card URL."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>Permit request {escape(view.request_id)}</title>"
        f"<style>body{{margin:0;padding:32px 16px;background:{_SHELL};}}"
        f"a:focus-visible{{outline:2px solid {_BRASS_DARK};outline-offset:3px;}}"
        "</style>"
        "</head><body>"
        f"{card_fragment_html(view)}"
        "</body></html>"
    )


def render_text_summary(view: ApprovalCardView) -> str:
    """Plain-text fallback carrying the same terms as the card."""
    lines = [
        "Permit request — an agent is asking for authority",
        "",
        view.justification,
        "",
        f"Agent wallet:    {view.subject_wallet_id}",
        f"Issuer wallet:   {view.issuer_wallet_id}",
        f"Budget:          {view.max_credits} credits",
        f"Tools:           {', '.join(view.allowed_tools) or '(none)'}",
        f"Scopes:          {', '.join(view.scopes) or '(none)'}",
        f"Permit expires:  {_stamp(view.permit_expires_at)}",
        f"Decide by:       {_stamp(view.decide_by)}",
        f"Request id:      {view.request_id}",
    ]
    if view.simulated:
        lines.insert(1, "[SIMULATED — not valid for production authority]")
    approval_url = safe_approval_url(view.approval_url)
    if approval_url:
        lines += ["", f"Review & decide: {approval_url}"]
    return "\n".join(lines)
