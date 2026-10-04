"""AutoGen integration for Agent Middleware API."""

from .client import B2AClient
from .tool import B2AFunctionTool, register_b2a_tools

__all__ = ["B2AClient", "B2AFunctionTool", "register_b2a_tools"]
__version__ = "0.1.0"
