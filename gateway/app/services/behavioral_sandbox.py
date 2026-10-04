"""
Behavioral Sandbox Engine — Phase 6
==================================
Allows agents to test real tool execution (not just cost simulation) in
controlled environments. Host Python execution is disabled by default because
a subprocess is not a production sandbox boundary.

Supports:
- Python dry-run simulation, container execution via Docker when configured,
  and unsafe host subprocess execution only behind an explicit local-development
  opt-in
- MCP tool sandboxing with mocked responses
- HTTP proxy mode for API testing
- Redis-backed state isolation per environment
"""

import asyncio
import json
import logging
import resource
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis

from ..core.config import get_settings
from ..core.durable_state import get_durable_state
from ..core.trust_mode import UNSAFE_HOST_SANDBOX_BACKENDS
from ..core.url_guard import check_outbound_url
from ..schemas.sandbox_behavioral import (
    ExecutionStatus,
    SandboxEnvironment,
    SandboxEnvironmentCreate,
    SandboxEnvironmentType,
    ToolExecutionRequest,
    ToolExecutionResponse,
)

logger = logging.getLogger(__name__)


def _limit_child_process(memory_limit_mb: int) -> None:
    """Apply best-effort resource limits before executing opt-in host Python."""
    memory_bytes = memory_limit_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (ValueError, OSError, AttributeError):
        logger.debug("Unable to apply address-space limit to sandbox subprocess")

    try:
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    except (ValueError, OSError, AttributeError):
        logger.debug("Unable to apply CPU limit to sandbox subprocess")


