"""OpenAI integration for Agent Middleware API.

The model's own ``tool_call.id`` is the operation identity: it is assigned
once per tool call, lives in the conversation transcript, and therefore
survives every retry of that call. :class:`GovernedToolRunner` derives the
trust plane's ``Idempotency-Key`` from it, persists the derivation before the
first network call, and never invents a key of its own.
"""

from .client import B2AClient
from .runner import (
    GovernedToolResult,
    GovernedToolRunner,
    InMemoryOperationKeyStore,
    JsonFileOperationKeyStore,
    OperationKeyStore,
    OperationRecord,
    PermitRecord,
    ToolCall,
    normalize_tool_call,
)

__all__ = [
    "B2AClient",
    "GovernedToolResult",
    "GovernedToolRunner",
    "InMemoryOperationKeyStore",
    "JsonFileOperationKeyStore",
    "OperationKeyStore",
    "OperationRecord",
    "PermitRecord",
    "ToolCall",
    "normalize_tool_call",
]
__version__ = "0.1.0"
