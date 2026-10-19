"""Trust-plane facade: signed, bounded authority (permits).

Re-exports the canonical permit implementation from :mod:`app.services.permits`.
"""

from __future__ import annotations

from app.services.permits import (
    PermitCreationRejectedError,
    PermitError,
    PermitService,
    PermitValidation,
    PermitWriteContendedError,
    extract_recipient_identity,
    get_permit_service,
    permit_constraints_snapshot,
    permit_model_to_response,
    recipient_binding_matches,
)

__all__ = [
    "PermitCreationRejectedError",
    "PermitError",
    "PermitService",
    "PermitValidation",
    "PermitWriteContendedError",
    "extract_recipient_identity",
    "get_permit_service",
    "permit_constraints_snapshot",
    "permit_model_to_response",
    "recipient_binding_matches",
]
