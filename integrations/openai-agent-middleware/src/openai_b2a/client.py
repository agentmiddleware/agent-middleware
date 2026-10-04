"""B2A client for the OpenAI integration.

Thin subclass of ``b2a_sdk.AgentMiddlewareClient``. All HTTP plumbing lives in
the shared base; this module exists so the OpenAI-flavored helpers hang off a
client name that matches the other framework wrappers.
"""

from b2a_sdk import AgentMiddlewareClient


class B2AClient(AgentMiddlewareClient):
    """Client for Agent Middleware API used by the OpenAI runner."""
