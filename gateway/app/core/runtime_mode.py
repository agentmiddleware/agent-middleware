"""
Per-service simulation mode registry.

Every frozen proof surface whose behavior still depends on mocks, synthetic
data, or hardcoded responses gates that behavior through
``is_simulation("<service>")``. Production-like deployments disable the proof
surface routers entirely. A flag may only be set to ``False`` after an
explicit product decision unfreezes that surface and a real adapter is wired.

See ``docs/PROOF_SURFACES.md`` for the current product boundary.
"""

from __future__ import annotations

from .config import get_settings


# Service name -> Settings attribute.
# Adding a service here without also adding the Settings field will raise
# at startup on first lookup — intentional, so drift is loud.
_SERVICE_TO_SETTING: dict[str, str] = {
    "oracle": "SIMULATION_MODE_ORACLE",
    "red_team": "SIMULATION_MODE_RED_TEAM",
    "rtaas": "SIMULATION_MODE_RTAAS",
    "media_engine": "SIMULATION_MODE_MEDIA_ENGINE",
    "iot_bridge": "SIMULATION_MODE_IOT_BRIDGE",
    "telemetry_pm": "SIMULATION_MODE_TELEMETRY_PM",
    "agent_comms": "SIMULATION_MODE_AGENT_COMMS",
    "content_factory": "SIMULATION_MODE_CONTENT_FACTORY",
    "protocol_gen": "SIMULATION_MODE_PROTOCOL_GEN",
    "sandbox": "SIMULATION_MODE_SANDBOX",
    "human_approval": "SIMULATION_MODE_HUMAN_APPROVAL",
}


SERVICE_NAMES: frozenset[str] = frozenset(_SERVICE_TO_SETTING.keys())


class UnknownServiceError(ValueError):
    """Raised when a service name isn't registered for simulation mode."""


def is_simulation(service: str) -> bool:
    """Return whether the named service is running in simulation mode."""
    attr = _SERVICE_TO_SETTING.get(service)
    if attr is None:
        raise UnknownServiceError(
            f"Unknown service '{service}'. Known: {sorted(SERVICE_NAMES)}"
        )
    return bool(getattr(get_settings(), attr))


def require_simulation(service: str, issue: str | None = None) -> None:
    """
    Guard used at the top of a frozen simulated method. Raises
    NotImplementedError if the service's simulation flag was disabled —
    surfaces a loud failure instead of silently running a stub against
    production traffic. ``issue`` remains as a compatibility-only diagnostic
    suffix for callers outside this package.
    """
    if not is_simulation(service):
        suffix = f" (tracking: {issue})" if issue else ""
        raise NotImplementedError(
            f"{service}: real adapter is outside the current frozen proof-surface "
            f"scope{suffix}. Set SIMULATION_MODE_{service.upper()}=true or obtain "
            "an explicit product decision before implementing it; see "
            "docs/PROOF_SURFACES.md."
        )


def get_simulation_modes() -> dict[str, bool]:
    """Snapshot of every service's simulation state. Used by health checks."""
    return {name: is_simulation(name) for name in sorted(SERVICE_NAMES)}


def simulation_settings_field(service: str) -> str:
    """Return the Settings attribute name for a gated service (e.g. SIMULATION_MODE_ORACLE)."""
    attr = _SERVICE_TO_SETTING.get(service)
    if attr is None:
        raise UnknownServiceError(
            f"Unknown service '{service}'. Known: {sorted(SERVICE_NAMES)}"
        )
    return attr
