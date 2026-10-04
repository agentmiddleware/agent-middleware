"""CrewAI integration for Agent Middleware API."""

from .client import B2AClient
from .tool import CrewAIB2ATool

__all__ = ["B2AClient", "CrewAIB2ATool"]
__version__ = "0.1.0"
