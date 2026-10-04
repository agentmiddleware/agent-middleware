from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import AuthContext, get_auth_context
from app.schemas.policies import (
    PolicyBundleCreate,
    PolicyBundleListResponse,
    PolicyBundlePatch,
    PolicyBundleResponse,
)
from app.services.policies import (
    create_policy_bundle,
    get_policy_bundle,
    list_policy_bundles,
    patch_policy_bundle,
)

router = APIRouter(prefix="/v1/policies", tags=["Policy Bundles"])


@router.post(
    "", response_model=PolicyBundleResponse, status_code=status.HTTP_201_CREATED
)
async def create_policy(
    request: PolicyBundleCreate,
    auth: AuthContext = Depends(get_auth_context),
) -> PolicyBundleResponse:
    auth.require_bootstrap_admin()
    return await create_policy_bundle(request)


@router.get("", response_model=PolicyBundleListResponse)
async def list_policies(
    wallet_id: str | None = Query(None),
    auth: AuthContext = Depends(get_auth_context),
) -> PolicyBundleListResponse:
    auth.require_bootstrap_admin()
    policies = await list_policy_bundles(wallet_id=wallet_id)
    return PolicyBundleListResponse(policies=policies, total=len(policies))


@router.get("/{policy_id}", response_model=PolicyBundleResponse)
async def get_policy(
    policy_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> PolicyBundleResponse:
    auth.require_bootstrap_admin()
    policy = await get_policy_bundle(policy_id)
    if not policy:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "policy_not_found",
                "message": f"Policy {policy_id} not found",
            },
        )
    return policy


@router.patch("/{policy_id}", response_model=PolicyBundleResponse)
async def patch_policy(
    policy_id: str,
    request: PolicyBundlePatch,
    auth: AuthContext = Depends(get_auth_context),
) -> PolicyBundleResponse:
    auth.require_bootstrap_admin()
    policy = await patch_policy_bundle(policy_id, request)
    if not policy:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "policy_not_found",
                "message": f"Policy {policy_id} not found",
            },
        )
    return policy
