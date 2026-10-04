"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
AWI Enhanced Router — Phase 9
=============================

New endpoints for Phase 9 AWI features:
- Passkey/WebAuthn challenge and verification
- Bidirectional DOM synchronization
- RAG-based memory queries

Based on arXiv:2506.10953v1 gap analysis.
"""

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from ..core.auth import AuthContext, get_auth_context
from ..core.time import utc_now
from ..schemas.awi_enhanced import (
    DOMBridgeSessionRequest,
    DOMBridgeSessionResponse,
    DOMRepresentationType,
    DOMStateResponse,
    DOMSyncRequest,
    DOMSyncResponse,
    MemoryIndexRequest,
    MemoryIndexResponse,
    MemorySearchResult,
    PasskeyChallengeRequest,
    PasskeyChallengeResponse,
    PasskeySessionStatus,
    PasskeyVerifyRequest,
    PasskeyVerifyResponse,
    RAGQueryRequest,
    RAGQueryResponse,
    SessionContextResponse,
)
from ..services.awi_playwright_bridge import BrowserSessionLimitExceeded

if TYPE_CHECKING:
    from ..services.awi_rag_engine import SessionMemory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/awi", tags=["AWI Enhanced"])
_DOM_SESSION_WALLETS: dict[str, str | None] = {}


def _not_found(kind: str, resource_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": f"{kind} {resource_id} not found"},
    )


def _require_owner_or_not_found(
    auth: AuthContext, wallet_id: str | None, not_found: HTTPException
) -> None:
    """Authorize a resource owned by ``wallet_id``, or answer ``not_found``.

    The session helpers used to 404 a missing id and only then run the wallet
    check, whose 403 body carries the *owning* wallet id. Any wallet-scoped
    key could therefore tell a real session id from an invented one and learn
    which wallet owns it. A resource the caller may not see is now reported
    exactly like one that does not exist; owners and bootstrap admins are
    unaffected.
    """
    try:
        if wallet_id:
            auth.require_wallet_access(wallet_id)
        else:
            auth.require_bootstrap_admin()
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise not_found from None
        raise


async def _require_awi_session_access(session_id: str, auth: AuthContext) -> None:
    """Authorize access to wallet-scoped AWI session state."""
    from ..services.awi_session import get_awi_session_manager

    manager = get_awi_session_manager()
    session = await manager.get_session(session_id)

    if not session:
        raise _not_found("Session", session_id)

    _require_owner_or_not_found(
        auth, session.wallet_id, _not_found("Session", session_id)
    )


async def _require_dom_session_access(session_id: str, auth: AuthContext) -> None:
    """Authorize access to standalone DOM bridge sessions."""
    from ..services.awi_playwright_bridge import get_playwright_bridge

    bridge = get_playwright_bridge()
    session = await bridge.get_session(session_id)
    if not session:
        raise _not_found("Session", session_id)

    _require_owner_or_not_found(
        auth, _DOM_SESSION_WALLETS.get(session_id), _not_found("Session", session_id)
    )


async def _require_memory_access(session_id: str, auth: AuthContext) -> None:
    """Authorize access to RAG memories through their owning AWI session."""
    try:
        await _require_awi_session_access(session_id, auth)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        auth.require_bootstrap_admin()


async def _load_owned_memory(memory_id: str, auth: AuthContext) -> "SessionMemory":
    """Load a RAG memory, or 404 — whether it is missing or not the caller's.

    The denial for a foreign memory used to be a 403 naming the owning wallet
    (or, once its session was gone, ``admin_access_denied``), next to a 404
    for an unknown id. Every denial now carries the same body as a missing
    memory and never names the owning session or wallet.
    """
    from ..services.awi_rag_engine import get_awi_rag_engine

    memory = await get_awi_rag_engine().get_memory(memory_id)
    if memory is None:
        raise _not_found("Memory", memory_id)
    try:
        await _require_memory_access(memory.session_id, auth)
    except HTTPException as exc:
        if exc.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ):
            raise _not_found("Memory", memory_id) from None
        raise
    return memory


def _rag_owner_scope(wallet_id: str, auth: AuthContext) -> set[str | None]:
    """Memory owners a caller acting for ``wallet_id`` may retrieve from.

    Bootstrap admins additionally see memories of ownerless (admin-created)
    sessions, matching the route-level tenant filter on ``/rag/query``.
    """
    owners: set[str | None] = {wallet_id}
    if auth.is_bootstrap_admin:
        owners.add(None)
    return owners


# ─────────────────────────────────────────────────────────────────────────────
# Passkey / WebAuthn Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/passkey/challenge",
    summary="Create passkey challenge",
    description=(
        "Create a WebAuthn challenge for high-risk AWI action verification. "
        "Governed: requires X-Permit-Id and Idempotency-Key."
    ),
)
async def create_passkey_challenge(
    request: PasskeyChallengeRequest,
    auth: AuthContext = Depends(get_auth_context),
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
):
    """
    Create a WebAuthn registration/authentication challenge.

    Governed: requires ``X-Permit-Id`` + ``Idempotency-Key`` (tool
    ``awi_passkey_challenge``). Prefer MCP when integrating agents.
    """
    from ..services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
        consume_awi_http_replay,
        raise_awi_http_error,
    )
    from ..services.awi_session import get_awi_session_manager
    from ..services.webauthn_provider import get_webauthn_provider

    await _require_awi_session_access(request.session_id, auth)
    session = await get_awi_session_manager().get_session(request.session_id)
    wallet_id = session.wallet_id if session else None

    request_payload = request.model_dump(mode="json")
    gov = await begin_awi_http_governed(
        auth=auth,
        wallet_id=wallet_id,
        tool_name="awi_passkey_challenge",
        endpoint="POST /v1/awi/passkey/challenge",
        permit_id=x_permit_id,
        idempotency_key_lines=idempotency_key_lines,
        request_payload=request_payload,
    )
    replayed = consume_awi_http_replay(gov)
    if replayed is not None:
        return replayed

    webauthn = get_webauthn_provider()

    try:
        requires = await webauthn.requires_passkey(request.session_id, request.action)
        if not requires:
            await raise_awi_http_error(
                gov,
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "passkey_not_required",
                    "message": (
                        f"Action '{request.action}' does not require passkey verification"
                    ),
                },
            )

        gov.dispatch_started = True
        challenge = await webauthn.create_challenge(
            session_id=request.session_id,
            action=request.action,
        )
        body = PasskeyChallengeResponse(**challenge).model_dump(mode="json")
        return await complete_awi_http_governed(
            gov,
            request_payload=request_payload,
            response_payload=body,
        )
    except ValueError as e:
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "challenge_failed", "message": str(e)},
        )
    except HTTPException as exc:
        if gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=exc.status_code,
                detail=exc.detail
                if isinstance(exc.detail, dict)
                else {"error": "execution_failed"},
            )
        raise
    except Exception as e:
        logger.exception("Failed to create passkey challenge")
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "challenge_creation_failed", "message": str(e)},
        )


@router.post(
    "/passkey/verify",
    summary="Verify passkey response",
    description=(
        "Verify the WebAuthn credential response from the client. "
        "Governed: requires X-Permit-Id and Idempotency-Key."
    ),
)
async def verify_passkey(
    request: PasskeyVerifyRequest,
    auth: AuthContext = Depends(get_auth_context),
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
):
    """
    Verify WebAuthn assertion response.

    After successful verification, the action is marked as verified
    for 5 minutes (configurable).
    """
    from ..services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
        consume_awi_http_replay,
        raise_awi_http_error,
    )
    from ..services.awi_session import get_awi_session_manager
    from ..services.webauthn_provider import get_webauthn_provider

    webauthn = get_webauthn_provider()
    challenge = webauthn.get_challenge(request.challenge_id)
    if not challenge:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "verification_failed", "message": "Challenge not found"},
        )

    await _require_awi_session_access(challenge.session_id, auth)
    session = await get_awi_session_manager().get_session(challenge.session_id)
    wallet_id = session.wallet_id if session else None
    request_payload = request.model_dump(mode="json")
    gov = await begin_awi_http_governed(
        auth=auth,
        wallet_id=wallet_id,
        tool_name="awi_passkey_verify",
        endpoint="POST /v1/awi/passkey/verify",
        permit_id=x_permit_id,
        idempotency_key_lines=idempotency_key_lines,
        request_payload=request_payload,
    )
    replayed = consume_awi_http_replay(gov)
    if replayed is not None:
        return replayed

    try:
        gov.dispatch_started = True
        result = await webauthn.verify_response(
            challenge_id=request.challenge_id,
            credential=request.credential,
        )
        body = PasskeyVerifyResponse(**result).model_dump(mode="json")
        return await complete_awi_http_governed(
            gov,
            request_payload=request_payload,
            response_payload=body,
        )
    except ValueError as e:
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "verification_failed", "message": str(e)},
        )
    except HTTPException as exc:
        if gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=exc.status_code,
                detail=exc.detail
                if isinstance(exc.detail, dict)
                else {"error": "execution_failed"},
            )
        raise
    except Exception as e:
        logger.exception("Passkey verification error")
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "verification_error", "message": str(e)},
        )


@router.get(
    "/passkey/status/{session_id}/{action}",
    response_model=PasskeySessionStatus,
    summary="Check passkey verification status",
    description="Check if a session:action pair has valid passkey verification.",
)
async def check_passkey_status(
    session_id: str,
    action: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Check if the action is currently verified for this session."""
    from ..services.webauthn_provider import get_webauthn_provider

    await _require_awi_session_access(session_id, auth)

    webauthn = get_webauthn_provider()

    status_info = await webauthn.get_verification_status(session_id, action)
    return PasskeySessionStatus(**status_info)


