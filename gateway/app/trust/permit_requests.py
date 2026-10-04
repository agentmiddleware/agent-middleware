"""Trust-plane facade: permit requests (agent asks, human approves, we mint).

Re-exports the canonical implementation from
:mod:`app.services.permit_requests`, plus the shared approver-card renderers
the request surfaces render from.
"""

from __future__ import annotations

from app.services.approval_card import (
    ApprovalCardView,
    card_fragment_html,
    render_email_html,
    render_page_html,
    render_text_summary,
)
from app.services.permit_requests import (
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_EXPIRED,
    REQUEST_STATUS_FAILED,
    REQUEST_STATUS_MINTING,
    REQUEST_STATUS_PENDING,
    REQUEST_STATUS_REJECTED,
    PermitRequestConflictError,
    PermitRequestError,
    PermitRequestService,
    card_view,
    get_permit_request_service,
    permit_request_hash,
    poll_url,
    request_to_response,
)

__all__ = [
    "ApprovalCardView",
    "PermitRequestConflictError",
    "PermitRequestError",
    "PermitRequestService",
    "REQUEST_STATUS_APPROVED",
    "REQUEST_STATUS_EXPIRED",
    "REQUEST_STATUS_FAILED",
    "REQUEST_STATUS_MINTING",
    "REQUEST_STATUS_PENDING",
    "REQUEST_STATUS_REJECTED",
    "card_fragment_html",
    "card_view",
    "get_permit_request_service",
    "permit_request_hash",
    "poll_url",
    "request_to_response",
    "render_email_html",
    "render_page_html",
    "render_text_summary",
]