class BehavioralSandboxEngine:
    """
    Core engine for behavioral sandbox execution.

    Provides dry-run and mocked execution for tool testing without affecting
    production systems. Each environment gets its own Redis namespace for state
    isolation when Redis is available. Host Python execution is unsafe and
    disabled unless a real backend such as Docker is explicitly configured.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self._redis: redis.Redis | None = None
        self._environments: dict[str, dict[str, Any]] = {}
        self._state = get_durable_state()

    @staticmethod
    def _state_key(env_id: str) -> str:
        return f"bhe.environments.{env_id}"

    @staticmethod
    def _env_data_to_json(env_data: dict[str, Any]) -> dict[str, Any]:
        env = env_data["env"]
        executions = env_data.get("executions", [])
        return {
            "env": env.model_dump(mode="json"),
            "executions": [
                execution.model_dump(mode="json") for execution in executions
            ],
            "state": env_data.get("state", {}),
            "env_vars": env_data.get("env_vars", {}),
        }

    @staticmethod
    def _env_data_from_json(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "env": SandboxEnvironment.model_validate(payload["env"]),
            "executions": [
                ToolExecutionResponse.model_validate(execution)
                for execution in payload.get("executions", [])
            ],
            "state": payload.get("state", {}),
            "env_vars": payload.get("env_vars", {}),
        }

    async def _save_environment(self, env_id: str) -> None:
        if env_id not in self._environments:
            return
        await self._state.save_json(
            self._state_key(env_id),
            self._env_data_to_json(self._environments[env_id]),
        )

    async def _load_environment(self, env_id: str) -> dict[str, Any] | None:
        env_data = self._environments.get(env_id)
        if env_data:
            return env_data

        payload = await self._state.load_json(self._state_key(env_id))
        if not isinstance(payload, dict):
            return None

        try:
            env_data = self._env_data_from_json(payload)
        except Exception:
            logger.exception("Skipping corrupt behavioral sandbox state: %s", env_id)
            return None

        self._environments[env_id] = env_data
        return env_data

    async def get_redis(self) -> redis.Redis | None:
        """Get or create Redis connection."""
        if self._redis is None:
            try:
                self._redis = redis.from_url(self.redis_url, decode_responses=True)
            except Exception:
                return None
        return self._redis

    async def create_environment(
        self, request: SandboxEnvironmentCreate
    ) -> SandboxEnvironment:
        """Create a new sandbox environment."""
        env_id = f"sandbox-{uuid.uuid4().hex[:12]}"

        env = SandboxEnvironment(
            env_id=env_id,
            name=request.name,
            environment_type=request.environment_type,
            status="created",
            wallet_id=request.wallet_id,
            created_at=datetime.now(timezone.utc),
            timeout_seconds=request.timeout_seconds,
            memory_limit_mb=request.memory_limit_mb,
            network_access=request.network_access,
        )

        self._environments[env_id] = {
            "env": env,
            "executions": [],
            "state": {},
            "env_vars": request.env_vars,
        }
        await self._save_environment(env_id)

        r = await self.get_redis()
        if r:
            try:
                await r.hset(
                    f"bhe:{env_id}",
                    mapping={
                        "created_at": env.created_at.isoformat(),
                        "status": "created",
                        "name": request.name,
                        "env_type": request.environment_type.value,
                    },
                )
                await r.expire(f"bhe:{env_id}", 3600)
            except Exception:
                pass

        logger.info(f"Created behavioral sandbox environment: {env_id}")
        return env

    async def execute_tool(
        self, request: ToolExecutionRequest
    ) -> ToolExecutionResponse:
        """Execute a tool within a sandbox environment."""
        env_data = await self._load_environment(request.env_id)
        if env_data is None:
            raise ValueError(f"Environment not found: {request.env_id}")

        env = env_data["env"]

        execution_id = f"exec-{uuid.uuid4().hex[:12]}"
        started_at = datetime.now(timezone.utc)

        execution = ToolExecutionResponse(
            execution_id=execution_id,
            env_id=request.env_id,
            tool_name=request.tool_name,
            status=ExecutionStatus.RUNNING,
            started_at=started_at,
        )

        env.executions_count += 1
        env.last_execution_at = started_at

        try:
            if env.environment_type == SandboxEnvironmentType.PYTHON_SUBPROCESS:
                result = await self._execute_python_subprocess(
                    request.tool_input,
                    request.dry_run,
                    env.timeout_seconds,
                    env.memory_limit_mb,
                    env_data["env_vars"],
                )
            elif env.environment_type == SandboxEnvironmentType.MCP_SANDBOX:
                result = await self._execute_mcp_sandbox(
                    request.tool_name,
                    request.tool_input,
                    request.dry_run,
                    env.timeout_seconds,
                )
            else:
                result = await self._execute_http_proxy(
                    request.tool_name,
                    request.tool_input,
                    request.dry_run,
                    env.timeout_seconds,
                    network_access=env.network_access,
                )

            execution.status = (
                ExecutionStatus.SUCCESS if result["success"] else ExecutionStatus.FAILED
            )
            execution.output = result.get("output")
            execution.error = result.get("error")
            execution.resources_used = result.get("resources", {})

            if request.dry_run:
                execution.cost_estimate = result.get("cost_estimate", 0.0)

        except asyncio.TimeoutError:
            execution.status = ExecutionStatus.TIMEOUT
            execution.error = f"Execution timed out after {env.timeout_seconds}s"
        except Exception as e:
            execution.status = ExecutionStatus.FAILED
            execution.error = str(e)
            logger.exception(f"Sandbox execution failed: {execution_id}")

        execution.completed_at = datetime.now(timezone.utc)
        execution.duration_ms = int(
            (execution.completed_at - started_at).total_seconds() * 1000
        )

        env_data["executions"].append(execution)
        await self._save_environment(request.env_id)

        r = await self.get_redis()
        if r:
            try:
                await r.lpush(
                    f"bhe:{request.env_id}:executions",
                    json.dumps(
                        {
                            "execution_id": execution.execution_id,
                            "tool_name": execution.tool_name,
                            "status": execution.status.value,
                            "duration_ms": execution.duration_ms,
                            "started_at": started_at.isoformat(),
                        }
                    ),
                )
            except Exception:
                pass

        return execution

    async def _execute_python_subprocess(
        self,
        tool_input: dict[str, Any],
        dry_run: bool,
        timeout_seconds: int,
        memory_limit_mb: int,
        env_vars: dict[str, str],
    ) -> dict[str, Any]:
        """Execute Python code through the configured sandbox backend."""
        code = tool_input.get("code", "print('Hello from sandbox')")
        context = tool_input.get("context", {})
        sandbox_code = self._build_python_wrapper(code, context, dry_run)

        if dry_run:
            return {
                "success": True,
                "output": {
                    "sandboxed": True,
                    "mode": "python_subprocess_dry_run",
                    "code_provided": bool(code),
                    "context_keys": sorted(context.keys()),
                    "dry_run": True,
                },
                "cost_estimate": 0.001,
            }

        settings = get_settings()
        backend = settings.BEHAVIORAL_SANDBOX_PYTHON_BACKEND.strip().lower()

        if backend == "docker":
            return await self._execute_python_docker(
                sandbox_code=sandbox_code,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
                env_vars=env_vars,
                image=settings.BEHAVIORAL_SANDBOX_DOCKER_IMAGE,
            )

        if (
            backend in UNSAFE_HOST_SANDBOX_BACKENDS
            or settings.ALLOW_UNSAFE_HOST_PYTHON_SANDBOX
        ):
            return await self._execute_python_host(
                sandbox_code=sandbox_code,
                timeout_seconds=timeout_seconds,
                memory_limit_mb=memory_limit_mb,
                env_vars=env_vars,
                unsafe_allowed=True,
            )

        if backend != "disabled":
            logger.warning("Unknown Python sandbox backend configured: %s", backend)

        logger.warning(
            "Blocked Python sandbox execution. Configure "
            "BEHAVIORAL_SANDBOX_PYTHON_BACKEND=docker for container isolation, "
            "or unsafe_host only for local dev."
        )
        return {
            "success": False,
            "error": (
                "Python execution is disabled. Configure "
                "BEHAVIORAL_SANDBOX_PYTHON_BACKEND=docker for container isolation, "
                "or unsafe_host only for local development."
            ),
            "resources": {
                "blocked": True,
                "reason": "python_execution_backend_disabled",
            },
        }

    @staticmethod
    def _build_python_wrapper(code: str, context: dict[str, Any], dry_run: bool) -> str:
        """Build the small runner passed to the selected execution backend."""
        return f"""