@router.delete(
    "/passkey/invalidate/{session_id}",
    summary="Invalidate passkey verifications",
    description="Invalidate all passkey verifications for a session.",
)
async def invalidate_passkey(
    session_id: str,
    action: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Invalidate passkey verifications for a session."""
    from ..services.webauthn_provider import get_webauthn_provider

    await _require_awi_session_access(session_id, auth)

    webauthn = get_webauthn_provider()

    invalidated = await webauthn.invalidate_verification(session_id, action)

    return {
        "session_id": session_id,
        "action": action,
        "invalidated_count": invalidated,
    }


@router.get(
    "/passkey/high-risk-actions",
    summary="List high-risk actions",
    description="Get list of actions that require passkey verification.",
)
async def list_high_risk_actions(auth: AuthContext = Depends(get_auth_context)):
    """Get all actions that require passkey verification."""
    from ..services.webauthn_provider import get_webauthn_provider

    webauthn = get_webauthn_provider()

    return {
        "actions": webauthn.get_high_risk_actions(),
        "count": len(webauthn.HIGH_RISK_ACTIONS),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DOM Bridge Endpoints (AWI Session Integration)
# ─────────────────────────────────────────────────────────────────────────────


class DOMAttachRequest(BaseModel):
    """Request to attach DOM bridge to an AWI session."""

    session_id: str
    target_url: str | None = None


class DOMAttachResponse(BaseModel):
    """Response after attaching DOM bridge."""

    status: str
    session_id: str
    dom_session_id: str
    elements_count: int
    page_type: str


@router.post(
    "/dom/attach",
    response_model=DOMAttachResponse,
    summary="Attach DOM bridge to AWI session",
    description="Attach a Playwright DOM bridge to an existing AWI session for live browser automation.",
)
async def attach_dom_bridge(
    request: DOMAttachRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Attach a Playwright DOM bridge to an existing AWI session.

    After attachment, all actions executed via POST /v1/awi/execute will
    be routed through the real browser via Playwright, enabling
    interaction with any human-facing website.

    This is the key to making AWI work with existing websites:
    - Agent sends semantic action (e.g., "search_and_sort")
    - Bridge translates to real DOM commands
    - Browser executes the commands
    - Bridge extracts resulting state as AWI representation
    """
    from ..services.awi_session import get_awi_session_manager

    await _require_awi_session_access(request.session_id, auth)

    manager = get_awi_session_manager()

    try:
        result = await manager.attach_dom_bridge(
            session_id=request.session_id,
            target_url=request.target_url,
        )
        return DOMAttachResponse(**result)
    except BrowserSessionLimitExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "dom_session_limit_exceeded", "message": str(e)},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "session_not_found", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Failed to attach DOM bridge: {request.session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "attachment_failed", "message": str(e)},
        )


