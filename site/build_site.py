#!/usr/bin/env python3
"""Render the static marketing site only when launch contacts are real.

The source HTML deliberately contains non-deployable tokens. Vercel runs this
script and serves only ``dist/``; a missing or obviously provisional contact
therefore fails the build instead of leaking a fake funnel into production.

The Vercel Web Analytics loader is emitted only when
``PUBLIC_ENABLE_VERCEL_ANALYTICS=true``. Deploying the ``/_vercel/insights``
script tag against a project whose Web Analytics is not enabled makes every
page load log a 404 plus a MIME-type refusal in the browser console, so the
default build omits the tag entirely.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse


SITE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SITE_ROOT.parent
DEFAULT_OUTPUT = SITE_ROOT / "dist"
# Required launch contacts. Pilot intake is email-first by design: a prospect
# sends the tool scenario and gets a written reply, and a call happens only when
# a scenario needs one. So the accountable name and the monitored address gate
# the build; a booking link is optional and never a prerequisite.
CONTACT_FIELDS = {
    "@@PUBLIC_DISPLAY_NAME@@": "PUBLIC_DISPLAY_NAME",
    "@@PUBLIC_CONTACT_EMAIL@@": "PUBLIC_CONTACT_EMAIL",
}
BOOKING_TOKEN = "@@PUBLIC_BOOKING_URL@@"
BOOKING_CONTEXT_TOKEN = "@@BOOKING_CONTEXT@@"
PILOT_EMAIL_TOKEN = "@@PILOT_EMAIL_HREF@@"
PILOT_EMAIL_BODY = """One-tool paid pilot enquiry

1. Tool or action (synthetic or redacted only):
2. What goes wrong on retry:
3. How we check whether the action ran:
4. Cost of one duplicate or unproven call (money, recovery hours, or customer consequence; measured or estimated):
5. Budget owner and target decision date:

Optional workflow assumptions: monthly actions, baseline duplicate rate, expected reduction, and total monthly cost.