import contextlib
import io
import json

context = json.loads({json.dumps(context)!r})
dry_run = {dry_run!r}

def sandboxed_execute(context, dry_run):
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec({code!r})
        return {{"success": True, "output": output.getvalue() or "executed"}}
    except Exception as e:
        return {{"success": False, "error": str(e), "output": output.getvalue()}}

result = sandboxed_execute(context, dry_run)
print(json.dumps(result))
"""

    async def _execute_python_docker(
        self,
        sandbox_code: str,
        timeout_seconds: int,
        memory_limit_mb: int,
        env_vars: dict[str, str],
        image: str,
    ) -> dict[str, Any]:
        """Execute Python in an unprivileged Docker container."""
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            f"{memory_limit_mb}m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--user",
            "65534:65534",
            "--env",
            "SANDBOX=true",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        for name, value in env_vars.items():
            if name.isidentifier():
                command.extend(["--env", f"{name}={value}"])
        command.extend([image, "python", "-I", "-S", "-c", sandbox_code])

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return {
                "success": False,
                "error": "Docker executable not found for sandbox backend.",
                "resources": {"blocked": True, "reason": "docker_unavailable"},
            }

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "error": f"Timeout after {timeout_seconds}s",
                "resources": {"timeout": True, "backend": "docker"},
            }

        return self._parse_python_execution_output(
            stdout=stdout,
            stderr=stderr,
            backend="docker",
            returncode=proc.returncode,
        )

    async def _execute_python_host(
        self,
        sandbox_code: str,
        timeout_seconds: int,
        memory_limit_mb: int,
        env_vars: dict[str, str],
        unsafe_allowed: bool,
    ) -> dict[str, Any]:
        """Execute Python on the host; unsafe and only for local development."""
        if not unsafe_allowed:
            logger.warning(
                "Blocked host Python sandbox execution. Configure a real isolation "
                "backend or set ALLOW_UNSAFE_HOST_PYTHON_SANDBOX=true only for local dev."
            )
            return {
                "success": False,
                "error": (
                    "Python subprocess execution is disabled because host Python is "
                    "not a production sandbox. Use an external isolation backend "
                    "or set ALLOW_UNSAFE_HOST_PYTHON_SANDBOX=true only for local development."
                ),
                "resources": {
                    "blocked": True,
                    "reason": "unsafe_host_execution_disabled",
                },
            }

        try:
            with tempfile.TemporaryDirectory(prefix="bhe-python-") as tmpdir:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    sandbox_code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**dict(env_vars), "SANDBOX": "true"},
                    cwd=tmpdir,
                    preexec_fn=lambda: _limit_child_process(memory_limit_mb),
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout_seconds
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return {
                        "success": False,
                        "error": f"Timeout after {timeout_seconds}s",
                        "resources": {"timeout": True},
                    }

            return self._parse_python_execution_output(
                stdout=stdout,
                stderr=stderr,
                backend="unsafe_host",
                returncode=proc.returncode,
            )

        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _parse_python_execution_output(
        stdout: bytes,
        stderr: bytes,
        backend: str,
        returncode: int | None = 0,
    ) -> dict[str, Any]:
        output = stdout.decode(errors="replace").strip()
        error = stderr.decode(errors="replace").strip()

        if returncode != 0:
            return {
                "success": False,
                "error": error or f"Python runner exited with status {returncode}",
                "output": output,
                "resources": {"backend": backend},
            }

        try:
            result = json.loads(output)
            if isinstance(result, dict) and isinstance(result.get("success"), bool):
                result["resources"] = {"backend": backend}
                return result
        except json.JSONDecodeError:
            pass

        return {
            "success": False,
            "error": error or "Python runner returned no valid structured result",
            "output": output,
            "resources": {"backend": backend},
        }

    async def _execute_mcp_sandbox(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        dry_run: bool,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Execute an MCP tool in sandboxed mode with mocked responses."""
        mock_responses = {
            "get_weather": {
                "temperature": 72,
                "conditions": "sunny",
                "location": tool_input.get("location", "unknown"),
            },
            "send_email": {"sent": True, "message_id": f"mock-{uuid.uuid4().hex[:8]}"},
            "create_database": {
                "created": True,
                "db_id": f"mock-db-{uuid.uuid4().hex[:8]}",
            },
        }

        await asyncio.sleep(0.01)

        if dry_run:
            return {
                "success": True,
                "output": {
                    "sandboxed": True,
                    "tool": tool_name,
                    "input": tool_input,
                    "mock_response": mock_responses.get(
                        tool_name, {"status": "mocked"}
                    ),
                    "dry_run": True,
                    "cost_estimate": 0.001,
                },
                "cost_estimate": 0.001,
            }

        return {
            "success": True,
            "output": {
                "sandboxed": True,
                "tool": tool_name,
                "input": tool_input,
                "mock_response": mock_responses.get(tool_name, {"status": "mocked"}),
            },
        }

    async def _execute_http_proxy(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        dry_run: bool,
        timeout_seconds: int,
        *,
        network_access: bool = False,
    ) -> dict[str, Any]:
        """Execute an HTTP request in proxy mode."""
        url = tool_input.get("url")
        method = tool_input.get("method", "GET")

        if not url:
            return {"success": False, "error": "URL required for HTTP proxy mode"}

        if dry_run:
            return {
                "success": True,
                "output": {
                    "sandboxed": True,
                    "method": method,
                    "url": url,
                    "dry_run": True,
                    "cost_estimate": 0.0001,
                },
                "cost_estimate": 0.0001,
            }

        # The environment's network_access flag (default False) gates every
        # real request, and the URL guard blocks SSRF targets (loopback,
        # RFC1918, link-local metadata, non-http schemes) even when it's on.
        if not network_access:
            return {
                "success": False,
                "error": "network_access is disabled for this environment",
            }
        block_reason = await check_outbound_url(url)
        if block_reason:
            return {
                "success": False,
                "error": f"request blocked ({block_reason}): {url}",
            }

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                # Don't follow redirects: the URL guard only vetted the
                # original target, so a 3xx to a loopback/metadata address
                # would otherwise slip past it.
                async with session.request(
                    method,
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    allow_redirects=False,
                ) as resp:
                    return {
                        "success": True,
                        "output": {
                            "sandboxed": True,
                            "status_code": resp.status,
                            "url": str(resp.url),
                        },
                    }
        except ImportError:
            return {
                "success": True,
                "output": {
                    "sandboxed": True,
                    "mode": "http_proxy_mock",
                    "url": url,
                    "method": method,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_environment_owner(self, env_id: str) -> tuple[bool, str | None]:
        """Return ``(found, owner_wallet_id)`` for tenant-isolation checks.

        Lets a router enforce that the caller owns an environment before
        reading or destroying it, without exposing the internal env-data shape.
        """
        env_data = await self._load_environment(env_id)
        if env_data is None:
            return False, None
        return True, env_data["env"].wallet_id

    async def get_environment_state(self, env_id: str) -> dict[str, Any]:
        """Get current state of a sandbox environment."""
        env_data = await self._load_environment(env_id)
        if env_data is None:
            raise ValueError(f"Environment not found: {env_id}")

        env = env_data["env"]

        return {
            "env_id": env_id,
            "status": env.status,
            "executions": [
                {
                    "execution_id": e.execution_id,
                    "tool_name": e.tool_name,
                    "status": e.status.value,
                    "duration_ms": e.duration_ms,
                    "started_at": e.started_at.isoformat(),
                }
                for e in env_data["executions"]
            ],
            "metrics": self._calculate_metrics(env_data),
        }

    def _calculate_metrics(self, env_data: dict[str, Any]) -> dict[str, Any]:
        """Calculate metrics for an environment."""
        executions = env_data["executions"]
        total = len(executions)
        successful = sum(1 for e in executions if e.status == ExecutionStatus.SUCCESS)
        failed = sum(1 for e in executions if e.status == ExecutionStatus.FAILED)
        timeouts = sum(1 for e in executions if e.status == ExecutionStatus.TIMEOUT)

        durations = [e.duration_ms for e in executions if e.duration_ms]
        avg_time = sum(durations) / len(durations) if durations else 0

        return {
            "total_executions": total,
            "successful_executions": successful,
            "failed_executions": failed,
            "timeout_count": timeouts,
            "avg_execution_time_ms": avg_time,
        }

    async def destroy_environment(self, env_id: str) -> bool:
        """Destroy a sandbox environment."""
        env_data = await self._load_environment(env_id)
        if env_data is None:
            return False

        self._environments.pop(env_id, None)
        await self._state.delete(self._state_key(env_id))

        r = await self.get_redis()
        if r:
            try:
                await r.delete(f"bhe:{env_id}")
                await r.delete(f"bhe:{env_id}:executions")
            except Exception:
                pass

        logger.info(f"Destroyed behavioral sandbox environment: {env_id}")
        return True

    async def close(self):
        """Cleanup resources."""
        if self._redis:
            await self._redis.close()


_behavioral_sandbox: BehavioralSandboxEngine | None = None


def get_behavioral_sandbox() -> BehavioralSandboxEngine:
    """Get singleton behavioral sandbox instance."""
    global _behavioral_sandbox
    if _behavioral_sandbox is None:
        redis_url = "redis://localhost:6379"
        _behavioral_sandbox = BehavioralSandboxEngine(redis_url)
    return _behavioral_sandbox