@router.delete(
    "/dom/attach/{session_id}",
    summary="Detach DOM bridge from AWI session",
)
async def detach_dom_bridge(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Detach the Playwright DOM bridge from an AWI session.

    Returns the session to mock/internal AWI mode. The browser context is closed.
    """
    from ..services.awi_session import get_awi_session_manager

    await _require_awi_session_access(session_id, auth)

    manager = get_awi_session_manager()

    try:
        result = await manager.detach_dom_bridge(session_id)
        return result
    except Exception as e:
        logger.exception(f"Failed to detach DOM bridge: {session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "detachment_failed", "message": str(e)},
        )


@router.get(
    "/dom/attach/{session_id}/status",
    summary="Get DOM bridge status",
)
async def get_dom_bridge_status(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Check if a session has a DOM bridge attached.
    """
    from ..services.awi_session import get_awi_session_manager

    await _require_awi_session_access(session_id, auth)

    manager = get_awi_session_manager()

    status = await manager.get_dom_bridge_status(session_id)
    return status


@router.post(
    "/dom/session",
    response_model=DOMBridgeSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create DOM bridge session",
    description="Create a browser session for bidirectional DOM translation.",
)
async def create_dom_session(
    request: DOMBridgeSessionRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a new Playwright bridge session.

    The session opens a headless browser and navigates to the target URL.
    Use the returned session_id for subsequent DOM operations.
    """
    from ..services.awi_playwright_bridge import (
        BrowserSessionLimitExceeded,
        get_playwright_bridge,
    )

    if request.wallet_id:
        auth.require_wallet_access(request.wallet_id)
    else:
        auth.require_bootstrap_admin()

    bridge = get_playwright_bridge()

    try:
        session = await bridge.create_session(
            target_url=request.target_url,
            headless=request.headless,
            viewport=(request.viewport_width, request.viewport_height),
        )
        _DOM_SESSION_WALLETS[session.session_id] = request.wallet_id
        return DOMBridgeSessionResponse(
            session_id=session.session_id,
            current_url=session.current_url or request.target_url,
        )
    except BrowserSessionLimitExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "dom_session_limit_exceeded", "message": str(e)},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_target_url", "message": str(e)},
        )
    except Exception as e:
        logger.exception("Failed to create DOM session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "session_creation_failed", "message": str(e)},
        )


@router.delete(
    "/dom/session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Destroy DOM bridge session",
)
async def destroy_dom_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Destroy a Playwright bridge session and cleanup resources."""
    from ..services.awi_playwright_bridge import get_playwright_bridge

    await _require_dom_session_access(session_id, auth)

    bridge = get_playwright_bridge()

    destroyed = await bridge.destroy_session(session_id)
    _DOM_SESSION_WALLETS.pop(session_id, None)

    if not destroyed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )


@router.get(
    "/dom/sessions",
    summary="List DOM sessions",
    description="List all active DOM bridge sessions.",
)
async def list_dom_sessions(auth: AuthContext = Depends(get_auth_context)):
    """List all active DOM bridge sessions."""
    from ..services.awi_playwright_bridge import get_playwright_bridge

    bridge = get_playwright_bridge()

    sessions = await bridge.list_sessions()
    if not auth.is_bootstrap_admin:
        sessions = [
            s
            for s in sessions
            if _DOM_SESSION_WALLETS.get(s["session_id"]) == auth.wallet_id
        ]

    return {
        "sessions": sessions,
        "count": len(sessions),
    }


@router.post(
    "/dom/sync",
    summary="Execute AWI action via DOM",
    description=(
        "Execute an AWI action against real browser DOM via Playwright. "
        "Governed: requires X-Permit-Id and Idempotency-Key."
    ),
)
async def sync_dom(
    request: DOMSyncRequest,
    auth: AuthContext = Depends(get_auth_context),
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
):
    """
    Bidirectional AWI ↔ DOM translation.

    Translates the AWI action to Playwright commands, executes them,
    and returns the resulting state representation.
    """
    from ..services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
        consume_awi_http_replay,
        raise_awi_http_error,
    )
    from ..services.awi_playwright_bridge import get_playwright_bridge

    await _require_dom_session_access(request.session_id, auth)
    wallet_id = _DOM_SESSION_WALLETS.get(request.session_id)
    request_payload = request.model_dump(mode="json")
    gov = await begin_awi_http_governed(
        auth=auth,
        wallet_id=wallet_id,
        tool_name="awi_dom_sync",
        endpoint="POST /v1/awi/dom/sync",
        permit_id=x_permit_id,
        idempotency_key_lines=idempotency_key_lines,
        request_payload=request_payload,
    )
    replayed = consume_awi_http_replay(gov)
    if replayed is not None:
        return replayed

    bridge = get_playwright_bridge()

    try:
        commands = await bridge.translate_action(
            session_id=request.session_id,
            action=request.action,
            parameters=request.parameters,
        )

        gov.dispatch_started = True
        execution = await bridge.execute_commands(
            session_id=request.session_id,
            commands=commands,
        )

        representation = (
            await bridge.extract_state_representation(
                session_id=request.session_id,
                representation_type="summary",
                include_elements=True,
            )
            if execution.success
            else {}
        )

        body = DOMSyncResponse(
            session_id=request.session_id,
            execution_id=str(uuid4()),
            action=request.action,
            commands_generated=len(commands),
            commands_executed=execution.commands_executed,
            status="success" if execution.success else "error",
            effect_status=None if execution.success else "unknown",
            new_url=execution.new_url,
            state_representation=representation,
            error=execution.error,
        ).model_dump(mode="json")
        return await complete_awi_http_governed(
            gov,
            request_payload=request_payload,
            response_payload=body,
        )
    except HTTPException as exc:
        if gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=exc.status_code,
                detail=exc.detail
                if isinstance(exc.detail, dict)
                else {"error": "execution_failed"},
            )
        raise
    except ValueError as e:
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "translation_failed", "message": str(e)},
        )
    except Exception as e:
        logger.exception("DOM sync failed")
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "sync_failed", "message": str(e)},
        )