Do not include production secrets, credentials, or customer data.
"""
BOOKING_FIELD = "PUBLIC_BOOKING_URL"
# Markup that only makes sense with a booking link sits between these two
# comments. When no link is configured the whole block is removed, so the
# emitted page never carries an empty href or an unresolved token; when one is
# configured only the marker comments are removed.
BOOKING_BLOCK_START = "<!-- booking:start -->"
BOOKING_BLOCK_END = "<!-- booking:end -->"
# Marker lines may end in LF or CRLF, and the closing marker may end the
# file; the block is dropped or unwrapped identically either way.
_BOOKING_BLOCK = re.compile(
    rf"^[ \t]*{re.escape(BOOKING_BLOCK_START)}[ \t]*\r?\n.*?"
    rf"^[ \t]*{re.escape(BOOKING_BLOCK_END)}[ \t]*(?:\r?\n|\Z)",
    flags=re.DOTALL | re.MULTILINE,
)
_BOOKING_MARKER_LINE = re.compile(
    rf"^[ \t]*(?:{re.escape(BOOKING_BLOCK_START)}|{re.escape(BOOKING_BLOCK_END)})[ \t]*(?:\r?\n|\Z)",
    flags=re.MULTILINE,
)
ANALYTICS_FLAG = "PUBLIC_ENABLE_VERCEL_ANALYTICS"
ANALYTICS_TOKEN = "@@VERCEL_ANALYTICS_SCRIPTS@@"
ANALYTICS_FLAG_ENABLED = frozenset({"true"})
ANALYTICS_FLAG_DISABLED = frozenset({"", "false"})
# The queue shim lives in /va-init.js rather than an inline <script> so the
# deployed Content-Security-Policy can stay script-src 'self' with no
# 'unsafe-inline'. It must load before the insights script reads window.vaq.
ANALYTICS_SCRIPTS = """<script src="/va-init.js?v=gateway-19"></script>
    <script defer src="/_vercel/insights/script.js"></script>"""
# Shared chrome is rendered at build time so it also works without JavaScript.
SITE_PARTIALS = {
    "@@SITE_NAV@@": "partials/nav.html",
    "@@SITE_FOOTER@@": "partials/footer.html",
}
STYLES_CSS_VERSION_TOKEN = "@@STYLES_CSS_VERSION@@"
CONCEPT_CSS_VERSION_TOKEN = "@@CONCEPT_CSS_VERSION@@"
BUILD_DATE_TOKEN = "@@BUILD_DATE@@"
FAQ_JSONLD_TOKEN = "@@FAQ_JSONLD@@"
HERO_CONSOLE_TOKEN = "@@HERO_CONSOLE@@"
LOOP_TRANSCRIPT_TOKEN = "@@LOOP_TRANSCRIPT@@"
LIVE_VERIFICATION_TOKEN = "@@LIVE_VERIFICATION@@"
# The day the published sample receipt was issued, read from the receipt's own
# signed claims so the page can never drift from the artifact it describes.
PROOF_RECEIPT_ISSUED_TOKEN = "@@PROOF_RECEIPT_ISSUED@@"
TRANSCRIPT = SITE_ROOT / "proof" / "transcript.json"
LIVE_RECEIPT = SITE_ROOT / "proof" / "receipt.json"
LIVE_KEYS = SITE_ROOT / "proof" / "trust-keys.json"
#: The hero shows the loop's spine; the governed-path section shows all of it.
HERO_CONSOLE_STEPS = ("authorize", "invoke", "replay", "verify")
SECURITY_TXT_EXPIRES_TOKEN = "@@SECURITY_TXT_EXPIRES@@"
# security.txt must carry a future Expires (RFC 9116 §2.5.5). Regenerating it
# one year out on every build means a deployed site never serves a lapsed file.
SECURITY_TXT_LIFETIME_DAYS = 365
TEXT_ASSETS = (
    "index.html",
    "pilot/index.html",
    "proof/index.html",
    "compare/index.html",
    "concept/index.html",
    "demo-key/index.html",
    "try/index.html",
    "404.html",
    "sitemap.xml",
    ".well-known/security.txt",
    # The machine-pointer files cite the published receipt's issue date, read
    # from the bundle at build time like the HTML pages, so the two can never
    # disagree.
    "llm.txt",
    "llms.txt",
    "llms-full.txt",
)
COPY_ASSETS = (
    ".well-known",
    "a11y.js",
    "a11y-preload.js",
    "arcade-boot.js",
    "arcade.css",
    "arcade.js",
    "fonts",
    "fonts.css",
    "analytics.js",
    "home-key.js",
    "pilot-fit.js",
    "pilot",
    "compare",
    "concept",
    "demo-key",
    "try",
    "favicon.svg",
    "social-card.png",
    "proof",
    "robots.txt",
    "sitemap.xml",
    "styles.css",
    "utm-capture.js",
    "va-init.js",
    "wave.js",
)
REQUIRED_PUBLIC_ASSETS = (
    *SITE_PARTIALS.values(),
    *TEXT_ASSETS,
    *COPY_ASSETS,
    "demo-key/demo-key.js",
    "try/try.css",
    "try/try.js",
    "proof/receipt.json",
    "proof/trust-keys.json",
    "proof/transcript.json",
    # The standalone offline verifier ships with the page that documents it.
    # COPY_ASSETS already copies the whole proof/ directory; listing the file
    # here fails the build if it ever goes missing instead of publishing a
    # page whose download link 404s.
    "proof/verify_receipt.py",
)
PROVISIONAL_TERMS = (
    "change me",
    "changeme",
    "example.com",
    "example.org",
    "example.net",
    "fake",
    "placeholder",
    "provisional",
    "test@",
    "your email",
    "your name",
)
RESERVED_HOSTS = {"example", "invalid", "localhost", "test"}
RESERVED_HOST_SUFFIXES = tuple(f".{host}" for host in RESERVED_HOSTS)


class LaunchConfigurationError(ValueError):
    """Raised when a build would publish an unusable human funnel."""


def _reject_provisional(field: str, value: str) -> None:
    lowered = value.casefold()
    if any(term in lowered for term in PROVISIONAL_TERMS):
        raise LaunchConfigurationError(f"{field} contains a provisional value")
    if "@@" in value:
        raise LaunchConfigurationError(f"{field} contains an unresolved token")


def _is_reserved_hostname(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    return normalized in RESERVED_HOSTS or normalized.endswith(RESERVED_HOST_SUFFIXES)


def validated_contacts(
    environment: dict[str, str], *, escape_markup: bool = True
) -> dict[str, str]:
    """Return contact values or refuse to build the public site.

    ``escape_markup`` is on for HTML and XML targets. Plain-text targets such as
    ``.well-known/security.txt`` take the raw value, because an entity-escaped
    address there would be served verbatim to whoever reads the file.
    """

    missing = [
        name
        for name in CONTACT_FIELDS.values()
        if not environment.get(name, "").strip()
    ]
    if missing:
        raise LaunchConfigurationError(
            "missing required launch contact values: " + ", ".join(sorted(missing))
        )

    display_name = environment["PUBLIC_DISPLAY_NAME"].strip()
    email = environment["PUBLIC_CONTACT_EMAIL"].strip()
    booking_url = environment.get(BOOKING_FIELD, "").strip()

    for field, value in (
        ("PUBLIC_DISPLAY_NAME", display_name),
        ("PUBLIC_CONTACT_EMAIL", email),
        (BOOKING_FIELD, booking_url),
    ):
        if value:
            _reject_provisional(field, value)

    if (
        display_name.casefold() == "agent middleware api"
        or len(display_name) < 3
        or re.search(
            r"\b(?:example|fake|placeholder|provisional|test)\b",
            display_name,
            flags=re.IGNORECASE,
        )
    ):
        raise LaunchConfigurationError(
            "PUBLIC_DISPLAY_NAME must identify an accountable person or entity"
        )
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
        raise LaunchConfigurationError(
            "PUBLIC_CONTACT_EMAIL is not a valid email address"
        )
    if _is_reserved_hostname(email.rsplit("@", 1)[1]):
        raise LaunchConfigurationError(
            "PUBLIC_CONTACT_EMAIL must use a routable public domain"
        )

    # Optional: a configured booking link is validated as strictly as before,
    # and an absent one simply leaves the booking blocks out of the build.
    if booking_url:
        booking = urlparse(booking_url)
        if booking.scheme != "https" or not booking.hostname:
            raise LaunchConfigurationError(
                "PUBLIC_BOOKING_URL must be an absolute HTTPS URL"
            )
        if booking.username or booking.password:
            raise LaunchConfigurationError(
                "PUBLIC_BOOKING_URL must not contain credentials"
            )
        if _is_reserved_hostname(booking.hostname):
            raise LaunchConfigurationError(
                "PUBLIC_BOOKING_URL must use a routable public domain"
            )
        blocked_booking_hosts = {
            "api.thisisatest.tech",
            "thisisatest.tech",
            "www.thisisatest.tech",
            "agentmiddleware.dev",
            "www.agentmiddleware.dev",
        }
        if booking.hostname.casefold() in blocked_booking_hosts:
            raise LaunchConfigurationError(
                "PUBLIC_BOOKING_URL must point to a booking service"
            )

    if not escape_markup:
        values = {
            "@@PUBLIC_DISPLAY_NAME@@": display_name,
            "@@PUBLIC_CONTACT_EMAIL@@": email,
        }
        if booking_url:
            values[BOOKING_TOKEN] = booking_url
        return values
    escaped = {
        "@@PUBLIC_DISPLAY_NAME@@": html.escape(display_name),
        "@@PUBLIC_CONTACT_EMAIL@@": html.escape(email, quote=True),
    }
    if booking_url:
        escaped[BOOKING_TOKEN] = html.escape(booking_url, quote=True)
    return escaped


def booking_configured(environment: dict[str, str]) -> bool:
    """Whether the build carries the optional booking link."""

    return bool(environment.get(BOOKING_FIELD, "").strip())


def render_booking_blocks(markup: str, *, configured: bool) -> str:
    """Resolve the optional booking blocks in one page.

    With a booking link configured only the marker comments go; without one
    the whole block goes, so no page ever ships an empty ``href`` or a
    ``@@PUBLIC_BOOKING_URL@@`` token. Unbalanced markers are a launch error.
    """

    if markup.count(BOOKING_BLOCK_START) != markup.count(BOOKING_BLOCK_END):
        raise LaunchConfigurationError("booking block markers are unbalanced")
    if configured:
        return _BOOKING_MARKER_LINE.sub("", markup)
    rendered = _BOOKING_BLOCK.sub("", markup)
    if BOOKING_BLOCK_START in rendered or BOOKING_BLOCK_END in rendered:
        raise LaunchConfigurationError(
            "booking block markers must each sit on their own line"
        )
    return rendered


def vercel_analytics_enabled(environment: dict[str, str]) -> bool:
    """Return whether the build should emit the Vercel Web Analytics loader.

    A misspelled value fails the build instead of silently disabling analytics
    the operator meant to turn on.
    """

    raw = environment.get(ANALYTICS_FLAG, "").strip().casefold()
    if raw in ANALYTICS_FLAG_ENABLED:
        return True
    if raw in ANALYTICS_FLAG_DISABLED:
        return False
    raise LaunchConfigurationError(
        f'{ANALYTICS_FLAG} must be "true", "false", or unset'
    )


def _copy_asset(relative_path: str, output: Path) -> None:
    source = SITE_ROOT / relative_path
    destination = output / relative_path
    if source.is_dir():
        # Withhold the operator note only. A blanket "*.md" would also silently
        # drop Markdown from .well-known or proof if either ever gained one, and
        # it would strip fonts/OFL.txt's neighbours; the license itself is
        # plain text precisely so it stays published.
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("README.md"),
        )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


FONT_MANIFEST = SITE_ROOT / "fonts.manifest.json"
FONT_PRELOAD_TOKEN = "@@FONT_PRELOADS@@"
FONT_STYLESHEET = SITE_ROOT / "fonts.css"
FONTS_CSS_VERSION_TOKEN = "@@FONTS_CSS_VERSION@@"


def fonts_css_version() -> str:
    """Return a cache key derived from the generated stylesheet's content.

    fonts.css names content-hashed woff2 files and vendor_fonts.py deletes the
    ones it replaces. A hand-maintained token would let a visitor hold a cached
    stylesheet that points at files the deploy has already removed, so the
    stylesheet's own key has to move whenever its bytes do.
    """

    try:
        payload = FONT_STYLESHEET.read_bytes()
    except OSError as error:
        raise LaunchConfigurationError(
            f"fonts.css is missing; re-run vendor_fonts.py ({error})"
        ) from error
    return hashlib.sha256(payload).hexdigest()[:8]


def _require_declared_fonts() -> None:
    """Refuse to deploy a stylesheet whose @font-face files are not present.

    ``fonts`` appearing in REQUIRED_PUBLIC_ASSETS only proves the directory
    exists; an interrupted ``vendor_fonts.py`` run can leave it empty, and every
    src would then 404 on the live site with the build still reporting success.
    """

    declared = set(
        re.findall(
            r'url\("/fonts/([^"]+)"\)',
            (SITE_ROOT / "fonts.css").read_text(encoding="utf-8"),
        )
    )
    if not declared:
        raise LaunchConfigurationError("fonts.css declares no @font-face src")
    absent = sorted(
        name for name in declared if not (SITE_ROOT / "fonts" / name).is_file()
    )
    if absent:
        raise LaunchConfigurationError(
            "fonts.css references missing files (re-run vendor_fonts.py): "
            + ", ".join(absent)
        )


def font_preload_tags() -> str:
    """Render the <link rel="preload"> block from the generated manifest.

    Filenames carry a content hash, so hand-written preloads in the HTML would
    silently rot the moment a font is re-vendored. ``crossorigin`` is required
    even same-origin: without it the browser discards the preload and fetches
    the file a second time.
    """

    try:
        manifest = json.loads(FONT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LaunchConfigurationError(
            f"fonts.manifest.json is missing or malformed; re-run "
            f"vendor_fonts.py ({error})"
        ) from error
    # json.loads happily returns a list or a string; manifest.get would then
    # raise AttributeError and the build would die with a traceback rather than
    # the launch error the caller documents.
    if not isinstance(manifest, dict):
        raise LaunchConfigurationError(
            "fonts.manifest.json must be a JSON object; re-run vendor_fonts.py"
        )
    preload = manifest.get("preload") or []
    if not isinstance(preload, list) or not all(
        isinstance(name, str) for name in preload
    ):
        raise LaunchConfigurationError(
            "fonts.manifest.json preload must be a list of filenames"
        )
    if not preload:
        raise LaunchConfigurationError("fonts.manifest.json preloads nothing")
    # Filenames are content-hashed, so a stale entry names a file that no longer
    # exists and would ship a <link rel="preload"> that 404s.
    absent = sorted(
        name for name in preload if not (SITE_ROOT / "fonts" / name).is_file()
    )
    if absent:
        raise LaunchConfigurationError(
            "fonts.manifest.json preloads missing files (re-run "
            "vendor_fonts.py): " + ", ".join(absent)
        )
    return "\n".join(
        f'    <link rel="preload" href="/fonts/{name}" as="font"\n'
        f'      type="font/woff2" crossorigin />'
        for name in preload
    ).lstrip()


class _FaqListParser(HTMLParser):
    """Read the question/answer pairs out of a page's ``<dl class="faq-list">``.

    Google's FAQPage guidance is that the marked-up answer must be the answer
    the reader sees. Hand-maintained JSON-LD drifts from the prose the first
    time someone edits one and not the other, so the structured data is
    generated from the very markup it describes and cannot disagree with it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._in_list = False
        self._depth = 0
        self._collecting: str | None = None
        self._parts: list[str] = []
        self.questions: list[str] = []
        self.answers: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "dl" and "faq-list" in (attributes.get("class") or "").split():
            self._in_list = True
            self._depth = 0
            return
        if not self._in_list:
            return
        if tag == "dl":
            self._depth += 1
        elif tag in {"dt", "dd"} and self._depth == 0:
            self._collecting = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_list:
            return
        if tag == "dl":
            if self._depth == 0:
                self._in_list = False
            else:
                self._depth -= 1
            return
        if tag == self._collecting and self._depth == 0:
            # Concatenated, not space-joined: the source already carries the
            # whitespace around inline markup, so joining with spaces would
            # publish "terminal outcome , and" wherever an <em> meets a comma.
            text = " ".join("".join(self._parts).split())
            (self.questions if tag == "dt" else self.answers).append(text)
            self._collecting = None
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._collecting is not None:
            self._parts.append(data)


