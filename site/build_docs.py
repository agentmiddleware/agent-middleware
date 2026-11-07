"""Build the small, source-controlled documentation site from Markdown."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


DOCS_SOURCE = Path(__file__).resolve().parent / "docs-src"
SITE_URL = "https://www.agentmiddleware.dev"
LICENSE_LINE = "Apache 2.0 (gateway is Fair Source, FSL-1.1)"
FRONT_MATTER_FIELDS = {"title", "section", "order", "description"}
INLINE = re.compile(r"`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
LIST_ITEM = re.compile(r"^(?:([-*])\s+|(\d+)\.\s+)(.+)$")
TABLE_RULE = re.compile(
    r"^\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)*\s*\|?$|^\|(?:\s*:?-{3,}:?\s*\|)+$"
)


class DocsBuildError(ValueError):
    """A documentation source cannot be rendered safely or consistently."""


def _safe_href(destination: str) -> str:
    destination = destination.strip()
    parsed = urlsplit(destination)
    if not destination or parsed.scheme not in {"", "http", "https", "mailto"}:
        raise DocsBuildError(f"unsafe Markdown link: {destination}")
    if parsed.scheme == "" and (
        destination.startswith("//") or not destination.startswith(("/", "#"))
    ):
        raise DocsBuildError(
            f"Markdown links must use an absolute site path or anchor: {destination}"
        )
    return html.escape(destination, quote=True)


def _inline(source: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in INLINE.finditer(source):
        parts.append(html.escape(source[cursor : match.start()]))
        code, label, destination = match.groups()
        if code is not None:
            parts.append(f"<code>{html.escape(code)}</code>")
        else:
            parts.append(
                f'<a href="{_safe_href(destination)}">{html.escape(label)}</a>'
            )
        cursor = match.end()
    parts.append(html.escape(source[cursor:]))
    return "".join(parts)


def _anchor_id(title: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "section"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}-{seen[base]}"


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def render_markdown(source: str) -> str:
    """Render only the Markdown forms used by the documentation sources."""

    lines = source.splitlines()
    result: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None
    seen_anchors: dict[str, int] = {}
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            result.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind is not None:
            result.append(f"</{list_kind}>")
            list_kind = None

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            close_list()
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            if index == len(lines):
                raise DocsBuildError("unclosed Markdown code fence")
            result.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
        elif not stripped:
            flush_paragraph()
            close_list()
        elif heading := HEADING.fullmatch(stripped):
            flush_paragraph()
            close_list()
            level = len(heading[1])
            title = heading[2]
            anchor = _anchor_id(title, seen_anchors)
            result.append(f'<h{level} id="{anchor}">{_inline(title)}</h{level}>')
        elif (
            index + 1 < len(lines)
            and "|" in stripped
            and TABLE_RULE.fullmatch(lines[index + 1].strip())
        ):
            flush_paragraph()
            close_list()
            headings = _table_cells(line)
            result.append("<table><thead><tr>")
            result.extend(f"<th>{_inline(cell)}</th>" for cell in headings)
            result.append("</tr></thead><tbody>")
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                cells = _table_cells(lines[index])
                if len(cells) != len(headings):
                    raise DocsBuildError("Markdown table row has wrong column count")
                result.append("<tr>")
                result.extend(f"<td>{_inline(cell)}</td>" for cell in cells)
                result.append("</tr>")
                index += 1
            result.append("</tbody></table>")
            continue
        elif item := LIST_ITEM.fullmatch(stripped):
            flush_paragraph()
            kind = "ul" if item[1] else "ol"
            if list_kind != kind:
                close_list()
                result.append(f"<{kind}>")
                list_kind = kind
            result.append(f"<li>{_inline(item[3])}</li>")
        else:
            close_list()
            paragraph.append(stripped)
        index += 1
    flush_paragraph()
    close_list()
    return "\n".join(result)


def _front_matter(source: str, filename: str) -> tuple[dict[str, str], str]:
    lines = source.splitlines()
    if not lines or lines[0] != "---":
        raise DocsBuildError(f"{filename} needs front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise DocsBuildError(f"{filename} has unclosed front matter") from exc
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator or key not in FRONT_MATTER_FIELDS or key in fields:
            raise DocsBuildError(f"{filename} has invalid front matter: {line}")
        fields[key] = value.strip()
    if fields.keys() != FRONT_MATTER_FIELDS or not all(fields.values()):
        raise DocsBuildError(f"{filename} needs title, section, order, description")
    return fields, "\n".join(lines[end + 1 :])


def _page_template(
    title: str, description: str, canonical: str, sidebar: str, body: str
) -> str:
    page_url = f"{SITE_URL}{canonical}"
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(title)} | Agent Middleware docs</title>
    <meta name="description" content="{html.escape(description, quote=True)}" />
    <meta name="robots" content="index, follow" />
    <meta name="theme-color" content="#ffffff" />
    <link rel="canonical" href="{page_url}" />
    <link rel="alternate" hreflang="en" href="{page_url}" />
    <link rel="alternate" hreflang="x-default" href="{page_url}" />
    <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
    <link rel="alternate" type="application/json" title="Agent manifest" href="/.well-known/agent.json" />
    <link rel="alternate" type="text/plain" title="Instructions for autonomous agents" href="/llms.txt" />
    <link rel="alternate" type="text/plain" title="Extended briefing for autonomous agents" href="/llms-full.txt" />
    <meta property="og:type" content="website" />
    <meta property="og:locale" content="en_US" />
    <meta property="og:site_name" content="Agent Middleware API" />
    <meta property="og:title" content="{html.escape(title, quote=True)} | Agent Middleware docs" />
    <meta property="og:description" content="{html.escape(description, quote=True)}" />
    <meta property="og:url" content="{page_url}" />
    <meta property="og:image" content="{SITE_URL}/social-card.png" />
    @@FONT_PRELOADS@@
    <link rel="stylesheet" href="/fonts.css?v=@@FONTS_CSS_VERSION@@" />
    <link rel="stylesheet" href="/styles.css?v=@@STYLES_CSS_VERSION@@" />
    <script src="/a11y-preload.js?v=gateway-19"></script>
    @@VERCEL_ANALYTICS_SCRIPTS@@
    <script defer src="/analytics.js?v=gateway-19"></script>
    <script defer src="/utm-capture.js?v=utm-1"></script>
    <script defer src="/a11y.js?v=gateway-19"></script>
  </head>
  <body class="site-light">
    <a class="skip-link" href="#main">Skip to content</a>
    @@SITE_NAV@@
    <main id="main" class="docs-layout">
      <aside class="docs-sidebar" aria-label="Documentation">
        <a class="docs-sidebar-home" href="/docs/">Documentation</a>
{sidebar}
      </aside>
      <article class="docs-content">
{body}
      </article>
    </main>
    @@SITE_FOOTER@@
  </body>
</html>
"""