@router.get(
    "/dom/state/{session_id}",
    response_model=DOMStateResponse,
    summary="Get DOM state representation",
    description="Get current state of the DOM as an AWI representation.",
)
async def get_dom_state(
    session_id: str,
    representation_type: DOMRepresentationType = DOMRepresentationType.SUMMARY,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get current DOM state as AWI representation."""
    from ..services.awi_playwright_bridge import get_playwright_bridge

    await _require_dom_session_access(session_id, auth)

    bridge = get_playwright_bridge()

    session = await bridge.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    try:
        state = await bridge.extract_state_representation(
            session_id=session_id,
            representation_type=representation_type.value,
            include_elements=True,
        )

        return DOMStateResponse(
            session_id=session_id,
            url=state.get("url", ""),
            title=state.get("title", ""),
            representation_type=representation_type,
            page_type=state.get("page_type", "generic"),
            main_content=state.get("main_content", ""),
            interactive_elements=[],
            forms=state.get("forms", []),
            navigation=state.get("navigation", []),
        )

    except Exception as e:
        logger.exception(f"DOM state extraction failed: {session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "extraction_failed", "message": str(e)},
        )


@router.post(
    "/dom/preview",
    summary="Preview AWI action translation",
    description="Preview what commands will be generated for an action.",
)
async def preview_action(
    request: DOMSyncRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Preview commands without executing them."""
    from ..services.awi_playwright_bridge import get_playwright_bridge

    await _require_dom_session_access(request.session_id, auth)

    bridge = get_playwright_bridge()

    try:
        preview = await bridge.preview_action(
            session_id=request.session_id,
            action=request.action,
            parameters=request.parameters,
        )
        return preview
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "preview_failed", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Action preview failed: {request.session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "preview_error", "message": str(e)},
        )


