"""AutoGen integration for Agent Middleware API."""

from ._permit_store import JsonFilePermitStore, PermitStore
from .client import B2AClient
from .tool import B2AFunctionTool, register_b2a_tools

__all__ = [
    "B2AClient",
    "B2AFunctionTool",
    "JsonFilePermitStore",
    "PermitStore",
    "register_b2a_tools",
]
__version__ = "0.1.0"
