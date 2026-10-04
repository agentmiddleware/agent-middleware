"""Architectural guards for the trust-plane boundary.

Two invariants keep the product core honest:

1. The trust package must depend only inward — on shared infrastructure and the
   spine service modules — never on protocol routers or example workloads. If
   the core grew a dependency on, say, the oracle or AWI, it would no longer be
   a self-contained product. There is exactly one documented exception, listed
   in ``TRUST_PACKAGE_LAZY_EXCEPTIONS``: the MCP adapter in
   ``app/trust/adapters.py`` reaches ``app.routers.mcp._execute_registered_tool``
   lazily, at call time, because the governed pipeline itself still lives in
   the MCP router. The trust package is a facade over that pipeline, not a
   self-contained copy of it.
2. The core trust routers must consume the spine through the `app.trust` facade,
   not by importing the underlying services directly — otherwise the facade is
   decorative.

These are enforced by static import analysis so they fail fast on regression.
Every import in a file is inspected — module scope, function bodies, class
bodies and conditional blocks alike — because a lazy import inside a function
crosses the boundary just as surely as one at the top of the module.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRUST_DIR = ROOT / "app" / "trust"

# Infrastructure the spine may depend on.
ALLOWED_PACKAGE_PREFIXES = (
    "app.core",
    "app.db",
    "app.schemas",
    "app.policy",
    "app.trust",
)

# The spine service modules the facade is allowed to wrap.
SPINE_SERVICE_MODULES = {
    "app.services.agent_money",
    "app.services.audit_chain",
    "app.services.audit_log",
    "app.services.governance",
    "app.services.human_approval",
    "app.services.idempotency",
    "app.services.permit_requests",
    # The approver-facing card is part of the permit-request spine: it is the
    # rendering of a stored request, shared by the notification email and the
    # hosted page so the two cannot show different terms.
    "app.services.approval_card",
    "app.services.permits",
    "app.services.quotes",
    # One definition of a tool's price, shared by the quote endpoint and the
    # governed invoke that honors the quote.
    "app.services.pricing",
    "app.services.policies",
    "app.services.receipts",
    "app.services.refund_reconciliation",
    "app.services.signing_keys",
}

# Core trust routers that must reach the spine through the facade.
CORE_TRUST_ROUTERS = (
    "app/routers/mcp.py",
    "app/routers/permits.py",
    "app/routers/permit_requests.py",
    "app/routers/quotes.py",
    "app/routers/receipts.py",
    "app/routers/evidence.py",
    "app/routers/audit.py",
    "app/routers/me.py",
    "app/routers/keys.py",
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def _resolve(module_name: str, node: ast.ImportFrom) -> str | None:
    """Resolve an ImportFrom (absolute or relative) to an absolute module path."""
    if node.level == 0:
        return node.module
    # Relative: the anchor is the importing module's containing package.
    containing = module_name.split(".")[:-1]
    ascend = node.level - 1
    base = containing[: len(containing) - ascend] if ascend else containing
    parts = list(base)
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


# (file name in app/trust, imported module) pairs that may cross the inward-only
# boundary. Each must be imported inside a function body, never at module scope:
# the lazy import is what keeps app.trust import-light and free of cycles with
# the router it calls back into. Adding an entry here is an architectural
# decision, not a test fix; test_trust_package_lazy_exceptions_are_exact pins
# the set so it cannot grow silently.
TRUST_PACKAGE_LAZY_EXCEPTIONS = frozenset({("adapters.py", "app.routers.mcp")})


def _is_module(dotted: str) -> bool:
    """True when ``dotted`` names a module or package in this repository."""
    candidate = ROOT.joinpath(*dotted.split("."))
    return (
        candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file()
    )


def _imports(path: Path, source: str | None = None) -> list[tuple[str, bool]]:
    """Return every absolute ``app.*`` module ``path`` imports, anywhere in it.

    Each entry is ``(module, in_function)``. The whole syntax tree is walked,
    so an import inside a function, method, class body or ``if`` block is
    reported exactly like one at module scope. Relative imports are resolved
    to absolute form so the guard is independent of import style, and
    ``from app.services import permits`` is reported as the submodule
    ``app.services.permits`` it actually binds rather than as its package.
    ``source`` parses that text as though it lived at ``path``.
    """
    module_name = _module_name(path)
    tree = ast.parse(path.read_text() if source is None else source)
    function_imports: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    function_imports.add(id(inner))

    targets: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        in_function = id(node) in function_imports
        if isinstance(node, ast.ImportFrom):
            resolved = _resolve(module_name, node)
            if not resolved or not resolved.startswith("app"):
                continue
            for alias in node.names:
                submodule = f"{resolved}.{alias.name}"
                targets.append(
                    (submodule if _is_module(submodule) else resolved, in_function)
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    targets.append((alias.name, in_function))
    return targets


def _trust_package_imports() -> list[tuple[str, str, bool]]:
    return [
        (path.name, module, in_function)
        for path in sorted(TRUST_DIR.glob("*.py"))
        for module, in_function in _imports(path)
    ]


def test_trust_package_only_depends_inward():
    violations: list[str] = []
    for file_name, module, in_function in _trust_package_imports():
        allowed = module.startswith(ALLOWED_PACKAGE_PREFIXES) or (
            module in SPINE_SERVICE_MODULES
        )
        if allowed:
            continue
        if (file_name, module) in TRUST_PACKAGE_LAZY_EXCEPTIONS and in_function:
            continue
        violations.append(f"{file_name} -> {module}")
    assert not violations, (
        f"app/trust must not depend on routers or non-spine workloads: {violations}"
    )


def test_trust_package_lazy_exceptions_are_exact():
    """The exception list holds one entry, and that entry is still real.

    A stale entry would quietly pre-approve a future import of the same
    module, so each listed import must still exist, and still be lazy.
    """
    assert TRUST_PACKAGE_LAZY_EXCEPTIONS == {("adapters.py", "app.routers.mcp")}
    seen = {
        (file_name, module): in_function
        for file_name, module, in_function in _trust_package_imports()
        if (file_name, module) in TRUST_PACKAGE_LAZY_EXCEPTIONS
    }
    assert set(seen) == TRUST_PACKAGE_LAZY_EXCEPTIONS, (
        "stale TRUST_PACKAGE_LAZY_EXCEPTIONS entry: "
        f"{TRUST_PACKAGE_LAZY_EXCEPTIONS - set(seen)}"
    )
    assert all(seen.values()), (
        "an allow-listed router import must stay inside a function body: "
        f"{[key for key, lazy in seen.items() if not lazy]}"
    )


def test_core_trust_routers_use_the_facade():
    violations: list[str] = []
    for rel in CORE_TRUST_ROUTERS:
        path = ROOT / rel
        for module, in_function in _imports(path):
            if module in SPINE_SERVICE_MODULES:
                scope = "function body" if in_function else "module scope"
                violations.append(f"{rel} -> {module} ({scope})")
    assert not violations, (
        "core trust routers must import spine primitives via app.trust, "
        f"not directly: {violations}"
    )


def test_import_scan_sees_function_body_imports():
    """The scanner itself must report lazy and submodule-style imports.

    Without this, a regression back to a module-scope-only walk would make
    both boundary tests pass vacuously.
    """
    source = (
        "import app.core.config\n"
        "\n"
        "def lazy():\n"
        "    from app.services.permits import permit_constraints_snapshot\n"
        "    return permit_constraints_snapshot\n"
        "\n"
        "class Holder:\n"
        "    def method(self):\n"
        "        from ..services import receipts\n"
        "        return receipts\n"
    )
    found = _imports(ROOT / "app" / "routers" / "_boundary_probe.py", source)
    assert sorted(found) == [
        ("app.core.config", False),
        ("app.services.permits", True),
        ("app.services.receipts", True),
    ]
