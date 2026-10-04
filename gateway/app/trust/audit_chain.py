"""Trust-plane facade: tamper-evident audit chain.

Re-exports the canonical audit-chain verification and signing helpers from
:mod:`app.services.audit_chain`, plus the high-level event recorder from
:mod:`app.services.audit_log`.
"""

from __future__ import annotations

from app.services.audit_chain import (
    AuditChainContendedError,
    AuditChainVerification,
    AuditEventConflictError,
    audit_payload,
    sign_audit_model,
    verify_audit_chain,
)
from app.services.audit_log import (
    count_audit_events,
    count_audit_events_grouped,
    list_audit_events,
    record_audit_event,
    summarize_audit_events,
)

__all__ = [
    "AuditChainContendedError",
    "AuditChainVerification",
    "AuditEventConflictError",
    "audit_payload",
    "sign_audit_model",
    "verify_audit_chain",
    "record_audit_event",
    "list_audit_events",
    "count_audit_events",
    "count_audit_events_grouped",
    "summarize_audit_events",
]
