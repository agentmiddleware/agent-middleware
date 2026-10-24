#!/usr/bin/env python3
"""Vendor the site's webfonts from Google Fonts into ``site/fonts/``.

Run this only to add a family/weight or to refresh the files; the output is
committed, so a normal build never touches the network.

    python3 vendor_fonts.py            # rewrite fonts/, fonts.css, the manifest
    python3 vendor_fonts.py --check    # fail if the committed output is stale

Exit codes: 0 clean, 1 the committed output drifted, 2 the tool could not reach
or parse upstream. ``--check`` has to be able to tell drift from a broken tool.

Rationale for self-hosting, the subset policy, and the variable-font naming rule
live in one place: the Typography section of ``site/README.md``.

Filenames carry a content hash, so ``vercel.json`` can serve them ``immutable``
and a refreshed font can never be served stale from a browser cache.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parent
FONT_DIR = SITE_ROOT / "fonts"
STYLESHEET = SITE_ROOT / "fonts.css"
#: Not published: build_site.py reads it to emit the <link rel="preload"> tags.
MANIFEST = SITE_ROOT / "fonts.manifest.json"

# A modern desktop UA: the css2 API serves woff2 only to browsers it recognises,
# and returns bulkier ttf to anything it does not.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# Must stay in step with the font stacks in styles.css. Declaration order is the
# order faces appear in the generated stylesheet.
FAMILIES = {
    "Press Start 2P": (400,),
    "IBM Plex Mono": (400, 500, 600),
    "Public Sans": (400, 500, 600),
    "Libre Franklin": (700, 800),
    "Instrument Serif": (400,),
}
WANTED_SUBSETS = ("latin", "latin-ext")
#: Font URLs are read out of a fetched CSS response and whatever they name gets
#: committed and deployed, so the host is pinned rather than trusted.
ALLOWED_FONT_HOSTS = frozenset({"fonts.gstatic.com"})
#: Faces rendered in the first viewport of every page. ``latin`` only:
#: latin-ext exists for characters most pages never show. Press Start 2P is
#: deliberately absent: it dresses only the footer's PRESS START control and
#: the arcade overlay, both far below the fold.
PRELOAD = (
    ("Instrument Serif", 400, "latin"),
    ("Libre Franklin", 800, "latin"),
    ("Public Sans", 400, "latin"),
    ("IBM Plex Mono", 400, "latin"),
    ("IBM Plex Mono", 500, "latin"),
    ("IBM Plex Mono", 600, "latin"),
)
HASH_LENGTH = 8

STYLESHEET_HEADER = """/* ==========================================================================
   Self-hosted webfonts — same origin, no third-party request.

   GENERATED FILE. Do not edit by hand: run `python3 vendor_fonts.py` in site/
   and commit the result. The test suite asserts that every src below resolves
   to a committed file and that no file is orphaned; `vendor_fonts.py --check`
   additionally re-fetches upstream.

   Why this is vendored, and why some faces share a src: see the Typography
   section of site/README.md.
   ========================================================================== */