# ─────────────────────────────────────────────────────────────────────────────
# RAG Memory Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/rag/index",
    status_code=status.HTTP_201_CREATED,
    summary="Index AWI session",
    description=(
        "Index a completed AWI session for future retrieval. "
        "Governed: requires X-Permit-Id and Idempotency-Key."
    ),
)
async def index_session(
    request: MemoryIndexRequest,
    auth: AuthContext = Depends(get_auth_context),
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
):
    """
    Index an AWI session for semantic search.

    Extracts entities, infers intent, and generates embeddings for retrieval.
    """
    from ..services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
        consume_awi_http_replay,
        raise_awi_http_error,
    )
    from ..services.awi_rag_engine import get_awi_rag_engine
    from ..services.awi_session import get_awi_session_manager

    await _require_awi_session_access(request.session_id, auth)
    session = await get_awi_session_manager().get_session(request.session_id)
    wallet_id = session.wallet_id if session else None
    request_payload = request.model_dump(mode="json")
    gov = await begin_awi_http_governed(
        auth=auth,
        wallet_id=wallet_id,
        tool_name="awi_memory_index",
        endpoint="POST /v1/awi/rag/index",
        permit_id=x_permit_id,
        idempotency_key_lines=idempotency_key_lines,
        request_payload=request_payload,
    )
    replayed = consume_awi_http_replay(gov)
    if replayed is not None:
        return replayed

    rag = get_awi_rag_engine()

    try:
        gov.dispatch_started = True
        memory_id = await rag.index_session(
            session_id=request.session_id,
            session_type=request.session_type,
            action_history=request.action_history,
            state_snapshots=request.state_snapshots,
            metadata=request.metadata,
            owner_wallet_id=wallet_id,
        )

        memory = await rag.get_memory(memory_id)

        body = MemoryIndexResponse(
            memory_id=memory_id,
            session_id=request.session_id,
            indexed_at=memory.created_at if memory else utc_now(),
            entities_extracted=len(memory.key_entities) if memory else 0,
            intent_inferred=memory.user_intent if memory else "",
        ).model_dump(mode="json")
        return await complete_awi_http_governed(
            gov,
            request_payload=request_payload,
            response_payload=body,
        )

    except HTTPException as exc:
        if gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=exc.status_code,
                detail=exc.detail
                if isinstance(exc.detail, dict)
                else {"error": "execution_failed"},
            )
        raise
    except Exception as e:
        logger.exception(f"Failed to index session: {request.session_id}")
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "indexing_failed", "message": str(e)},
        )