def load_transcript() -> dict:
    """Read the recorded governed-loop transcript and refuse anything off.

    The page renders this file verbatim, so a missing, malformed, or stale
    transcript is a launch error, not a blank panel. Stale means the live
    verifier output was recorded against a different receipt than the one
    published beside it: republishing ``receipt.json`` without re-running
    ``scripts/record_site_transcript.py`` fails here instead of shipping a
    verdict for the wrong artifact.
    """
    try:
        transcript = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LaunchConfigurationError(
            "site/proof/transcript.json is missing; run "
            "python scripts/record_site_transcript.py"
        ) from exc
    except json.JSONDecodeError as exc:
        raise LaunchConfigurationError(
            f"site/proof/transcript.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(transcript, dict):
        raise LaunchConfigurationError("site/proof/transcript.json must be an object")
    steps = transcript.get("steps")
    if not isinstance(steps, list) or not steps:
        raise LaunchConfigurationError("site/proof/transcript.json has no steps")
    for step in steps:
        if not isinstance(step, dict) or not all(
            isinstance(step.get(key), str) and step.get(key)
            for key in ("id", "loop", "title", "request", "note")
        ):
            raise LaunchConfigurationError(
                "every transcript step needs id, loop, title, request and note"
            )
        response = step.get("response")
        rows_ok = isinstance(response, list) and all(
            isinstance(row, list) and len(row) == 2 for row in response
        )
        if not (rows_ok or isinstance(step.get("output"), str)):
            raise LaunchConfigurationError(
                f"transcript step {step.get('id')!r} needs a response of "
                "[key, value] rows or an output string"
            )
    ids = [step["id"] for step in steps]
    for step in steps:
        # A verifier step is only evidence if it verified. The recorder
        # refuses to write a failed one; the build refuses to render it.
        if step["id"] == "verify" and not str(step.get("output", "")).startswith(
            "VERIFIED"
        ):
            raise LaunchConfigurationError(
                "the transcript's offline verify step did not verify; the site "
                "will not publish a failing verdict as proof"
            )
    missing = [name for name in HERO_CONSOLE_STEPS if name not in ids]
    if missing:
        raise LaunchConfigurationError(
            "transcript lacks the steps the hero shows: " + ", ".join(missing)
        )
    source = transcript.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("label"), str):
        raise LaunchConfigurationError("transcript must label its source")
    live = transcript.get("live_receipt_verification")
    if not isinstance(live, dict) or not all(
        isinstance(live.get(key), str) for key in ("receipt_id", "command", "output")
    ):
        raise LaunchConfigurationError(
            "transcript lacks the live receipt's verifier output"
        )
    try:
        bundle_bytes = LIVE_RECEIPT.read_bytes()
        bundle = json.loads(bundle_bytes.decode("utf-8"))
        published_id = json.loads(bundle["signing_input"])["receipt_id"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LaunchConfigurationError(
            f"site/proof/receipt.json is unreadable: {exc}"
        ) from exc
    try:
        keys_bytes = LIVE_KEYS.read_bytes()
    except OSError as exc:
        raise LaunchConfigurationError(
            f"site/proof/trust-keys.json is unreadable: {exc}"
        ) from exc
    if live.get("exit_code") != 0 or not live["output"].startswith("VERIFIED"):
        raise LaunchConfigurationError(
            "the live receipt did not verify when the transcript was recorded; "
            "the site will not publish a failing verdict as proof"
        )
    # The verdict line is the first line of verify_cli output, so the id
    # must appear there: matching it anywhere in the output would accept a
    # verdict for a different receipt that merely mentions this one.
    if live["receipt_id"] != published_id or not live["output"].startswith(
        f"VERIFIED  {published_id}"
    ):
        raise LaunchConfigurationError(
            "transcript's live verifier output is for "
            f"{live['output'].splitlines()[0]!r} (recorded as "
            f"{live['receipt_id']}), but receipt.json publishes {published_id}; "
            "re-run python scripts/record_site_transcript.py"
        )
    # Same id is not enough: the verdict must have been produced from the
    # exact bytes being published, or a re-signed or edited bundle under
    # the same receipt id would ship with a verdict it never earned.
    if live.get("bundle_sha256") != hashlib.sha256(bundle_bytes).hexdigest():
        raise LaunchConfigurationError(
            "transcript's live verifier output was recorded against different "
            "receipt.json bytes than the ones being published; re-run "
            "python scripts/record_site_transcript.py"
        )
    # The same holds for the key set the verifier checked the signature
    # against: the command beside the verdict names trust-keys.json, so the
    # published file must be the one that produced the verdict.
    if live.get("trust_document_sha256") != hashlib.sha256(keys_bytes).hexdigest():
        raise LaunchConfigurationError(
            "transcript's live verifier output was recorded against different "
            "trust-keys.json bytes than the ones being published; re-run "
            "python scripts/record_site_transcript.py"
        )
    # The latency line beside the loop is read from the recording like every
    # other number on the page. A transcript without the demo's timed run is
    # from an older recorder and must be re-recorded, not rendered blank.
    latency = transcript.get("latency")
    if not isinstance(latency, dict) or not (
        _is_number(latency.get("samples"))
        and latency["samples"] >= 1
        and all(
            _is_number(latency.get(key)) and latency[key] >= 0
            for key in ("p50_ms", "p95_ms")
        )
        and all(
            isinstance(latency.get(key), str) and latency[key]
            for key in ("path", "tool", "transport")
        )
    ):
        raise LaunchConfigurationError(
            "transcript lacks the demo's gateway latency sample (samples, p50_ms, "
            "p95_ms, path, tool, transport); re-run "
            "python scripts/record_site_transcript.py"
        )
    return transcript


def _is_number(value: object) -> bool:
    """True for an int or float that is not a bool (bool subclasses int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_latency_ms(value: float) -> str:
    """One decimal, the way the loop panel prints a millisecond figure.

    Kept as one function so the tests can format the recorded value the same
    way the page does instead of guessing at the rounding.
    """
    return f"{float(value):.1f}"


def render_latency_line(transcript: dict) -> str:
    """The demo's timed run, rendered beside the loop with its caveat."""
    latency = transcript["latency"]
    text = (
        "Gateway time per governed call in this run: "
        f"p50 {format_latency_ms(latency['p50_ms'])} ms, "
        f"p95 {format_latency_ms(latency['p95_ms'])} ms over "
        f"{latency['samples']} fresh calls ({latency['transport']}). "
        "Recorded on one machine, on the local governed-tool path with a "
        "stand-in echo tool and no upstream dispatch: a reference point, not "
        "a production number. The pilot records yours."
    )
    return f'<p class="console-latency">{html.escape(text)}</p>'


def published_receipt_issued_date() -> str:
    """Return the ISO date the published sample receipt was issued.

    Read from ``created_at`` inside the receipt's signed ``signing_input``, so
    the "historical sample" sentence on the pages names the date the bundle
    itself attests to. A receipt without a parseable issue date is a launch
    error: the page would otherwise have to guess.
    """
    try:
        bundle = json.loads(LIVE_RECEIPT.read_text(encoding="utf-8"))
        created_at = json.loads(bundle["signing_input"])["created_at"]
        issued = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LaunchConfigurationError(
            f"site/proof/receipt.json has no readable created_at: {exc}"
        ) from exc
    return issued.date().isoformat()


def _console_output(text: str) -> str:
    """A verifier's stdout as markup; the verdict word carries its own class."""
    lines = html.escape(text).split("\n")
    if lines and lines[0].startswith("VERIFIED"):
        lines[0] = (
            '<span class="console-ok">VERIFIED</span>' + lines[0][len("VERIFIED") :]
        )
    return "\n".join(lines)


def _console_request(text: str) -> str:
    """A request as a prompt line: the first line gets the ``$``."""
    lines = html.escape(text).split("\n")
    first = '<span class="console-prompt" aria-hidden="true">$ </span>' + lines[0]
    rest = ["  " + line for line in lines[1:]]
    return "\n".join([first, *rest])


def render_console(
    transcript: dict, *, steps: tuple[str, ...] | None, full: bool, labelled_by: str
) -> str:
    """Render recorded steps as a terminal panel.

    ``full`` adds the loop label and title above each step; the hero omits
    them and shows only the request, the response, and the note.
    """
    wanted = [
        step for step in transcript["steps"] if steps is None or step["id"] in steps
    ]
    if steps is not None:
        order = {name: index for index, name in enumerate(steps)}
        wanted.sort(key=lambda step: order[step["id"]])
    parts = [
        f'<figure class="console{" console-full" if full else ""}" aria-labelledby="{labelled_by}">'
    ]
    recorded = html.escape(str(transcript.get("recorded_at", ""))[:10])
    parts.append(
        '<figcaption class="console-bar">'
        f'<span class="console-title" id="{labelled_by}">governed loop · recorded</span>'
        f'<span class="console-meta">make prove-trust-plane · <time datetime="{recorded}">{recorded}</time></span>'
        "</figcaption>"
    )
    parts.append('<ol class="console-steps">')
    for step in wanted:
        parts.append(f'<li class="console-step" data-step="{html.escape(step["id"])}">')
        if full:
            parts.append(
                f'<p class="console-loop">{html.escape(step["loop"])}</p>'
                f'<h3 class="console-step-title">{html.escape(step["title"])}</h3>'
            )
        parts.append(
            f'<pre class="console-request"><code>{_console_request(step["request"])}</code></pre>'
        )
        if isinstance(step.get("output"), str):
            parts.append(
                f'<pre class="console-output"><code>{_console_output(step["output"])}</code></pre>'
            )
        else:
            rows = "".join(
                f"<div><dt>{html.escape(str(key))}</dt><dd>{html.escape(str(value))}</dd></div>"
                for key, value in step["response"]
            )
            parts.append(f'<dl class="console-response">{rows}</dl>')
        parts.append(f'<p class="console-note">{html.escape(step["note"])}</p>')
        parts.append("</li>")
    parts.append("</ol>")
    if full:
        parts.append(render_latency_line(transcript))
    parts.append(
        '<p class="console-foot">'
        + html.escape(transcript["source"]["label"])
        + ' <a href="/proof/transcript.json">Read the full recording.</a>'
        "</p>"
    )
    parts.append("</figure>")
    return "\n".join(parts)


def render_live_verification(transcript: dict) -> str:
    """The offline verifier's real output for the published receipt."""
    live = transcript["live_receipt_verification"]
    return (
        '<figure class="console console-live" aria-labelledby="live-verify-title">'
        '<figcaption class="console-bar">'
        '<span class="console-title" id="live-verify-title">offline verifier · published sample receipt</span>'
        '<span class="console-meta">b2a-verify-receipt</span>'
        "</figcaption>"
        f'<pre class="console-request"><code>{_console_request(live["command"])}</code></pre>'
        f'<pre class="console-output"><code>{_console_output(live["output"])}</code></pre>'
        "</figure>"
    )


def faq_jsonld(markup: str, *, indent: str = " " * 10) -> str:
    """Render the FAQPage node for a page from its own visible Q&A markup."""

    parser = _FaqListParser()
    parser.feed(markup)
    parser.close()
    questions, answers = parser.questions, parser.answers
    if not questions:
        raise LaunchConfigurationError(
            "a page requests FAQ structured data but publishes no "
            '<dl class="faq-list"> to build it from'
        )
    if len(questions) != len(answers):
        raise LaunchConfigurationError(
            f"FAQ markup is unbalanced: {len(questions)} questions, "
            f"{len(answers)} answers"
        )
    empty = [question for question, answer in zip(questions, answers) if not answer]
    if empty:
        raise LaunchConfigurationError(
            "FAQ answers must not be empty: " + "; ".join(empty)
        )
    node = {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in zip(questions, answers)
        ],
    }
    body = json.dumps(node, indent=2, ensure_ascii=False)
    # Re-indent so the node sits inside the surrounding @graph array rather
    # than flush against the left margin.
    first, *rest = body.split("\n")
    return "\n".join([first, *(indent + line for line in rest)])