"""


class VendorError(RuntimeError):
    """Raised when upstream fonts cannot be vendored reproducibly."""


def _ssl_context() -> ssl.SSLContext:
    """Trust the system store, plus any CA bundle the environment names.

    OpenSSL already honours ``SSL_CERT_FILE`` when the default context loads.
    ``REQUESTS_CA_BUNDLE``/``CURL_CA_BUNDLE`` are conventions it does not read,
    so they are layered on with ``load_verify_locations``. Passing ``cafile=``
    to ``create_default_context`` instead would *replace* the system roots with
    a single corporate cert and break every fetch to a normal public host.
    """

    context = ssl.create_default_context()
    for variable in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        bundle = os.environ.get(variable, "").strip()
        if bundle and Path(bundle).is_file():
            context.load_verify_locations(cafile=bundle)
    return context


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=60, context=_ssl_context()
        ) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as error:
        raise VendorError(f"could not fetch {url}: {error}") from error


def _extract(pattern: str, block: str, what: str) -> str:
    """Pull one field out of an @font-face block, or name the shape that changed."""

    match = re.search(pattern, block)
    if match is None:
        raise VendorError(
            f"upstream @font-face has no {what}; the css2 response shape "
            f"changed (is USER_AGENT still recognised?): {block[:120]!r}"
        )
    return match.group(1)


def _slug(family: str) -> str:
    return family.lower().replace(" ", "-")


def discover_faces() -> list[dict]:
    """Return one descriptor per (family, weight, subset) face."""

    faces: list[dict] = []
    for family, weights in FAMILIES.items():
        spec = f"{family.replace(' ', '+')}:wght@{';'.join(str(w) for w in weights)}"
        css = _get(
            f"https://fonts.googleapis.com/css2?family={spec}&display=swap"
        ).decode("utf-8")
        # The API labels every @font-face with its subset in a preceding comment.
        for subset, block in re.findall(
            r"/\*\s*([\w-]+)\s*\*/\s*(@font-face\s*\{.*?\})", css, re.S
        ):
            if subset not in WANTED_SUBSETS:
                continue
            faces.append(
                {
                    "family": family,
                    "weight": int(
                        _extract(r"font-weight:\s*(\d+)", block, "font-weight")
                    ),
                    "subset": subset,
                    "url": _extract(
                        r"url\((https://[^)]+\.woff2)\)", block, "woff2 src"
                    ),
                    "range": _extract(
                        r"unicode-range:\s*([^;]+);", block, "unicode-range"
                    ).strip(),
                }
            )

    # Subset has to be part of the completeness check: a silently dropped
    # latin-ext would otherwise pass here and then be deleted from the repo by
    # write(), taking the site's accented-character coverage with it.
    expected = {
        (family, weight, subset)
        for family, weights in FAMILIES.items()
        for weight in weights
        for subset in WANTED_SUBSETS
    }
    missing = expected - {
        (face["family"], face["weight"], face["subset"]) for face in faces
    }
    if missing:
        raise VendorError(f"upstream returned no face for: {sorted(missing)}")
    return faces


def download(faces: list[dict]) -> dict[str, bytes]:
    """Fetch each distinct URL once and name files by content hash.

    A variable family serves one file per subset for all its weights, so several
    faces legitimately share a URL; fetching per face would pull the same bytes
    repeatedly over separate TLS handshakes.
    """

    payloads: dict[str, bytes] = {}
    for face in faces:
        if face["url"] not in payloads:
            host = urllib.parse.urlsplit(face["url"]).hostname or ""
            if host.casefold() not in ALLOWED_FONT_HOSTS:
                raise VendorError(
                    f"refusing to vendor from unexpected host {host!r}: {face['url']}"
                )
            blob = _get(face["url"])
            if blob[:4] != b"wOF2":
                raise VendorError(f"{face['url']} is not woff2")
            payloads[face["url"]] = blob
        face["sha"] = hashlib.sha256(payloads[face["url"]]).hexdigest()

    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for face in faces:
        grouped[face["sha"]].append(face)

    files: dict[str, bytes] = {}
    for sha, group in grouped.items():
        # One file shared by several weights means a variable font; naming it
        # after a single weight would be a lie.
        stem = _slug(group[0]["family"])
        if len({face["weight"] for face in group}) == 1:
            stem = f"{stem}-{group[0]['weight']}"
        name = f"{stem}-{group[0]['subset']}.{sha[:HASH_LENGTH]}.woff2"
        for face in group:
            face["file"] = name
        files[name] = payloads[group[0]["url"]]
    return files


def render_stylesheet(faces: list[dict]) -> str:
    families = list(FAMILIES)
    ordered = sorted(
        faces,
        key=lambda face: (
            families.index(face["family"]),
            face["weight"],
            face["subset"],
        ),
    )
    blocks = [
        f"/* {face['subset']} */\n"
        "@font-face {\n"
        f'  font-family: "{face["family"]}";\n'
        "  font-style: normal;\n"
        f"  font-weight: {face['weight']};\n"
        "  font-display: swap;\n"
        f'  src: url("/fonts/{face["file"]}") format("woff2");\n'
        f"  unicode-range: {face['range']};\n"
        "}\n"
        for face in ordered
    ]
    return STYLESHEET_HEADER + "\n" + "\n".join(blocks)


def render_manifest(faces: list[dict]) -> str:
    """Emit the preload list that build_site.py turns into <link> tags."""

    by_key = {(f["family"], f["weight"], f["subset"]): f["file"] for f in faces}
    preload: list[str] = []
    for key in PRELOAD:
        if key not in by_key:
            raise VendorError(f"PRELOAD names a face that does not exist: {key}")
        if by_key[key] not in preload:  # variable families share one file
            preload.append(by_key[key])
    return json.dumps({"preload": preload}, indent=2) + "\n"


def vendor() -> tuple[dict[str, bytes], str, str]:
    """Fetch upstream and return the files, stylesheet and manifest to write."""

    faces = discover_faces()
    files = download(faces)
    # render_* read the "file" key that download() assigns, so order matters.
    return files, render_stylesheet(faces), render_manifest(faces)


def write(files: dict[str, bytes], stylesheet: str, manifest: str) -> None:
    """Replace the committed font output with a freshly vendored set."""

    FONT_DIR.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        (FONT_DIR / name).write_bytes(blob)
    # Delete only once every new file is on disk, so an interrupted run cannot
    # leave the tree with a stylesheet pointing at files that no longer exist.
    for stale in FONT_DIR.glob("*.woff2"):
        if stale.name not in files:
            stale.unlink()
    STYLESHEET.write_text(stylesheet, encoding="utf-8")
    MANIFEST.write_text(manifest, encoding="utf-8")


def check(files: dict[str, bytes], stylesheet: str, manifest: str) -> list[str]:
    """Return every way the committed output differs from upstream."""

    problems: list[str] = []
    on_disk = {path.name for path in FONT_DIR.glob("*.woff2")}
    for extra in sorted(on_disk - set(files)):
        problems.append(f"fonts/{extra} is committed but no longer referenced")
    for name, blob in files.items():
        path = FONT_DIR / name
        if not path.is_file():
            problems.append(f"fonts/{name} is missing")
        elif path.read_bytes() != blob:
            problems.append(f"fonts/{name} differs from upstream")
    for path, expected, label in (
        (STYLESHEET, stylesheet, "fonts.css"),
        (MANIFEST, manifest, "fonts.manifest.json"),
    ):
        if not path.is_file():
            problems.append(f"{label} is missing")
        elif path.read_text(encoding="utf-8") != expected:
            problems.append(f"{label} is stale; re-run vendor_fonts.py")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Run the CLI: 0 clean, 1 committed output drifted, 2 upstream unusable."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed output matches upstream instead of rewriting it",
    )
    args = parser.parse_args(argv)
    try:
        files, stylesheet, manifest = vendor()
    except VendorError as error:
        print(f"font vendoring failed: {error}", file=sys.stderr)
        return 2

    if args.check:
        problems = check(files, stylesheet, manifest)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0

    write(files, stylesheet, manifest)
    total = sum(len(blob) for blob in files.values())
    print(f"vendored {len(files)} files ({total:,} bytes) into {FONT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
