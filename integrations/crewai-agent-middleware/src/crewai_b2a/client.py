"""B2A Client for CrewAI integration.

Thin subclass of ``b2a_sdk.AgentMiddlewareClient``. All HTTP plumbing lives in the
shared base; this module exists so framework-specific helpers can hang off a
CrewAI-flavored client name.
"""

from b2a_sdk import AgentMiddlewareClient


class B2AClient(AgentMiddlewareClient):
    """Client for Agent Middleware API with CrewAI compatibility."""
