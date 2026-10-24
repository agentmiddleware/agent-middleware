"""Public MCP endpoint (``/mcp/public``): unauthenticated, read-only.

A receipt is only portable evidence if someone who holds no credential here
can still check its signature — the same reasoning that keeps
``/.well-known/trust-keys.json`` open. This endpoint extends that stance to
MCP clients that cannot hold an API key at all (public plugin runtimes such
as ChatGPT apps): a stateless Streamable HTTP surface whose tools verify
evidence and describe the plane, and nothing else.

Hard boundary: nothing reachable from this module mints permits, debits
wallets, or enters the governed invoke path. The three tools are pure reads
— ``verify_receipt`` checks a caller-supplied portable bundle against the
plane's published Ed25519 keys (via :mod:`b2a_sdk.receipt_verifier`, the
same verifier an offline third party runs), ``get_trust_plane_info`` mirrors
public discovery metadata, and ``list_governed_tools`` mirrors the
unauthenticated ``/mcp/tools.json`` manifest. Verification failures are
structured results, never errors: "invalid" is a verdict, "unknown_key" or
"keys_unavailable" is the verifier's situation, and conflating them would
let an outage read as fraud.

Transport mirrors the standard endpoint: each POST gets a fresh stateless
SDK transport in JSON mode, GET/DELETE answer 405, and cross-origin browser
calls are refused. Disabled by default — ``ENABLE_PUBLIC_MCP_ENDPOINT`` must
be set explicitly, so the surface cannot appear before an operator turns it
on. Production-like environments refuse to boot with the flag set, and the
handler 404s there even if the flag is on; receipt verification stays on
``/.well-known/trust-keys.json``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mcp.types as mcp_types
from fastapi import APIRouter, HTTPException, Request
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from app.core.config import get_settings, public_api_origin
from app.core.trust_mode import is_production_like_environment
from app.core.product_positioning import (
    POSITIONING_DESCRIPTION,
    get_product_positioning,
)
from app.routers.mcp_standard import _SdkTransportResponse, _validate_origin
from app.routers.well_known import _published_key
from app.schemas.billing import ServiceCategory
from app.services.mcp_generator import MCP_SERVER_VERSION, get_mcp_generator
from app.services.signing_keys import (
    RECEIPT_CANONICALIZATION,
    SigningKeyError,
    get_signing_key_service,
)

if TYPE_CHECKING:
    from b2a_sdk.receipt_verifier import VerificationKey


# Image layout: both Dockerfiles copy the in-repo SDK to /sdk/python, install
# it, and set PYTHONPATH=/sdk/python/src. From /app/app/routers/mcp_public.py,
# parents[3] is /, so the checkout-relative path is this same file.
_IMAGE_SDK_RECEIPT_VERIFIER = Path("/sdk/python/src/b2a_sdk/receipt_verifier.py")
_CHECKOUT_SDK_RECEIPT_VERIFIER = (
    Path(__file__).resolve().parents[3]
    / "sdk"
    / "python"
    / "src"
    / "b2a_sdk"
    / "receipt_verifier.py"
)


def _receipt_verifier_file_candidates() -> tuple[Path, ...]:
    """Checkout path first. In the image that path is ``/sdk/python/...``."""
    if _CHECKOUT_SDK_RECEIPT_VERIFIER == _IMAGE_SDK_RECEIPT_VERIFIER:
        return (_IMAGE_SDK_RECEIPT_VERIFIER,)
    return (_CHECKOUT_SDK_RECEIPT_VERIFIER, _IMAGE_SDK_RECEIPT_VERIFIER)


def _load_receipt_verifier_file(module_path: Path):
    spec = importlib.util.spec_from_file_location(
        "_agent_middleware_receipt_verifier",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load receipt verifier from {module_path}")
    receipt_verifier = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = receipt_verifier
    spec.loader.exec_module(receipt_verifier)
    return receipt_verifier


def _import_receipt_verifier():
    """Import the first-party offline verifier.

    Images declare the dependency by installing ``/sdk/python`` and putting
    ``/sdk/python/src`` on ``PYTHONPATH``. A checkout that has not installed
    the package still loads that same file from the repository root. The
    fallback is not a second implementation.
    """
    try:
        return importlib.import_module("b2a_sdk.receipt_verifier")
    except ImportError as import_error:
        for module_path in _receipt_verifier_file_candidates():
            if module_path.is_file():
                return _load_receipt_verifier_file(module_path)
        raise import_error


_receipt_verifier = _import_receipt_verifier()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp/public", tags=["MCP Public Endpoint"])

PUBLIC_SERVER_NAME = "Agent Middleware Trust Verifier"

# Reject the complete unauthenticated HTTP request before the MCP SDK parses
# JSON. The signing-input check below remains a second, verifier-specific
# guard for non-HTTP callers of the handler.
_MAX_PUBLIC_MCP_REQUEST_BODY_BYTES = 256 * 1024
_MAX_SIGNING_INPUT_BYTES = 256 * 1024

_SERVER_INSTRUCTIONS = (
    "Read-only, unauthenticated verification surface for an Agent Middleware "
    "transaction-integrity boundary. verify_receipt checks whether a portable "
    "receipt bundle is "
    "authentic: 'verified' and 'invalid' are verdicts about the receipt; "
    "'mismatch' is a deterministic consistency rejection; "
    "'unknown_key', 'malformed', 'unsupported', and 'keys_unavailable' mean "
    "the verifier cannot tell and must never be reported as proof of "
    "tampering. get_trust_plane_info returns the boundary's identity and "
    "published Ed25519 signing keys; list_governed_tools shows what the "
    "authenticated transaction boundary can invoke. Nothing on this surface can "
    "create signing authority, spend credits, mint permits, change governed "
    "tool registration, or invoke governed tools. Ingress rate-limit and "
    "observability bookkeeping still apply."
)

_STATUS_VALUES = [
    "verified",
    "invalid",
    "malformed",
    "unknown_key",
    "mismatch",
    "unsupported",
    "keys_unavailable",
]

_READ_ONLY = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_TOOLS: list[mcp_types.Tool] = [
    mcp_types.Tool(
        name="verify_receipt",
        title="Verify a portable receipt",
        description=(
            "Use this when the user provides a portable receipt bundle from "
            "an Agent Middleware transaction-integrity boundary and wants to "
            "know whether it is authentic and what it attests. Verifies the "
            "Ed25519 signature "
            "over the bundle's signing_input against this plane's published "
            "keys and reports the signed claims. A 'verified' result means "
            "the signature matches a key this deployment publishes; statuses "
            "'mismatch' means the envelope, signed payload, or expected issuer "
            "disagrees without demonstrating a bad signature; remaining "
            "statuses mean the verifier cannot tell."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "bundle": {
                    "anyOf": [{"type": "object"}, {"type": "string"}],
                    "description": (
                        "The portable receipt bundle exactly as returned by "
                        "GET /v1/receipts/{receipt_id}/portable — a JSON "
                        "object (or its JSON string) carrying signing_input, "
                        "signature, and kid, plus optional alg, "
                        "canonicalization, issuer, and receipt_id envelope "
                        "fields."
                    ),
                },
                "expected_issuer": {
                    "type": "string",
                    "description": (
                        "Optional public origin the bundle's issuer label "
                        "must match (e.g. https://api.example.com). Catches "
                        "relabeled evidence; does not by itself authenticate "
                        "the issuer."
                    ),
                },
            },
            "required": ["bundle"],
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "ok": {
                    "type": "boolean",
                    "description": (
                        "True only when the signature verified over the signed bytes."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": _STATUS_VALUES,
                    "description": (
                        "'verified' and 'invalid' are cryptographic verdicts; "
                        "'mismatch' is a deterministic consistency failure, "
                        "not a bad-signature claim; remaining statuses say "
                        "the verifier cannot tell."
                    ),
                },
                "reason": {"type": ["string", "null"]},
                "receipt_id": {"type": ["string", "null"]},
                "kid": {
                    "type": ["string", "null"],
                    "description": "Key id the bundle names as its signer.",
                },
                "claims": {
                    "type": "object",
                    "description": (
                        "Fields parsed out of the signed bytes (tool, "
                        "credits, timestamps, ...). Populated only when ok."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["ok", "status"],
        },
        annotations=_READ_ONLY,
    ),
    mcp_types.Tool(
        name="get_trust_plane_info",
        title="Get trust plane info",
        description=(
            "Use this when the user asks about this consequential-action "
            "transaction-integrity boundary, which Ed25519 keys it publishes "
            "for receipt verification, or where its public discovery and "
            "verification endpoints live. The tool name is a legacy alias."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "positioning": {
                    "type": "object",
                    "properties": {
                        "schema_version": {"type": "string"},
                        "id": {"type": "string"},
                        "scope": {"type": "object", "additionalProperties": True},
                        "claim_boundary": {"type": "string"},
                    },
                    "required": [
                        "schema_version",
                        "id",
                        "scope",
                        "claim_boundary",
                    ],
                    "additionalProperties": True,
                },
                "issuer": {
                    "type": "string",
                    "description": (
                        "Public origin of this plane; empty when PUBLIC_URL is unset."
                    ),
                },
                "alg": {"type": "string"},
                "canonicalization": {"type": "string"},
                "keys_available": {"type": "boolean"},
                "keys": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kid": {"type": "string"},
                            "alg": {"type": "string"},
                            "status": {"type": "string"},
                            "public_key_b64": {"type": "string"},
                            "activated_at": {"type": ["string", "null"]},
                            "retired_at": {"type": ["string", "null"]},
                        },
                        "required": ["kid", "alg", "status"],
                        "additionalProperties": True,
                    },
                },
                "endpoints": {"type": "object", "additionalProperties": True},
            },
            "required": [
                "name",
                "positioning",
                "issuer",
                "alg",
                "keys_available",
                "keys",
            ],
        },
        annotations=_READ_ONLY,
    ),
    mcp_types.Tool(
        name="list_governed_tools",
        title="List governed tools",
        description=(
            "Use this when the user wants to know which tools this transaction-"
            "integrity boundary governs — what an authenticated, wallet-scoped "
            "agent could invoke through the logical-action transaction loop. "
            "Returns name, description, category, and per-call pricing; "
            "full input schemas live at GET /mcp/tools.json. This tool "
            "cannot invoke anything."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [c.value for c in ServiceCategory],
                    "description": "Optional service category filter.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                    "description": "Maximum tools to return.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Number of tools to skip for pagination.",
                },
            },
            "additionalProperties": False,
        },
        outputSchema={
            "type": "object",
            "properties": {
                "tools": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "category": {"type": "string"},
                            "credits_per_call": {"type": ["number", "string", "null"]},
                            "unit_name": {"type": "string"},
                            "requires_permit": {"type": "boolean"},
                        },
                        "required": ["name", "description", "category"],
                        "additionalProperties": False,
                    },
                },
                "count": {"type": "integer"},
                "total": {"type": "integer"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "has_more": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "required": ["tools", "count", "total", "has_more"],
        },
        annotations=_READ_ONLY,
    ),
]


def _mcp_error(code: int, message: str) -> McpError:
    return McpError(mcp_types.ErrorData(code=code, message=message))


async def _published_key_set() -> dict[str, bytes | VerificationKey] | None:
    """Load the same key set a verifier gets from trust-keys.json.

    ``None`` means "no usable keys right now" — the tool-level analogue of
    the 503 that ``/.well-known/trust-keys.json`` serves instead of an empty
    list, and it must be reported as retry-later, never as a verdict.
    """
    service = get_signing_key_service()
    try:
        keys = await service.list_public_keys()
    except (SigningKeyError, SQLAlchemyError, RuntimeError):
        logger.warning("public_mcp_trust_keys_unavailable", exc_info=True)
        return None
    key_set = _receipt_verifier.key_set_from_document(
        {"keys": [_published_key(key) for key in keys]}
    )
    return key_set or None


def _keys_unavailable_result() -> tuple[dict[str, Any], str]:
    structured = {
        "ok": False,
        "status": "keys_unavailable",
        "reason": (
            "No usable signing key is published right now. Treat the "
            "receipt as unverified-pending, not invalid, and retry."
        ),
        "receipt_id": None,
        "kid": None,
        "claims": {},
    }
    return structured, (
        "CANNOT VERIFY (keys_unavailable) — no usable signing key is "
        "published right now. This is not a verdict on the receipt; retry "
        "later."
    )


def _signing_input_too_large(bundle: Any) -> bool:
    if isinstance(bundle, str):
        try:
            return len(bundle.encode("utf-8")) > _MAX_SIGNING_INPUT_BYTES
        except UnicodeEncodeError:
            return False
    if isinstance(bundle, dict):
        signing_input = bundle.get("signing_input")
        if isinstance(signing_input, str):
            try:
                return len(signing_input.encode("utf-8")) > _MAX_SIGNING_INPUT_BYTES
            except UnicodeEncodeError:
                return False
    return False


async def _tool_verify_receipt(
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    bundle = arguments.get("bundle")
    if bundle is None or not isinstance(bundle, dict | str):
        raise _mcp_error(
            -32602,
            "verify_receipt requires a 'bundle' argument: the portable "
            "receipt as a JSON object or JSON string.",
        )
    expected_issuer = arguments.get("expected_issuer")
    if expected_issuer is not None and not isinstance(expected_issuer, str):
        raise _mcp_error(-32602, "'expected_issuer' must be a string")

    if _signing_input_too_large(bundle):
        structured = {
            "ok": False,
            "status": "malformed",
            "reason": (
                "bundle exceeds the "
                f"{_MAX_SIGNING_INPUT_BYTES // 1024} KiB verification limit"
            ),
            "receipt_id": None,
            "kid": None,
            "claims": {},
        }
        return structured, (
            "CANNOT VERIFY (malformed) — the bundle is larger than this "
            "endpoint will verify."
        )

    # Malformed bundles and issuer mismatches need no key. An algorithm or
    # canonicalization string is not authenticated until the signature is
    # checked, so an empty key set must not classify it: that used to report
    # UNSUPPORTED and skip the published keys. Unknown kid falls through and
    # the real key set then decides invalid, unsupported, or unknown_key.
    preflight = _receipt_verifier.verify_bundle(
        bundle, {}, expected_issuer=expected_issuer
    )
    if preflight.status is _receipt_verifier.VerificationStatus.UNKNOWN_KEY:
        key_set = await _published_key_set()
        if key_set is None:
            return _keys_unavailable_result()
        result = _receipt_verifier.verify_bundle(
            bundle, key_set, expected_issuer=expected_issuer
        )
    else:
        result = preflight

    structured = {
        "ok": result.ok,
        "status": result.status.value,
        "reason": result.reason,
        "receipt_id": result.receipt_id,
        "kid": result.key_id,
        "claims": dict(result.claims),
    }
    if result.ok:
        text = (
            f"VERIFIED — signature checks out under published key "
            f"{result.key_id!r} for receipt {result.receipt_id!r}. The "
            "signed claims are in structuredContent.claims; report only "
            "those fields as attested."
        )
    elif result.status is _receipt_verifier.VerificationStatus.MISMATCH:
        # Checked before is_tampered: a MISMATCH is a rejection, but saying its
        # "signature does not hold" would be false. The signature may well have
        # verified -- what failed is agreement between the signed bytes and the
        # envelope around them, or an explicit caller expectation.
        text = (
            f"CANNOT ACCEPT (mismatch) — {result.reason}. The evidence or "
            "caller expectation is internally inconsistent; this does not "
            "mean the signature failed."
        )
    elif result.is_tampered:
        text = (
            f"INVALID — {result.reason}. The bundle is well-formed but its "
            "signature does not hold; treat it as altered or forged."
        )
    else:
        text = (
            f"CANNOT VERIFY ({result.status.value}) — {result.reason}. This "
            "is a statement about the verifier's situation, not evidence of "
            "tampering."
        )
    return structured, text


async def _tool_get_trust_plane_info(
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    service = get_signing_key_service()
    try:
        keys = await service.list_public_keys()
    except (SigningKeyError, SQLAlchemyError, RuntimeError):
        logger.warning("public_mcp_trust_keys_unavailable", exc_info=True)
        keys = []

    published = []
    for key in keys:
        entry = _published_key(key)
        entry.pop("jwk", None)  # concise; the JWK mirror lives at jwks.json
        published.append(entry)

    structured = {
        "name": PUBLIC_SERVER_NAME,
        "description": POSITIONING_DESCRIPTION,
        "positioning": get_product_positioning(),
        "issuer": public_api_origin(),
        "alg": "Ed25519",
        "canonicalization": RECEIPT_CANONICALIZATION,
        "keys_available": bool(published),
        "keys": published,
        "endpoints": {
            "trust_keys": "/.well-known/trust-keys.json",
            "jwks": "/.well-known/jwks.json",
            "portable_receipt": "/v1/receipts/{receipt_id}/portable",
            "tools_manifest": "/mcp/tools.json",
            "governed_mcp": "/mcp (authenticated, wallet-scoped API key)",
        },
    }
    kid_list = ", ".join(k["kid"] for k in published) or "none published"
    text = (
        "Transaction-integrity boundary at "
        f"{structured['issuer'] or '(PUBLIC_URL unset)'}; "
        f"published Ed25519 keys: {kid_list}."
    )
    return structured, text


async def _tool_list_governed_tools(
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    category_raw = arguments.get("category")
    category: ServiceCategory | None = None
    if category_raw is not None:
        try:
            category = ServiceCategory(category_raw)
        except ValueError as exc:
            valid = ", ".join(c.value for c in ServiceCategory)
            raise _mcp_error(
                -32602,
                f"Unknown category {category_raw!r}. Valid categories: {valid}",
            ) from exc

    limit = arguments.get("limit", 50)
    offset = arguments.get("offset", 0)
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 200):
        raise _mcp_error(-32602, "'limit' must be an integer from 1 to 200")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise _mcp_error(-32602, "'offset' must be a non-negative integer")

    # Read the already-synchronized registry directly. The canonical HTTP
    # manifest builder also performs lazy registration/unregistration; an
    # anonymous discovery call must not change governed dispatch state.
    manifest = await get_mcp_generator().generate_tools_json_async(category=category)
    entries = []
    for tool in manifest["tools"]:
        annotations = tool.get("annotations") or {}
        entries.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "category": annotations.get("category", "unknown"),
                "credits_per_call": annotations.get(
                    "creditsPerCallExact", annotations.get("creditsPerCall")
                ),
                "unit_name": annotations.get("unitName", "call"),
                "requires_permit": bool(annotations.get("requirePermit", False)),
            }
        )

    total = len(entries)
    page = entries[offset : offset + limit]
    structured = {
        "tools": page,
        "count": len(page),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(page) < total,
        "note": (
            "Full input schemas: GET /mcp/tools.json. Invoking any of these "
            "requires the authenticated governed endpoint and a permit."
        ),
    }
    text = (
        f"{total} governed tool(s)"
        + (f" in category {category.value!r}" if category else "")
        + f"; returning {len(page)} starting at offset {offset}."
    )
    return structured, text


_TOOL_HANDLERS = {
    "verify_receipt": _tool_verify_receipt,
    "get_trust_plane_info": _tool_get_trust_plane_info,
    "list_governed_tools": _tool_list_governed_tools,
}


def _build_public_mcp_server() -> Server:
    server = Server(
        PUBLIC_SERVER_NAME,
        version=MCP_SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
    )

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return _TOOLS

    async def handle_call_tool(
        req: mcp_types.CallToolRequest,
    ) -> mcp_types.ServerResult:
        """Dispatch to a public read-only handler.

        Registered directly (repo convention, see ``mcp_standard``) so
        argument-validation failures surface as JSON-RPC errors with real
        codes instead of collapsing into ``isError`` text results.
        """
        handler = _TOOL_HANDLERS.get(req.params.name)
        if handler is None:
            raise _mcp_error(
                -32602,
                f"Unknown tool: {req.params.name!r}. This public endpoint "
                "serves only read-only verification and discovery tools; "
                "governed tools require the authenticated /mcp endpoint.",
            )
        try:
            structured, text = await handler(req.params.arguments or {})
        except McpError:
            raise
        except Exception as exc:
            logger.exception("public_mcp_tool_call_failed")
            raise _mcp_error(-32603, "internal_error") from exc
        return mcp_types.ServerResult(
            mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=text)],
                structuredContent=structured,
            )
        )

    server.request_handlers[mcp_types.CallToolRequest] = handle_call_tool
    return server


_public_mcp_server = _build_public_mcp_server()


def _require_enabled() -> None:
    settings = get_settings()
    if is_production_like_environment(settings.ENVIRONMENT):
        raise HTTPException(status_code=404, detail="Not Found")
    if not settings.ENABLE_PUBLIC_MCP_ENDPOINT:
        raise HTTPException(status_code=404, detail="Not Found")


@router.post(
    "",
    name="Public MCP Streamable HTTP Endpoint",
    summary=(
        "Public read-only MCP endpoint (official MCP SDK, stateless "
        "Streamable HTTP, JSON responses, no authentication)"
    ),
)
async def handle_public_mcp(request: Request) -> Response:
    """Hand one public MCP message to the official SDK's transport.

    Deliberately no auth dependency: every tool behind this surface is a
    read of public material. Origin validation still applies — the absence
    of credentials is not an invitation to be a cross-site oracle.
    """
    _require_enabled()
    _validate_origin(request)
    return _SdkTransportResponse(
        _public_mcp_server,
        max_request_body_size=_MAX_PUBLIC_MCP_REQUEST_BODY_BYTES,
    )


@router.api_route("", methods=["GET", "DELETE"], include_in_schema=False)
async def public_mcp_no_stream() -> Response:
    """Stateless server: no server-initiated stream, no session to delete."""
    _require_enabled()
    return Response(status_code=405, headers={"Allow": "POST"})
