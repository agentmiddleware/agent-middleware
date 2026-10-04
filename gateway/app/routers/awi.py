"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
AWI Router — Phase 7
====================
Agentic Web Interface endpoints + MCP translation layer.

Based on arXiv:2506.10953v1 - "Build the web for agents, not agents for the web"

Provides standardized, stateful interfaces for web agents with:
- Higher-level unified actions (not DOM clicks)
- Progressive information transfer
- Agentic task queues with human pause/steer
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status

from ..core.auth import AuthContext, get_auth_context
from ..schemas.awi import (
    AWIHumanIntervention,
    AWIRepresentationRequest,
    AWIRepresentationResponse,
    AWIExecutionRequest,
    AWISession,
    AWISessionCreate,
    AWITask,
    AWITaskCreate,
    AWITaskQueueStatus,
    AWIActionCategory,
)
from ..services.awi_action_vocab import get_awi_vocabulary
from ..services.awi_session import get_awi_session_manager
from ..services.awi_task_queue import get_awi_task_queue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/awi", tags=["AWI"])


async def _require_session_access(session_id: str, auth: AuthContext) -> AWISession:
    """Authorize access to an AWI session before exposing or mutating state."""
    manager = get_awi_session_manager()
    session = await manager.get_session(session_id)
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "message": f"Session {session_id} not found"},
    )

    if not session:
        raise not_found

    # A session the caller may not see answers exactly like a missing one. The
    # wallet check's 403 names the *owning* wallet, so raising it after a 404
    # for unknown ids let any wallet-scoped key confirm a session id exists and
    # learn which wallet owns it.
    try:
        if session.wallet_id:
            auth.require_wallet_access(session.wallet_id)
        else:
            auth.require_bootstrap_admin()
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise not_found from None
        raise

    return session


@router.post(
    "/sessions",
    response_model=AWISession,
    status_code=status.HTTP_201_CREATED,
    summary="Create AWI session",
    description=(
        "Create a stateful AWI session for agentic web interactions. "
        "Based on arXiv:2506.10953v1 - Stateful interfaces for web agents."
    ),
)
async def create_session(
    request: AWISessionCreate,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a new Agentic Web Interface (AWI) session.

    Sessions provide stateful context for web agent interactions,
    enabling higher-level unified actions instead of raw DOM manipulation.
    """
    if request.wallet_id:
        auth.require_wallet_access(request.wallet_id)
    else:
        auth.require_bootstrap_admin()

    try:
        manager = get_awi_session_manager()
        return await manager.create_session(request)
    except Exception as e:
        logger.exception("Failed to create AWI session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "creation_failed", "message": str(e)},
        )


@router.get(
    "/sessions/{session_id}",
    summary="Get AWI session",
    description="Get the current state of an AWI session.",
)
async def get_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get the current state of an AWI session."""
    return await _require_session_access(session_id, auth)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Destroy AWI session",
    description="Destroy an AWI session and release resources.",
)
async def destroy_session(
    session_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Destroy an AWI session."""
    await _require_session_access(session_id, auth)
    manager = get_awi_session_manager()
    destroyed = await manager.destroy_session(session_id)

    if not destroyed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )


@router.post(
    "/execute",
    summary="Execute AWI action",
    description=(
        "Execute a standardized AWI action within a session. "
        "Governed: requires X-Permit-Id and Idempotency-Key."
    ),
)
async def execute_action(
    request: AWIExecutionRequest,
    auth: AuthContext = Depends(get_auth_context),
    x_permit_id: str | None = Header(None, alias="X-Permit-Id"),
    idempotency_key_lines: list[str] | None = Header(None, alias="Idempotency-Key"),
):
    """
    Execute a standardized AWI action.

    Actions use semantic vocabulary (search_and_sort, add_to_cart) instead
    of raw DOM manipulation, making agents more robust and website-independent.
    Governed: requires ``X-Permit-Id`` + ``Idempotency-Key`` (tool ``awi_execute``).
    """
    from ..services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
        consume_awi_http_replay,
        raise_awi_http_error,
    )

    gov = None
    try:
        session = await _require_session_access(request.session_id, auth)
        request_payload = request.model_dump(mode="json")
        gov = await begin_awi_http_governed(
            auth=auth,
            wallet_id=session.wallet_id,
            tool_name="awi_execute",
            endpoint="POST /v1/awi/execute",
            permit_id=x_permit_id,
            idempotency_key_lines=idempotency_key_lines,
            request_payload=request_payload,
        )
        replayed = consume_awi_http_replay(gov)
        if replayed is not None:
            return replayed

        manager = get_awi_session_manager()
        gov.dispatch_started = True
        result = await manager.execute_action(request)
        body = (
            result.model_dump(mode="json")
            if hasattr(result, "model_dump")
            else dict(result)
        )
        return await complete_awi_http_governed(
            gov,
            request_payload=request_payload,
            response_payload=body,
        )
    except HTTPException as exc:
        if gov is not None and gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=exc.status_code,
                detail=exc.detail
                if isinstance(exc.detail, dict)
                else {"error": "execution_failed"},
            )
        raise
    except Exception as e:
        logger.exception(f"AWI execution failed: {request.session_id}")
        detail = {"error": "execution_failed", "message": str(e)}
        if gov is not None and gov.replay_response is None:
            await raise_awi_http_error(
                gov,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail,
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        )


@router.post(
    "/represent",
    response_model=AWIRepresentationResponse,
    summary="Request representation",
    description="Request a specific representation of the current session state.",
)
async def request_representation(
    request: AWIRepresentationRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Request a specific representation of the session state.

    Agents can request exactly the information they need (summary, embedding,
    low-res screenshot, etc.) instead of receiving full DOM every time.
    """
    try:
        await _require_session_access(request.session_id, auth)
        manager = get_awi_session_manager()
        result = await manager.request_representation(request)

        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "message": f"Session {request.session_id} not found",
                },
            )

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Representation request failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "representation_failed", "message": str(e)},
        )


