"""
PROOF SURFACE — frozen. Do not expand; see docs/PROOF_SURFACES.md.
Behavioral Sandbox Router — Phase 6
===================================
Endpoints for creating and managing behavioral sandbox environments.

Allows agents to test real tool execution in isolated environments
when the selected backend provides isolation. Host Python subprocess execution
is disabled by default and is not a production sandbox boundary.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..core.auth import AuthContext, get_auth_context
from ..core.config import get_settings
from ..schemas.sandbox_behavioral import (
    SandboxEnvironment,
    SandboxEnvironmentCreate,
    ToolExecutionRequest,
    ToolExecutionResponse,
)
from ..services.behavioral_sandbox import get_behavioral_sandbox
from ..services.governance import record_governed_action

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/v1/sandbox/behavioral",
    tags=["Sandbox"],
)


@router.post(
    "/environments",
    response_model=SandboxEnvironment,
    status_code=status.HTTP_201_CREATED,
    summary="Create sandbox environment",
    description="Create an isolated sandbox environment for tool testing.",
)
async def create_environment(
    request: SandboxEnvironmentCreate,
    http_request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a new behavioral sandbox environment.

    The environment provides dry-run and mocked execution for testing tools
    without affecting production systems. Each environment has its own Redis
    namespace for state isolation when Redis is available.
    """
    # Bind the environment to the caller's wallet when they don't specify one,
    # so ownership is well-defined for the get/destroy access checks below. A
    # caller may only create an environment for its own wallet.
    if request.wallet_id is None:
        request.wallet_id = auth.wallet_id
    else:
        auth.require_wallet_access(request.wallet_id)
    try:
        engine = get_behavioral_sandbox()
        env = await engine.create_environment(request)
        await record_governed_action(
            event="sandbox.behavioral.environment.create",
            auth=auth,
            wallet_id=env.wallet_id or auth.wallet_id,
            target="sandbox",
            endpoint="/v1/sandbox/behavioral/environments",
            request_id=http_request.headers.get("X-Request-ID"),
            ok=True,
            metadata={
                "env_id": env.env_id,
                "environment_type": env.environment_type,
                "network_access": env.network_access,
            },
        )
        return env
    except Exception as e:
        logger.exception("Failed to create sandbox environment")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "creation_failed", "message": str(e)},
        )


@router.get(
    "/environments/{env_id}",
    summary="Get environment state",
    description="Get the current state of a sandbox environment.",
)
async def get_environment(
    env_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get the current state of a sandbox environment."""
    try:
        engine = get_behavioral_sandbox()
        found, owner = await engine.get_environment_owner(env_id)
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "message": f"Environment {env_id} not found",
                },
            )
        auth.require_wallet_access(owner)
        return await engine.get_environment_state(env_id)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Failed to get environment state: {env_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "fetch_failed", "message": str(e)},
        )


@router.delete(
    "/environments/{env_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Destroy environment",
    description="Destroy a sandbox environment and release resources.",
)
async def destroy_environment(
    env_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """Destroy a sandbox environment."""
    try:
        engine = get_behavioral_sandbox()
        found, owner = await engine.get_environment_owner(env_id)
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "message": f"Environment {env_id} not found",
                },
            )
        auth.require_wallet_access(owner)
        destroyed = await engine.destroy_environment(env_id)
        if not destroyed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "message": f"Environment {env_id} not found",
                },
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to destroy environment: {env_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "destruction_failed", "message": str(e)},
        )


@router.post(
    "/execute",
    response_model=ToolExecutionResponse,
    summary="Execute tool in sandbox",
    description="Execute a tool within an isolated sandbox environment.",
)
async def execute_tool(
    request: ToolExecutionRequest,
    http_request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Execute a tool within a sandbox environment.

    The tool can be:
    - Python code (dry-run by default; unsafe host subprocess mode requires opt-in)
    - MCP tool (sandboxed mode)
    - HTTP request (proxy mode)

    Use `dry_run=true` to simulate execution without side effects.
    """
    try:
        engine = get_behavioral_sandbox()
        found, owner = await engine.get_environment_owner(request.env_id)
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "message": f"Environment {request.env_id} not found",
                },
            )
        auth.require_wallet_access(owner)
        result = await engine.execute_tool(request)
        settings = get_settings()
        await record_governed_action(
            event="sandbox.execute",
            auth=auth,
            wallet_id=auth.wallet_id,
            target="sandbox",
            endpoint="/v1/sandbox/behavioral/execute",
            request_id=http_request.headers.get("X-Request-ID"),
            estimated_cost=result.cost_estimate,
            ok=result.error is None,
            error=result.error,
            metadata={
                "env_id": request.env_id,
                "tool_name": request.tool_name,
                "dry_run": request.dry_run,
                "status": result.status,
                "backend_mode": settings.BEHAVIORAL_SANDBOX_PYTHON_BACKEND,
                "allow_unsafe_host_python": settings.ALLOW_UNSAFE_HOST_PYTHON_SANDBOX,
                "resources_used": result.resources_used,
            },
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": str(e)},
        )
    except Exception as e:
        logger.exception(f"Sandbox execution failed: {request.env_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "execution_failed", "message": str(e)},
        )


@router.post(
    "/execute/{env_id}/{tool_name}",
    response_model=ToolExecutionResponse,
    summary="Execute tool by ID",
    description="Execute a named tool in a specific environment.",
)
async def execute_tool_by_name(
    http_request: Request,
    env_id: str,
    tool_name: str,
    tool_input: dict | None = None,
    dry_run: bool = False,
    auth: AuthContext = Depends(get_auth_context),
):
    """Execute a tool by name in a specific environment."""
    request = ToolExecutionRequest(
        env_id=env_id,
        tool_name=tool_name,
        tool_input=tool_input or {},
        dry_run=dry_run,
    )
    return await execute_tool(request, http_request, auth)