@router.post(
    "/rag/query",
    summary="Query session memories",
    description=(
        "Semantic search over past AWI session memories. "
        "Governed: requires X-Permit-Id, Idempotency-Key, and X-Wallet-Id."
    ),
)
async def query_memories(
    request: RAGQueryRequest,
    auth: AuthContext = Depends(get_auth_context),
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
    x_wallet_id: str | None = Header(None, alias="X-Wallet-Id"),
):
    """
    Query session memories with natural language.

    Returns relevant past sessions sorted by similarity to the query.
    """
    from ..services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
        consume_awi_http_replay,
        raise_awi_http_error,
    )
    from ..services.awi_rag_engine import get_awi_rag_engine
    from ..services.awi_session import get_awi_session_manager

    request_payload = request.model_dump(mode="json")
    gov = await begin_awi_http_governed(
        auth=auth,
        wallet_id=x_wallet_id,
        tool_name="awi_rag_query",
        endpoint="POST /v1/awi/rag/query",
        permit_id=x_permit_id,
        idempotency_key_lines=idempotency_key_lines,
        request_payload=request_payload,
    )
    replayed = consume_awi_http_replay(gov)
    if replayed is not None:
        return replayed

    rag = get_awi_rag_engine()
    sessions = get_awi_session_manager()

    try:
        start_time = utc_now()

        # Scope by owner inside the search, before scoring and top_k truncation:
        # filtering only afterwards let other tenants' better matches empty the
        # caller's results and bumped their memories' access counters.
        gov.dispatch_started = True
        results = await rag.search(
            query=request.query,
            session_type=request.session_type,
            top_k=request.top_k,
            similarity_threshold=request.similarity_threshold,
            include_raw_state=request.include_raw_state,
            owner_wallet_ids=_rag_owner_scope(gov.wallet_id, auth),
        )

        # Tenant isolation (defense in depth): only return memories for sessions
        # owned by this wallet.
        scoped: list[Any] = []
        for r in results:
            session = await sessions.get_session(r.session_id)
            if session is None:
                continue
            if session.wallet_id and session.wallet_id != gov.wallet_id:
                continue
            if not session.wallet_id and not auth.is_bootstrap_admin:
                continue
            scoped.append(r)

        search_time_ms = int((utc_now() - start_time).total_seconds() * 1000)

        body = RAGQueryResponse(
            query=request.query,
            results=[
                MemorySearchResult(
                    memory_id=r.memory_id,
                    session_id=r.session_id,
                    session_type=r.session_type,
                    user_intent=r.user_intent,
                    action_sequence=r.action_sequence,
                    key_entities=r.key_entities,
                    similarity_score=r.similarity_score,
                    created_at=r.created_at,
                    accessed_at=r.accessed_at,
                    access_count=r.access_count,
                    raw_state=r.raw_state if request.include_raw_state else None,
                )
                for r in scoped
            ],
            total_found=len(scoped),
            search_time_ms=search_time_ms,
        ).model_dump(mode="json")
        return await complete_awi_http_governed(
            gov,
            request_payload=request_payload,
            response_payload=body,
        )
    except HTTPException as exc:
        if gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=exc.status_code,
                detail=exc.detail
                if isinstance(exc.detail, dict)
                else {"error": "execution_failed"},
            )
        raise
    except Exception as e:
        logger.exception("RAG query failed")
        await raise_awi_http_error(
            gov,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "query_failed", "message": str(e)},
        )