def _validated_output_path(output: Path) -> Path:
    """Resolve a build target without authorizing deletion of existing data."""

    resolved = output.resolve()
    default_output = DEFAULT_OUTPUT.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()

    # Never allow the source tree, repository, temp root, or one of their
    # ancestors to become the recursive-delete target.
    if resolved == SITE_ROOT or resolved in SITE_ROOT.parents:
        raise LaunchConfigurationError("output must not contain the source tree")
    if resolved == default_output:
        return resolved
    if REPO_ROOT in resolved.parents:
        raise LaunchConfigurationError(
            "output must not be another path inside the repository"
        )

    # Tests and one-off previews may use a fresh temp path. Existing arbitrary
    # temp directories are refused because this function clears the target.
    if temp_root in resolved.parents and not resolved.exists():
        return resolved
    raise LaunchConfigurationError(
        "output must be site/dist or a new path under the system temp directory"
    )


def _build_timestamps() -> dict[str, str]:
    """Return the date tokens shared by the sitemap and security.txt."""

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SECURITY_TXT_LIFETIME_DAYS)
    return {
        BUILD_DATE_TOKEN: now.date().isoformat(),
        SECURITY_TXT_EXPIRES_TOKEN: expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def render_site(output: Path, environment: dict[str, str]) -> None:
    from build_docs import DocsBuildError, build_docs

    markup_replacements = validated_contacts(environment)
    text_replacements = validated_contacts(environment, escape_markup=False)
    with_booking = booking_configured(environment)
    name = text_replacements["@@PUBLIC_DISPLAY_NAME@@"]
    # One buyer-facing name. The note says who the call is with and which
    # product it concerns; the calendar host behind the link is plumbing and
    # is never named on the page.
    booking_context = f"Agent Middleware API calls are with {name}."
    markup_replacements[BOOKING_CONTEXT_TOKEN] = html.escape(
        booking_context, quote=True
    )
    pilot_email = (
        "mailto:"
        + quote(text_replacements["@@PUBLIC_CONTACT_EMAIL@@"], safe="@")
        + "?subject="
        + quote("One-tool paid pilot enquiry", safe="")
        + "&body="
        + quote(PILOT_EMAIL_BODY, safe="")
    )
    markup_replacements[PILOT_EMAIL_TOKEN] = html.escape(pilot_email, quote=True)
    timestamps = _build_timestamps()
    markup_replacements.update(timestamps)
    text_replacements.update(timestamps)
    analytics_enabled = vercel_analytics_enabled(environment)
    missing_assets = [
        relative_path
        for relative_path in REQUIRED_PUBLIC_ASSETS
        if not (SITE_ROOT / relative_path).exists()
    ]
    if missing_assets:
        raise LaunchConfigurationError(
            "missing required public assets: " + ", ".join(sorted(missing_assets))
        )
    _require_declared_fonts()
    partials = {
        token: (SITE_ROOT / path).read_text(encoding="utf-8").strip()
        for token, path in SITE_PARTIALS.items()
    }
    styles_version = hashlib.sha256(
        (SITE_ROOT / "styles.css").read_bytes()
    ).hexdigest()[:8]
    concept_version = hashlib.sha256(
        (SITE_ROOT / "concept/concept.css").read_bytes()
    ).hexdigest()[:8]
    try:
        generated_docs = build_docs()
    except (DocsBuildError, OSError, KeyError, ValueError) as exc:
        raise LaunchConfigurationError(f"docs build failed: {exc}") from exc
    output = _validated_output_path(output)

    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    for relative_path in COPY_ASSETS:
        _copy_asset(relative_path, output)

    for relative_path in (*TEXT_ASSETS, *generated_docs):
        rendered = (
            generated_docs[relative_path]
            if relative_path in generated_docs
            else (SITE_ROOT / relative_path).read_text(encoding="utf-8")
        )
        if relative_path.endswith(".html"):
            for token, partial in partials.items():
                rendered = rendered.replace(token, partial)
            # Dedicated markers survive formatting and added link attributes.
            for token, current_page in {
                "@@NAV_PILOT_CURRENT@@": "pilot/index.html",
                "@@NAV_PROOF_CURRENT@@": "proof/index.html",
                "@@NAV_COMPARE_CURRENT@@": "compare/index.html",
                "@@NAV_DEMO_KEY_CURRENT@@": "demo-key/index.html",
                "@@NAV_DOCS_CURRENT@@": "docs/",
            }.items():
                rendered = rendered.replace(
                    token,
                    ' aria-current="page"'
                    if (
                        relative_path.startswith(current_page)
                        if current_page == "docs/"
                        else relative_path == current_page
                    )
                    else "",
                )
            rendered = rendered.replace(STYLES_CSS_VERSION_TOKEN, styles_version)
            rendered = rendered.replace(CONCEPT_CSS_VERSION_TOKEN, concept_version)
        if analytics_enabled:
            rendered = rendered.replace(ANALYTICS_TOKEN, ANALYTICS_SCRIPTS)
        else:
            # Drop the token's whole line regardless of indentation so no
            # blank line or stray whitespace is left behind; the
            # unresolved-token check below still fails loudly if the token
            # ever stops sitting on its own line.
            rendered = re.sub(
                rf"^[ \t]*{re.escape(ANALYTICS_TOKEN)}[ \t]*\n",
                "",
                rendered,
                flags=re.MULTILINE,
            )
        if PROOF_RECEIPT_ISSUED_TOKEN in rendered:
            rendered = rendered.replace(
                PROOF_RECEIPT_ISSUED_TOKEN, published_receipt_issued_date()
            )
        if relative_path.endswith(".html"):
            rendered = render_booking_blocks(rendered, configured=with_booking)
            rendered = rendered.replace(FONT_PRELOAD_TOKEN, font_preload_tags())
            rendered = rendered.replace(FONTS_CSS_VERSION_TOKEN, fonts_css_version())
            if FAQ_JSONLD_TOKEN in rendered:
                rendered = rendered.replace(FAQ_JSONLD_TOKEN, faq_jsonld(rendered))
            if any(
                token in rendered
                for token in (
                    HERO_CONSOLE_TOKEN,
                    LOOP_TRANSCRIPT_TOKEN,
                    LIVE_VERIFICATION_TOKEN,
                )
            ):
                transcript = load_transcript()
                rendered = rendered.replace(
                    HERO_CONSOLE_TOKEN,
                    render_console(
                        transcript,
                        steps=HERO_CONSOLE_STEPS,
                        full=False,
                        labelled_by="hero-console-title",
                    ),
                )
                rendered = rendered.replace(
                    LOOP_TRANSCRIPT_TOKEN,
                    render_console(
                        transcript,
                        steps=None,
                        full=True,
                        labelled_by="loop-console-title",
                    ),
                )
                rendered = rendered.replace(
                    LIVE_VERIFICATION_TOKEN, render_live_verification(transcript)
                )
        replacements = (
            markup_replacements
            if relative_path.endswith((".html", ".xml"))
            else text_replacements
        )
        for token, replacement in replacements.items():
            rendered = rendered.replace(token, replacement)
        unresolved = sorted(set(re.findall(r"@@[A-Z0-9_]+@@", rendered)))
        if unresolved:
            raise LaunchConfigurationError(
                f"{relative_path} contains unresolved tokens: {', '.join(unresolved)}"
            )
        destination = output / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        render_site(args.output, dict(os.environ))
    except LaunchConfigurationError as exc:
        print(f"site build blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
