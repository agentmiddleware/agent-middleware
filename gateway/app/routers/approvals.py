"""Local-only human approval endpoints (AMW V2).

A human on the gateway host approves or denies paused irreversible tool calls
here (or via ``gateway/scripts/amw_approvals.py``). Every endpoint rejects
non-loopback clients with 403 and requires a bootstrap admin (operator) API
key. This router is intentionally not mounted in ``main.py`` yet; mounting
lands with the ``mcp_standard.py`` hook in a later PR.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.auth import AuthContext, get_auth_context
from app.services.approval_gate import ApprovalStore, DbApprovalStore

router = APIRouter(prefix="/v1/approvals", tags=["Approvals"])

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def require_loopback(request: Request) -> None:
    """Allow only clients on the gateway host itself."""
    host = request.client.host if request.client else ""
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="approvals are local-only")


_store: ApprovalStore = DbApprovalStore()


def get_approval_store() -> ApprovalStore:
    return _store


def set_approval_store_for_tests(store: ApprovalStore) -> None:
    global _store
    _store = store


class PendingApproval(BaseModel):
    nonce: str
    tool: str
    principal: str
    args_hash: str
    created_at: float
    deadline: float


class PendingApprovalsResponse(BaseModel):
    approvals: list[PendingApproval]


class DecisionResponse(BaseModel):
    nonce: str
    status: str


@router.get("/pending", response_model=PendingApprovalsResponse)
async def list_pending(
    _local: None = Depends(require_loopback),
    auth: AuthContext = Depends(get_auth_context),
) -> PendingApprovalsResponse:
    auth.require_bootstrap_admin()
    approvals = [
        PendingApproval(
            nonce=record.nonce,
            tool=record.tool,
            principal=record.principal,
            args_hash=record.args_hash,
            created_at=record.created_at,
            deadline=record.deadline,
        )
        for record in await get_approval_store().alist_pending(now=time.time())
    ]
    return PendingApprovalsResponse(approvals=approvals)


@router.post("/{nonce}/approve", response_model=DecisionResponse)
async def approve(
    nonce: str,
    _local: None = Depends(require_loopback),
    auth: AuthContext = Depends(get_auth_context),
) -> DecisionResponse:
    auth.require_bootstrap_admin()
    record = await get_approval_store().adecide(nonce, True, now=time.time())
    if record is None:
        raise HTTPException(status_code=404, detail="approval_not_found")
    return DecisionResponse(nonce=record.nonce, status=record.status)


@router.post("/{nonce}/deny", response_model=DecisionResponse)
async def deny(
    nonce: str,
    _local: None = Depends(require_loopback),
    auth: AuthContext = Depends(get_auth_context),
) -> DecisionResponse:
    auth.require_bootstrap_admin()
    record = await get_approval_store().adecide(nonce, False, now=time.time())
    if record is None:
        raise HTTPException(status_code=404, detail="approval_not_found")
    return DecisionResponse(nonce=record.nonce, status=record.status)