@router.get(
    "/rag/memory/{memory_id}",
    summary="Get memory details",
    description="Get detailed information about a specific memory.",
)
async def get_memory(
    memory_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get a specific memory by ID."""
    memory = await _load_owned_memory(memory_id, auth)

    return {
        "memory_id": memory.memory_id,
        "session_id": memory.session_id,
        "session_type": memory.session_type,
        "user_intent": memory.user_intent,
        "action_sequence": memory.action_sequence,
        "key_entities": memory.key_entities,
        "page_summaries": memory.page_summaries,
        "relevance_tags": memory.relevance_tags,
        "created_at": memory.created_at.isoformat(),
        "accessed_at": memory.accessed_at.isoformat(),
        "access_count": memory.access_count,
    }


@router.delete(
    "/rag/memory/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete memory",
    description="Delete a memory from the index.",
)
async def delete_memory(
    memory_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Delete a specific memory."""
    from ..services.awi_rag_engine import get_awi_rag_engine

    await _load_owned_memory(memory_id, auth)

    deleted = await get_awi_rag_engine().delete_memory(memory_id)

    if not deleted:
        raise _not_found("Memory", memory_id)


@router.get(
    "/rag/context/{session_id}",
    response_model=SessionContextResponse,
    summary="Get session context",
    description="Get relevant context from past sessions for the current session.",
)
async def get_session_context(
    session_id: str,
    session_type: str | None = None,
    top_k: int = 3,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Get relevant context from past sessions.

    Returns similar past sessions and suggested next actions to help
    the agent make informed decisions.
    """
    from ..services.awi_rag_engine import get_awi_rag_engine
    from ..services.awi_session import get_awi_session_manager

    rag = get_awi_rag_engine()
    session_manager = get_awi_session_manager()

    await _require_awi_session_access(session_id, auth)

    session = await session_manager.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    try:
        current_state = {
            "url": session.target_url,
            "goal": "",
        }

        # Context for a session draws only on memories of that session's own
        # wallet (or, for an ownerless admin session, ownerless memories).
        context = await rag.get_session_context(
            current_session_id=session_id,
            current_state=current_state,
            session_type=session_type,
            top_k=top_k,
            owner_wallet_ids={session.wallet_id},
        )

        return SessionContextResponse(**context)

    except Exception as e:
        logger.exception(f"Context retrieval failed: {session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "context_failed", "message": str(e)},
        )


@router.get(
    "/rag/stats",
    summary="Get RAG statistics",
    description="Get statistics about the memory store.",
)
async def get_rag_stats(auth: AuthContext = Depends(get_auth_context)):
    """Get statistics about indexed memories."""
    from ..services.awi_rag_engine import get_awi_rag_engine

    auth.require_bootstrap_admin()

    rag = get_awi_rag_engine()

    stats = await rag.get_stats()

    return stats


@router.get(
    "/rag/sessions/{session_id}/memories",
    summary="Get session memories",
    description="Get all indexed memories for a session.",
)
async def get_session_memories(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get all memories for a specific session."""
    from ..services.awi_rag_engine import get_awi_rag_engine

    await _require_awi_session_access(session_id, auth)

    rag = get_awi_rag_engine()

    memories = await rag.get_session_memories(session_id)

    return {
        "session_id": session_id,
        "memories": [
            {
                "memory_id": m.memory_id,
                "session_type": m.session_type,
                "user_intent": m.user_intent,
                "action_count": len(m.action_sequence),
                "entity_count": len(m.key_entities),
                "created_at": m.created_at.isoformat(),
            }
            for m in memories
        ],
        "count": len(memories),
    }


@router.delete(
    "/rag/sessions/{session_id}/memories",
    summary="Delete session memories",
    description="Delete all memories for a session.",
)
async def delete_session_memories(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Delete all memories for a specific session."""
    from ..services.awi_rag_engine import get_awi_rag_engine

    await _require_awi_session_access(session_id, auth)

    rag = get_awi_rag_engine()

    deleted = await rag.delete_session_memories(session_id)

    return {
        "session_id": session_id,
        "deleted_count": deleted,
    }
