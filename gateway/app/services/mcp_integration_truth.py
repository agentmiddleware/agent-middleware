"""
MCP tool honesty metadata (Phase 0).

Maps each tool's billing ``category`` to runtime simulation state so
``/mcp/tools.json`` stays aligned with ``/health/dependencies`` ``simulation_modes``.
"""

from __future__ import annotations

from typing import Any, Literal

from ..core.runtime_mode import SERVICE_NAMES, is_simulation

IntegrationStatus = Literal["simulated", "integrated", "platform", "postgres"]


def truth_for_category(category: str) -> dict[str, Any]:
    """
    Return stable annotation fields for MCP tool manifests.

    - ``simulated`` / ``integrated`` / ``postgres``: category is a gated runtime pillar
    - ``postgres``: Durable SQL path (and/or LLM): Oracle, Agent Comms, Content Factory when simulation is off
    - ``platform``: wallet/platform-level categories that are not runtime
      services at all and carry no SIMULATION_MODE flag (platform_fee,
      swarm_delegation). Every other ServiceCategory is gated; see
      tests/test_runtime_mode.py::test_every_service_category_is_classified.
    """
    if category in SERVICE_NAMES:
        sim = is_simulation(category)
        if not sim and category in ("oracle", "agent_comms", "content_factory"):
            status: IntegrationStatus = "postgres"
        else:
            status = "simulated" if sim else "integrated"
        return {
            "simulation": sim,
            "integration_status": status,
            "runtime_service": category,
        }
    return {
        "simulation": False,
        "integration_status": "platform",
        "runtime_service": None,
    }
