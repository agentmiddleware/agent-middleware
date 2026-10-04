#!/usr/bin/env python3
"""Fail when a comment or docstring names a symbol that no longer exists.

This gate exists because prose drifting from code is not cosmetic here. An
``AuditChainHeadModel`` docstring claimed concurrent audit writers serialized by
locking the row ``FOR UPDATE``; the service that owns the table does the
opposite (optimistic compare-and-set, chosen to avoid ``FOR UPDATE``). A
reviewer read the prose, trusted it, and wrote up a mechanism this system does
not implement. The same false claim had propagated to two other files before it
was caught.

No cheap check can catch that semantic contradiction. This one catches the
mechanically detectable half of the same failure: prose that names an
identifier the tree no longer defines, which is what a rename leaves behind.

The convention this relies on is the repo's own: real symbols are written in
double backticks inside comments and docstrings.

Usage:
  python scripts/check_doc_references.py          # exits non-zero on a dangling ref
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("app", "scripts", "migrations")

# A double-backticked name, dotted path, or call form appearing in prose.
BACKTICK_REF = re.compile(r"``([A-Za-z_][A-Za-z0-9_.]*)(?:\(\))?``")

# Names that legitimately appear in prose without being defined in this tree:
# builtins, common type names, and external identifiers we reference by name.
KNOWN_EXTERNAL = frozenset(
    {
        # Python builtins / typing vocabulary used descriptively in prose.
        "true",
        "false",
        "none",
        "null",
        "int",
        "str",
        "bool",
        "float",
        "dict",
        "list",
        "set",
        "bytes",
        "object",
        "Any",
        "Decimal",
        "datetime",
        "None",
        "True",
        "False",
        # HTTP header name, not a repo symbol.
        "Origin",
        # pytest/setuptools configuration key, not a repo symbol.
        "pythonpath",
        # MCP SDK class referenced when describing what the SDK transport does.
        "StreamableHTTPSessionManager",
        # Ethereum wallet RPC method named when describing the EIP-712 typed
        # data the x402 facilitator emits for the payer wallet to sign.
        "eth_signTypedData_v4",
        # FastAPI / Starlette routing classes named when explaining why HEAD
        # needs middleware support (app/middleware/head_method.py).
        "APIRoute",
        "Route",
        # Stdlib internals named when explaining interpreter-shutdown ordering.
        # Listed as full dotted names on purpose: allowlisting the bare tails
        # instead would exempt those common words everywhere in the tree.
        "threading._shutdown",
        "concurrent.futures.thread",
    }
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    return files


def _prose_spans(source: str) -> list[tuple[int, str]]:
    """Return (lineno, text) for every comment and docstring in ``source``."""
    spans: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                spans.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file we cannot tokenize is a problem for ruff/mypy, not for us.
        return spans

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return spans
    doc_nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, doc_nodes):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                spans.append((getattr(node, "lineno", 1), docstring))
    return spans


def _qualified_members(sources: dict[Path, str]) -> dict[str, set[str]]:
    """Map each class to the member names it actually defines.

    Lets a dotted reference be checked against its own owner instead of against
    every name in the tree.

    Classes only, deliberately. Module names are not reliable owners here: prose
    also writes ``table.column``, and this repo has table names that collide
    with module names (``permits``), so treating a module as an exhaustive
    owner would reject correct references to non-Python things.
    """
    members: dict[str, set[str]] = {}

    def _names_in_body(body: list[ast.stmt]) -> set[str]:
        found: set[str] = set()
        for statement in body:
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                found.add(statement.name)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        found.add(target.id)
            elif isinstance(statement, ast.AnnAssign) and isinstance(
                statement.target, ast.Name
            ):
                found.add(statement.target.id)
        return found

    for path, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                members.setdefault(node.name, set()).update(_names_in_body(node.body))
    return members


def _defined_names(sources: dict[Path, str]) -> set[str]:
    """Every identifier the tree defines or uses **as code**.

    Collected from the AST, never from raw text. Scanning text would let prose
    vouch for prose: an ordinary comment that happens to contain a word, or a
    renamed function whose old name survives in a docstring or string literal,
    would certify a reference that resolves to nothing. That would defeat the
    whole check, so definitions come from parsed syntax only.
    """
    names: set[str] = set()
    for path, source in sources.items():
        # The file exists whether or not it parses, so its name always resolves.
        names.add(path.stem)
        names.add(path.name)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # Unparseable source cannot vouch for any name. ruff/mypy own that
            # failure; this check simply contributes nothing from the file.
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.alias):
                names.add((node.asname or node.name).split(".")[0])
                names.add(node.name.split(".")[-1])
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.update(node.module.split("."))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Prose here also names things that are string *values* in code:
                # states (``expired``), error codes, env vars, table names.
                # Grounding those in real literals keeps them checkable without
                # letting free text vouch for a symbol.
                value = node.value.strip()
                if value and len(value) <= 128:
                    names.add(value)
    return names


def _reference_resolves(
    reference: str,
    defined: set[str],
    members: dict[str, set[str]] | None = None,
) -> bool:
    """True when ``reference`` names something the tree actually contains.

    The whole string is tried first, so dotted names that are real values or
    filenames resolve directly.

    For a remaining dotted reference, when the owner is a class this tree
    defines, the member must actually belong to **that** class — otherwise
    a reference to a deleted class's method would pass on any unrelated method
    of the same name. When the owner is not a known definition (an instance, a
    third-party object), both halves must still be defined somewhere, which is
    the most that can be checked without type inference.
    """
    if reference in defined:
        return True
    if "." not in reference:
        return False
    owner, _, member = reference.rpartition(".")
    owner_head = owner.split(".")[0]
    if members is not None and owner_head in members:
        return member in members[owner_head]
    return owner_head in defined and member in defined


def main(argv: list[str] | None = None) -> int:
    _ = argv  # no options; the check is all-or-nothing by design
    files = _python_files()
    sources = {
        path: path.read_text(encoding="utf-8", errors="replace") for path in files
    }
    defined = _defined_names(sources)
    members = _qualified_members(sources)

    dangling: list[tuple[str, int, str]] = []
    checked = 0
    for path, source in sources.items():
        for lineno, text in _prose_spans(source):
            for match in BACKTICK_REF.finditer(text):
                reference = match.group(1)
                tail = reference.split(".")[-1]
                if not tail or tail in KNOWN_EXTERNAL or reference in KNOWN_EXTERNAL:
                    continue
                checked += 1
                if not _reference_resolves(reference, defined, members):
                    rel = path.relative_to(ROOT)
                    dangling.append((str(rel), lineno, reference))

    if dangling:
        print(
            f"{len(dangling)} comment/docstring reference(s) name a symbol that "
            "does not exist in this tree:",
            file=sys.stderr,
        )
        for rel, lineno, reference in dangling:
            print(f"  {rel}:{lineno}  ``{reference}``", file=sys.stderr)
        print(
            "\nRename the reference to the current symbol, or add it to "
            "KNOWN_EXTERNAL in this script if it names something outside the "
            "repo.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Doc references OK: {checked} backticked reference(s) across "
        f"{len(files)} file(s) all resolve."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
