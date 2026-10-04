"""
Static Files Router
====================
Serves static discovery docs for agents (llms.txt + advertised markdown).
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pathlib import Path

from ..core.config import get_settings

router = APIRouter(tags=["Static"])

_PUBLIC_URL_PLACEHOLDER = "{{PUBLIC_URL}}"
_LOCAL_EXAMPLE = "http://localhost:8000"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Paths advertised in /.well-known/agent.json documentation (must 200).
_MARKDOWN_DOCS: dict[str, Path] = {
    "/WEDGE.md": _REPO_ROOT / "WEDGE.md",
    "/SECURITY_LIMITATIONS.md": _REPO_ROOT / "SECURITY_LIMITATIONS.md",
    "/DESIGN_PARTNER_GUIDE.md": _REPO_ROOT / "DESIGN_PARTNER_GUIDE.md",
    "/docs/partner-api-key-bootstrap.md": (
        _REPO_ROOT / "docs" / "partner-api-key-bootstrap.md"
    ),
    "/docs/agent-accountability.md": (_REPO_ROOT / "docs" / "agent-accountability.md"),
}


def _serve_markdown(path: str) -> PlainTextResponse:
    file_path = _MARKDOWN_DOCS.get(path)
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"{path} not found")
    return PlainTextResponse(
        file_path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


@router.get(
    "/llms.txt",
    summary="LLM-Readable Instructions",
    description=(
        "Plain-text instructions at the plural llms.txt proposal path. "
        "The legacy /llm.txt route serves identical content."
    ),
    response_class=PlainTextResponse,
)
@router.get(
    "/llm.txt",
    summary="Legacy LLM-Readable Instructions Alias",
    description=(
        "Backward-compatible singular alias for /llms.txt; both routes serve "
        "identical plain-text instructions."
    ),
    response_class=PlainTextResponse,
)
async def get_llm_txt():
    """
    Serve the LLM-readable instructions at both public paths.

    Substitutes ``{{PUBLIC_URL}}`` from settings so production agents do not
    treat localhost as the deployment base.
    """
    settings = get_settings()
    llm_path = _REPO_ROOT / "static" / "llm.txt"
    if llm_path.exists():
        content = llm_path.read_text(encoding="utf-8")
    else:
        content = f"""# Agent Middleware API — LLM-Readable Documentation

**Agent-first:** Intended reader = autonomous agents. Fetch GET /.well-known/agent.json first (use `capabilities` vs `proof_surfaces`); use GET /health/dependencies (`simulation_modes`, `enable_proof_surfaces`) before assuming real side effects.

**Version:** from GET /.well-known/agent.json
**Base URL:** {_PUBLIC_URL_PLACEHOLDER}
**Auth:** X-API-Key on protected routes

## Quick Start

1. GET /.well-known/agent.json — Bootstrap manifest (versioned positioning + labeled proof surfaces)
2. GET /llms.txt — Full prose (if this fallback appears, static/llm.txt is missing on disk)
3. GET /mcp/tools.json — MCP tools
4. GET /openapi.json — API contract

## MCP Tools
GET /mcp/tools.json — List available tools
"""

    public = (settings.PUBLIC_URL or "").strip().rstrip("/")
    if public:
        base = public
    else:
        base = f"(configure PUBLIC_URL for this deployment; local example {_LOCAL_EXAMPLE})"
    content = content.replace(_PUBLIC_URL_PLACEHOLDER, base)
    # Never present bare localhost as the production Base URL line.
    if not public and f"**Base URL:** {_LOCAL_EXAMPLE}" in content:
        content = content.replace(
            f"**Base URL:** {_LOCAL_EXAMPLE}",
            f"**Base URL:** {base}",
        )
    return PlainTextResponse(content=content)


@router.get(
    "/WEDGE.md",
    summary="Transaction-integrity boundary",
    description="Compatibility path to the transaction-integrity boundary definition.",
    response_class=PlainTextResponse,
)
async def get_wedge_md():
    return _serve_markdown("/WEDGE.md")


@router.get(
    "/SECURITY_LIMITATIONS.md",
    summary="Security limitations",
    description="Honest non-claims and production posture requirements.",
    response_class=PlainTextResponse,
)
async def get_security_limitations_md():
    return _serve_markdown("/SECURITY_LIMITATIONS.md")


@router.get(
    "/DESIGN_PARTNER_GUIDE.md",
    summary="Design partner guide",
    description="Transaction-integrity demo and dogfood path for design partners.",
    response_class=PlainTextResponse,
)
async def get_design_partner_guide_md():
    return _serve_markdown("/DESIGN_PARTNER_GUIDE.md")


@router.get(
    "/docs/partner-api-key-bootstrap.md",
    summary="Partner API key bootstrap",
    description="Gated operator flow to provision wallet-scoped agent API keys.",
    response_class=PlainTextResponse,
)
async def get_partner_api_key_bootstrap_md():
    return _serve_markdown("/docs/partner-api-key-bootstrap.md")


@router.get(
    "/docs/agent-accountability.md",
    summary="Agent accountability",
    description=(
        "Why an autonomous agent runs inside the permit/receipt loop, how to "
        "verify a receipt offline, and what receipts do not prove."
    ),
    response_class=PlainTextResponse,
)
async def get_agent_accountability_md():
    return _serve_markdown("/docs/agent-accountability.md")


@router.get(
    "/dashboard",
    summary="Public operator evidence index",
    description=(
        "Truthful public status and evidence links. It contains no fabricated "
        "telemetry, authenticated tenant data, or browser credential handling."
    ),
    response_class=HTMLResponse,
    responses={404: {"description": "Dashboard not found"}},
)
async def get_human_dashboard():
    dashboard_path = _REPO_ROOT / "static" / "dashboard.html"
    if not dashboard_path.is_file():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return HTMLResponse(
        dashboard_path.read_text(encoding="utf-8"),
        media_type="text/html; charset=utf-8",
    )
