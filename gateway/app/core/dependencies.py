"""
FastAPI Dependency Injection for service orchestrators.
Each service is a singleton — one instance shared across all requests.
This is the spine connecting routers to actual business logic.
"""

from functools import lru_cache
from .config import get_settings

from ..services.iot_bridge import ProtocolBridge
from ..services.telemetry_pm import AutonomousPM
from ..services.media_engine import MediaEngine
from ..services.agent_comms import AgentComms
from ..services.content_factory import ContentFactory
from ..services.red_team import RedTeamSwarm
from ..services.oracle import AgentOracle
from ..services.agent_money import AgentMoney
from ..services.protocol_engine import ProtocolEngine
from ..services.rtaas import RTaaSEngine
from ..services.sandbox import SandboxEngine
from ..services.telemetry_scope import TelemetryScope
from ..services.oracle_broadcast import OracleBroadcastEngine

settings = get_settings()


@lru_cache()
def get_iot_bridge() -> ProtocolBridge:
    """Singleton IoT Protocol Bridge with ACL engine and MQTT translator."""
    bridge = ProtocolBridge(
        mqtt_broker_url=settings.MQTT_BROKER_URL,
        mqtt_default_qos=settings.MQTT_DEFAULT_QOS,
    )
    return bridge


@lru_cache()
def get_autonomous_pm() -> AutonomousPM:
    """Singleton Autonomous Product Manager with event store and anomaly detector."""
    return AutonomousPM(
        retention_hours=settings.TELEMETRY_RETENTION_HOURS,
        git_remote=settings.GIT_REMOTE_URL,
        branch_prefix=settings.GIT_BRANCH_PREFIX,
    )


@lru_cache()
def get_media_engine() -> MediaEngine:
    """Singleton Programmatic Media Engine with full pipeline."""
    return MediaEngine()


@lru_cache()
def get_agent_comms() -> AgentComms:
    """Singleton Agent Communications with registry and message router."""
    return AgentComms()


@lru_cache()
def get_content_factory() -> ContentFactory:
    """Singleton Content Factory with format adapters and algorithmic scheduler."""
    return ContentFactory()


@lru_cache()
def get_red_team_swarm() -> RedTeamSwarm:
    """Singleton Red Team Security Swarm with attack engine and scan store."""
    return RedTeamSwarm()


@lru_cache()
def get_agent_oracle() -> AgentOracle:
    """Singleton Agent Oracle with crawler, indexer, and registration engine."""
    return AgentOracle()


@lru_cache()
def get_agent_money() -> AgentMoney:
    """Singleton Agent Money engine with wallets, metering, and arbitrage."""
    return AgentMoney()


@lru_cache()
def get_protocol_engine() -> ProtocolEngine:
    """Singleton Protocol Generation Engine — code-to-discovery pipeline."""
    return ProtocolEngine()


@lru_cache()
def get_rtaas_engine() -> RTaaSEngine:
    """Singleton Red-Team-as-a-Service engine."""
    return RTaaSEngine()


@lru_cache()
def get_sandbox_engine() -> SandboxEngine:
    """Singleton Interactive Testing Sandbox engine."""
    return SandboxEngine()


@lru_cache()
def get_telemetry_scope() -> TelemetryScope:
    """Singleton Telemetry Scoping engine — multi-tenant autonomous PM."""
    return TelemetryScope()


@lru_cache()
def get_broadcast_engine() -> OracleBroadcastEngine:
    """Singleton Oracle Broadcast — pushes APIs into agent directories."""
    return OracleBroadcastEngine(oracle_service=get_agent_oracle())