def _sidebar(sections: list[dict], available: set[str], current: str | None) -> str:
    markup: list[str] = []
    for section in sections:
        markup.append(f"<section><h2>{html.escape(section['title'])}</h2><ul>")
        for page in section["pages"]:
            title = html.escape(page["title"])
            slug = page["slug"]
            if slug in available:
                active = ' aria-current="page"' if slug == current else ""
                markup.append(f'<li><a href="/docs/{slug}/"{active}>{title}</a></li>')
            else:
                markup.append(f"<li><span>{title}</span></li>")
        markup.append("</ul></section>")
    return "\n".join(markup)


def build_docs() -> dict[str, str]:
    """Return generated relative output paths and content for the site build."""

    manifest = json.loads((DOCS_SOURCE / "nav.json").read_text(encoding="utf-8"))
    sections = manifest["sections"]
    pages: list[tuple[str, dict[str, str], str]] = []
    slugs: set[str] = set()
    for section in sections:
        for order, entry in enumerate(section["pages"], 1):
            slug = entry["slug"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in slugs:
                raise DocsBuildError(f"invalid or duplicate docs slug: {slug}")
            slugs.add(slug)
            source_path = DOCS_SOURCE / f"{slug}.md"
            if not source_path.exists():
                continue
            fields, markdown = _front_matter(
                source_path.read_text(encoding="utf-8"), slug
            )
            if (
                fields["title"] != entry["title"]
                or fields["section"] != section["title"]
                or fields["order"] != str(order)
            ):
                raise DocsBuildError(f"{slug}.md does not match nav.json")
            pages.append((slug, fields, markdown))
    orphans = {path.stem for path in DOCS_SOURCE.glob("*.md")} - {
        slug for slug, _, _ in pages
    }
    if orphans:
        raise DocsBuildError(
            "Markdown pages missing from nav.json: " + ", ".join(sorted(orphans))
        )

    available = {slug for slug, _, _ in pages}
    generated: dict[str, str] = {}
    landing: list[str] = [
        "<h1>Documentation</h1>",
        "<p>Learn how Agent Middleware (AMW) governs a tool Call and records a Receipt.</p>",
    ]
    for section in sections:
        landing.append(f"<section><h2>{html.escape(section['title'])}</h2><ul>")
        for entry in section["pages"]:
            slug = entry["slug"]
            if slug in available:
                landing.append(
                    f'<li><a href="/docs/{slug}/">{html.escape(entry["title"])}</a></li>'
                )
            else:
                landing.append(f"<li><span>{html.escape(entry['title'])}</span></li>")
        landing.append("</ul></section>")
    landing.append(f'<p class="docs-license">{LICENSE_LINE}</p>')
    generated["docs/index.html"] = _page_template(
        "Documentation",
        "Agent Middleware documentation",
        "/docs/",
        _sidebar(sections, available, None),
        "\n".join(landing),
    )

    index_lines = ["# Agent Middleware documentation", ""]
    for slug, fields, markdown in pages:
        path = f"/docs/{slug}/"
        generated[f"docs/{slug}/index.html"] = _page_template(
            fields["title"],
            fields["description"],
            path,
            _sidebar(sections, available, slug),
            render_markdown(markdown),
        )
        index_lines.append(
            f"- [{fields['title']}]({SITE_URL}{path}): {fields['description']}"
        )
    generated["docs/llms.txt"] = "\n".join(index_lines) + "\n"
    return generated
