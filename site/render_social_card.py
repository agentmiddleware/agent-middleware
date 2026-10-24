#!/usr/bin/env python3
"""Render social-card.png from social-card.svg with the site's own fonts.

The Open Graph image is served as a PNG because link crawlers do not rasterize
SVG. A hand-exported PNG drifts: it silently keeps whatever palette and font
substitutions the exporting machine had. This script pins the pipeline —
Chromium rasterizes the committed SVG with the vendored woff2 faces from
``fonts/``, so the card's Press Start 2P headline and IBM Plex Mono labels
are literally the site's typography, not a viewer's fallbacks.

Stdlib only, like ``vendor_fonts.py``. Needs a Chromium binary:

    cd site
    python3 render_social_card.py            # writes social-card.png

The binary is found from ``$CHROMIUM``, then ``$PLAYWRIGHT_BROWSERS_PATH``
(the layout Playwright installs in CI containers — its ``headless_shell``
build is preferred because its viewport matches ``--window-size``), then
common executable names on ``PATH``. Rerun this whenever ``social-card.svg``
or the vendored fonts change, and commit the PNG alongside the SVG.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parent
SVG_SOURCE = SITE_ROOT / "social-card.svg"
PNG_TARGET = SITE_ROOT / "social-card.png"
FONT_STYLESHEET = SITE_ROOT / "fonts.css"
CARD_SIZE = (1200, 630)
# --ink from styles.css. The card's ground is opaque ink, so a rendered
# bottom row that never touches this color means the viewport fell short of
# the card (full-UI Chromium reserves toolbar height inside --window-size)
# and the screenshot was padded — the exact bug the probe exists to catch.
#
# The card's scanline overlay darkens every third row, starting at y=0. The
# card is 630 tall, so the bottom row is y=629 and 629 % 3 == 2: it falls
# between bands and stays pure ink. Keep that in mind if the card height or
# the 3px scanline period ever changes — the probe would start reading a
# dimmed ground and fail for the wrong reason.
INK_RGB = (0x0B, 0x10, 0x20)


class RenderError(RuntimeError):
    """Raised when the card cannot be rendered as specified."""


def find_chromium(explicit: str | None = None) -> str:
    """Locate a Chromium binary without downloading anything."""

    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("CHROMIUM"):
        candidates.append(os.environ["CHROMIUM"])
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        root = Path(browsers_path)
        candidates += sorted(
            str(path)
            for path in root.glob(
                "chromium_headless_shell-*/chrome-linux/headless_shell"
            )
        )
        candidates.append(str(root / "chromium"))
        candidates += sorted(
            str(path) for path in root.glob("chromium-*/chrome-linux/chrome")
        )
    candidates += ["chromium", "chromium-browser", "google-chrome", "chrome"]

    for candidate in candidates:
        # shutil.which already requires the execute bit; the direct-path
        # fallback must too, or a non-executable $CHROMIUM would surface
        # later as a PermissionError traceback instead of a launch error.
        resolved = shutil.which(candidate) or (
            candidate
            if Path(candidate).is_file() and os.access(candidate, os.X_OK)
            else None
        )
        if resolved:
            return resolved
    raise RenderError("no Chromium binary found; set $CHROMIUM or install chromium")


def local_font_css() -> str:
    """Rewrite the generated stylesheet's absolute /fonts/ URLs to file:// ones.

    fonts.css is written for the deployed origin. Under file:// those absolute
    paths resolve to the filesystem root, so every face would silently fall
    back and the card would rasterize with substitute fonts — the exact drift
    this script exists to prevent.
    """

    try:
        stylesheet = FONT_STYLESHEET.read_text(encoding="utf-8")
    except OSError as error:
        raise RenderError(
            f"fonts.css is missing; run vendor_fonts.py first ({error})"
        ) from error
    return stylesheet.replace('url("/fonts/', f'url("{(SITE_ROOT / "fonts").as_uri()}/')


def shim_document() -> str:
    """Wrap the SVG in a minimal page that loads the vendored fonts.

    An <img src="social-card.svg"> would not do: SVG-as-image renders in an
    isolated document that cannot reach the page's @font-face rules. Inlined
    into the DOM, the SVG's text nodes resolve against the loaded faces.
    """

    try:
        markup = SVG_SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        raise RenderError(f"social-card.svg is missing ({error})") from error
    width, height = CARD_SIZE
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">\n'
        f"<style>{local_font_css()}\n"
        "html, body { margin: 0; padding: 0; }\n"
        f"svg {{ display: block; width: {width}px; height: {height}px; }}\n"
        "</style></head>\n"
        f"<body>{markup}</body></html>\n"
    )


def _png_bottom_row(payload: bytes) -> list[tuple[int, int, int]]:
    """Decode a Chromium screenshot far enough to read its last pixel row.

    Full-UI Chromium's new headless mode reserves toolbar height inside
    ``--window-size``, renders a shorter viewport, and still emits a PNG of
    the requested size — leaving a blank band where the card's footer should
    be. Dimensions alone cannot catch that, so the ground color is probed.
    """

    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise RenderError("Chromium did not produce a PNG")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[16:26])
    if bit_depth != 8 or color_type not in (2, 6):
        # Not the truecolor output Chromium emits; skip the probe rather
        # than misread an exotic encoding.
        return []

    idat = bytearray()
    offset = 8
    while offset < len(payload):
        (length,), kind = (
            struct.unpack(">I", payload[offset : offset + 4]),
            payload[offset + 4 : offset + 8],
        )
        if kind == b"IDAT":
            idat += payload[offset + 8 : offset + 8 + length]
        offset += length + 12
    raw = zlib.decompress(bytes(idat))

    channels = 4 if color_type == 6 else 3
    stride = width * channels
    previous = bytearray(stride)
    row = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = raw[position]
        line = raw[position + 1 : position + 1 + stride]
        position += 1 + stride
        if filter_type == 0:
            row[:] = line
        elif filter_type == 1:
            row[:] = line
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                row[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up = previous[i]
                up_left = previous[i - channels] if i >= channels else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = up_left
                row[i] = (line[i] + predictor) & 0xFF
        else:
            raise RenderError(f"unsupported PNG filter type {filter_type}")
        previous[:] = row
    return [(row[i], row[i + 1], row[i + 2]) for i in range(0, stride, channels)]


def validate_card(payload: bytes) -> None:
    """Refuse a screenshot that is not the full card at exact size."""
    width, height = struct.unpack(">II", payload[16:24])
    if (width, height) != CARD_SIZE:
        raise RenderError(
            f"rendered {width}x{height}, expected {CARD_SIZE[0]}x{CARD_SIZE[1]}"
        )
    bottom = _png_bottom_row(payload)
    if bottom and INK_RGB not in bottom:
        raise RenderError(
            "the bottom row never shows the ink ground — the viewport fell "
            "short of the card. Use a headless_shell build (preferred "
            "automatically under $PLAYWRIGHT_BROWSERS_PATH) instead of a "
            "full-UI Chromium."
        )


def render(chromium: str, output: Path) -> None:
    """Screenshot the shim with ``chromium``, validate, and write the PNG."""
    width, height = CARD_SIZE
    with tempfile.TemporaryDirectory(prefix="social-card-") as workdir:
        shim = Path(workdir) / "card.html"
        shim.write_text(shim_document(), encoding="utf-8")
        screenshot = Path(workdir) / "card.png"
        command = [
            chromium,
            f"--screenshot={screenshot}",
            f"--window-size={width},{height}",
            "--force-device-scale-factor=1",
            "--hide-scrollbars",
            "--disable-gpu",
            # The shim and the fonts live in different directories; without
            # this, file:// pages may not read file:// subresources.
            "--allow-file-access-from-files",
            # Fast-forward virtual time so @font-face loads complete before
            # the screenshot rather than racing it.
            "--virtual-time-budget=10000",
            shim.as_uri(),
        ]
        if Path(chromium).name != "headless_shell":
            # headless_shell builds are headless by construction; a full
            # build needs the flag.
            command.insert(1, "--headless=new")
        if os.geteuid() == 0:
            # Root cannot use the user namespace sandbox (typical in CI
            # containers); rendering a local file needs no sandbox anyway.
            command.insert(1, "--no-sandbox")
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=120
            )
        except OSError as error:
            raise RenderError(f"Chromium could not start: {error}") from error
        if completed.returncode != 0 or not screenshot.is_file():
            raise RenderError(
                "Chromium screenshot failed: "
                + (completed.stderr.strip() or f"exit {completed.returncode}")
            )
        payload = screenshot.read_bytes()

    validate_card(payload)
    try:
        output.write_bytes(payload)
    except OSError as error:
        raise RenderError(f"could not write {output}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit 0 on success, 2 on any render failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", help="path to a Chromium/Chrome binary")
    parser.add_argument("--output", type=Path, default=PNG_TARGET)
    args = parser.parse_args(argv)
    try:
        chromium = find_chromium(args.chromium)
        render(chromium, args.output)
    except RenderError as exc:
        print(f"social card not rendered: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
