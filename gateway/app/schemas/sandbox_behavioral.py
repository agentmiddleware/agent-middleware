"""
Schemas for the Behavioral Sandbox Engine.
All models are Pydantic v2 for automatic OpenAPI generation.
"""

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_SANDBOX_ENV_VARS = 32
MAX_SANDBOX_ENV_VALUE_CHARS = 4096
_ENV_VAR_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
# Caller-supplied variables must not steer the dynamic loader, the
# interpreter, the shell, or the sandbox's own SANDBOX marker.
_DENIED_ENV_VAR_PREFIXES = ("LD_", "DYLD_", "GLIBC_", "PYTHON")
_DENIED_ENV_VAR_NAMES = frozenset(
    {"PATH", "HOME", "SANDBOX", "BASH_ENV", "ENV", "IFS", "SHELLOPTS"}
)


class SandboxEnvironmentType(str, Enum):
    """Type of sandbox environment."""

    PYTHON_SUBPROCESS = "python_subprocess"
    MCP_SANDBOX = "mcp_sandbox"
    HTTP_PROXY = "http_proxy"


class ExecutionStatus(str, Enum):
    """Status of a sandbox execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    CANCELLED = "cancelled"


class SandboxEnvironmentCreate(BaseModel):
    """Request to create a sandbox environment."""

    environment_type: SandboxEnvironmentType = Field(
        default=SandboxEnvironmentType.PYTHON_SUBPROCESS,
        description="Type of sandbox environment to create",
    )
    name: str = Field(
        ..., max_length=128, description="Human-readable name for this environment"
    )
    wallet_id: str | None = Field(None, description="Optional wallet for billing")
    timeout_seconds: int = Field(
        default=30, ge=1, le=300, description="Max execution time"
    )
    memory_limit_mb: int = Field(
        default=256, ge=64, le=1024, description="Memory limit in MB"
    )
    network_access: bool = Field(
        default=False, description="Allow network access in sandbox"
    )
    env_vars: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Environment variables to set in sandbox (at most "
            f"{MAX_SANDBOX_ENV_VARS}; names match [A-Z_][A-Z0-9_]{{0,63}}; "
            "LD_*, DYLD_*, GLIBC_*, PYTHON*, PATH, HOME, SANDBOX and shell "
            "startup names are refused)"
        ),
    )

    @field_validator("env_vars")
    @classmethod
    def _validate_env_vars(cls, env_vars: dict[str, str]) -> dict[str, str]:
        if len(env_vars) > MAX_SANDBOX_ENV_VARS:
            raise ValueError(
                f"at most {MAX_SANDBOX_ENV_VARS} environment variables are allowed"
            )
        for name, value in env_vars.items():
            if not _ENV_VAR_NAME_RE.fullmatch(name):
                raise ValueError(
                    "environment variable names must match [A-Z_][A-Z0-9_]{0,63}"
                )
            if name in _DENIED_ENV_VAR_NAMES or name.startswith(
                _DENIED_ENV_VAR_PREFIXES
            ):
                raise ValueError(f"environment variable {name} is not allowed")
            if len(value) > MAX_SANDBOX_ENV_VALUE_CHARS or "\x00" in value:
                raise ValueError(
                    f"environment variable {name} must be at most "
                    f"{MAX_SANDBOX_ENV_VALUE_CHARS} characters with no NUL bytes"
                )
        return env_vars


class SandboxEnvironment(BaseModel):
    """A running sandbox environment."""

    env_id: str = Field(..., description="Unique environment identifier")
    name: str = Field(..., description="Human-readable name")
    environment_type: SandboxEnvironmentType
    status: str = Field(default="created")
    wallet_id: str | None = None
    created_at: datetime
    timeout_seconds: int
    memory_limit_mb: int
    network_access: bool
    executions_count: int = 0
    last_execution_at: datetime | None = None


class ToolExecutionRequest(BaseModel):
    """Request to execute a tool in a sandbox."""

    env_id: str = Field(..., description="Environment ID to execute in")
    tool_name: str = Field(..., description="Name of the tool to execute")
    tool_input: dict[str, Any] = Field(
        default_factory=dict, description="Tool input parameters"
    )
    dry_run: bool = Field(
        default=False, description="If True, simulate without side effects"
    )


class ToolExecutionResponse(BaseModel):
    """Response from a tool execution."""

    execution_id: str = Field(..., description="Unique execution identifier")
    env_id: str
    tool_name: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    output: Any | None = None
    error: str | None = None
    resources_used: dict[str, Any] | None = None
    cost_estimate: float | None = None


class EnvironmentState(BaseModel):
    """Current state of a sandbox environment."""

    env_id: str
    status: str
    executions: list[ToolExecutionResponse]
    total_execution_time_ms: int = 0
    memory_peak_mb: float = 0.0
    created_at: datetime


class SandboxMetrics(BaseModel):
    """Metrics for a sandbox environment."""

    env_id: str
    total_executions: int
    successful_executions: int
    failed_executions: int
    timeout_count: int
    avg_execution_time_ms: float
    total_cost: float
    sandboxed_operations: list[dict[str, Any]] = Field(default_factory=list)
