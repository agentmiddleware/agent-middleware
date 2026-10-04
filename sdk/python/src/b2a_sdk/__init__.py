"""Python client for governed Agent Middleware tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Dependency-minimal surface, imported eagerly: errors, models, and the offline
# receipt verifier need only the standard library plus ``cryptography`` (loaded
# lazily inside the verifier itself). Keeping these free of the HTTP stack is
# what lets ``python -m b2a_sdk.verify_cli --keys ...`` and
# ``from b2a_sdk import verify_bundle`` run with no networking library
# installed — the whole point of offline receipt verification.
from .errors import (
    AgentMiddlewareError,
    APIError,
    AuthenticationError,
    AuthorizationError,
    DeliveryUncertainError,
    IdempotencyConflictError,
    InsufficientFundsError,
    PermitDeniedError,
    TransportError,
)
from .models import (
    ACPCheckoutRequest,
    ACPCheckoutResponse,
    ACPLineItem,
    EvidenceBundle,
    InvocationResult,
    Permit,
    PermitRequest,
    Receipt,
    ReceiptVerification,
    ToolDefinition,
)
from .receipt_verifier import (
    VerificationError,
    VerificationResult,
    VerificationStatus,
    key_set_from_document,
    verify_bundle,
)

__version__ = "0.5.0"

# The HTTP client surface (client, edge_client, and the decorators that wrap it)
# pulls in httpx. Load those names lazily via PEP 562 so importing the package —
# or running the offline verifier — does not require httpx to be installed.
# Accessing one of these names imports its backing module on first use, which
# still raises the normal ImportError for httpx only when httpx is genuinely
# needed and absent.
_LAZY_ATTRS = {
    "AgentMiddlewareClient": "client",
    "B2AClient": "client",
    "B2AEdgeClient": "edge_client",
    "GovernedEdgeSession": "edge_client",
    "LocalDecision": "edge_client",
    "LocalPermitValidator": "edge_client",
    "X402Client": "x402",
    "billable": "decorators",
    "combined": "decorators",
    "monitored": "decorators",
    "parse_402_response": "x402",
}

if TYPE_CHECKING:  # let type checkers and IDEs resolve the lazy names statically
    from .client import AgentMiddlewareClient, B2AClient
    from .decorators import billable, combined, monitored
    from .edge_client import (
        B2AEdgeClient,
        GovernedEdgeSession,
        LocalDecision,
        LocalPermitValidator,
    )
    from .x402 import X402Client, parse_402_response


def __getattr__(name: str) -> object:
    module_name = _LAZY_ATTRS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache so later access is a plain attribute lookup
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "ACPCheckoutRequest",
    "ACPCheckoutResponse",
    "ACPLineItem",
    "APIError",
    "AgentMiddlewareClient",
    "AgentMiddlewareError",
    "AuthenticationError",
    "AuthorizationError",
    "B2AClient",
    "B2AEdgeClient",
    "DeliveryUncertainError",
    "EvidenceBundle",
    "GovernedEdgeSession",
    "IdempotencyConflictError",
    "InsufficientFundsError",
    "InvocationResult",
    "LocalDecision",
    "LocalPermitValidator",
    "Permit",
    "PermitDeniedError",
    "PermitRequest",
    "Receipt",
    "ReceiptVerification",
    "ToolDefinition",
    "TransportError",
    "VerificationError",
    "VerificationResult",
    "VerificationStatus",
    "X402Client",
    "billable",
    "combined",
    "monitored",
    "key_set_from_document",
    "parse_402_response",
    "verify_bundle",
]