@router.post(
    "/intervene",
    summary="Human intervention",
    description="Pause, resume, or steer an AWI session (human-in-the-loop).",
)
async def human_intervention(
    intervention: AWIHumanIntervention,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Human intervention in an AWI session.

    Allows humans to pause, resume, or steer agent actions for safety
    and alignment with human preferences.
    """
    try:
        await _require_session_access(intervention.session_id, auth)
        manager = get_awi_session_manager()
        return await manager.human_intervention(intervention, auth=auth)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Human intervention failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "intervention_failed", "message": str(e)},
        )


@router.get(
    "/vocabulary",
    summary="List AWI actions",
    description="List all standardized AWI actions.",
)
async def list_actions():
    """List all standardized AWI actions."""
    vocabulary = get_awi_vocabulary()
    actions = vocabulary.list_all_actions()

    return {
        "actions": [a.to_public_dict() for a in actions],
        "categories": [c.value for c in AWIActionCategory],
    }


@router.get(
    "/vocabulary/category/{category}",
    summary="List actions by category",
    description="List AWI actions in a specific category.",
)
async def list_actions_by_category(category: AWIActionCategory):
    """List AWI actions by category."""
    vocabulary = get_awi_vocabulary()
    actions = vocabulary.list_actions_by_category(category)

    return {
        "category": category.value,
        "actions": [a.to_public_dict() for a in actions],
    }


@router.post(
    "/tasks",
    response_model=AWITask,
    status_code=status.HTTP_201_CREATED,
    summary="Create AWI task",
    description="Create a new AWI task in the queue.",
)
async def create_task(
    request: AWITaskCreate,
    auth: AuthContext = Depends(get_auth_context),
):
    """Create a new AWI task in the queue."""
    auth.require_bootstrap_admin()
    try:
        queue = get_awi_task_queue()
        return await queue.create_task(request)
    except Exception as e:
        logger.exception("Failed to create AWI task")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "task_creation_failed", "message": str(e)},
        )


@router.get(
    "/tasks/{task_id}",
    summary="Get task status",
    description="Get the status of an AWI task.",
)
async def get_task(
    task_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get the status of an AWI task."""
    auth.require_bootstrap_admin()
    queue = get_awi_task_queue()
    task = await queue.get_task(task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Task {task_id} not found"},
        )

    return task


@router.get(
    "/queue/status",
    response_model=AWITaskQueueStatus,
    summary="Get queue status",
    description="Get current status of the AWI task queue.",
)
async def get_queue_status(auth: AuthContext = Depends(get_auth_context)):
    """Get current status of the AWI task queue."""
    auth.require_bootstrap_admin()
    queue = get_awi_task_queue()
    return await queue.get_queue_status()


@router.post(
    "/queue/pause",
    summary="Global pause",
    description="Pause all tasks globally (human intervention).",
)
async def global_pause(
    reason: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Pause all AWI tasks globally."""
    auth.require_bootstrap_admin()
    queue = get_awi_task_queue()
    await queue.global_pause(reason)
    return {"success": True, "status": "paused", "reason": reason}


@router.post(
    "/queue/resume",
    summary="Global resume",
    description="Resume all AWI tasks after global pause.",
)
async def global_resume(auth: AuthContext = Depends(get_auth_context)):
    """Resume all AWI tasks after global pause."""
    auth.require_bootstrap_admin()
    queue = get_awi_task_queue()
    await queue.global_resume()
    return {"success": True, "status": "resumed"}
