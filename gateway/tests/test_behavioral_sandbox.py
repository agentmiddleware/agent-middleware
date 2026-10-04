"""
Tests for the Behavioral Sandbox Engine — Phase 6
"""

import asyncio
import os
import pytest

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.core.config import get_settings
from app.services import behavioral_sandbox
from app.core.durable_state import DurableStateStore
from app.schemas.sandbox_behavioral import (
    SandboxEnvironmentCreate,
    SandboxEnvironmentType,
    ToolExecutionRequest,
)
from app.services.behavioral_sandbox import BehavioralSandboxEngine


@pytest.fixture
def engine():
    """Create a fresh engine for testing."""
    engine = BehavioralSandboxEngine(redis_url="redis://localhost:6379")
    return engine


@pytest.fixture
def sample_env_request():
    """Sample environment creation request."""
    return SandboxEnvironmentCreate(
        name="test-environment",
        environment_type=SandboxEnvironmentType.PYTHON_SUBPROCESS,
        timeout_seconds=10,
        memory_limit_mb=128,
        network_access=False,
    )


@pytest.fixture
async def sqlite_store(tmp_path, monkeypatch):
    """Create a SQLite durable-state store for sandbox rehydration tests."""
    monkeypatch.setenv("SQLITE_URL", os.fspath(tmp_path / "state.db"))
    monkeypatch.setenv("STATE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()

    store = DurableStateStore()
    yield store
    await store.close()
    get_settings.cache_clear()


class TestBehavioralSandboxSchemas:
    """Test schema validation."""

    def test_sandbox_environment_create_defaults(self):
        """Test default values for environment creation."""
        req = SandboxEnvironmentCreate(name="test")
        assert req.environment_type == SandboxEnvironmentType.PYTHON_SUBPROCESS
        assert req.timeout_seconds == 30
        assert req.memory_limit_mb == 256
        assert req.network_access is False
        assert req.env_vars == {}

    def test_sandbox_environment_types(self):
        """Test all environment types are valid."""
        for env_type in SandboxEnvironmentType:
            req = SandboxEnvironmentCreate(
                name="test",
                environment_type=env_type,
            )
            assert req.environment_type == env_type

    def test_tool_execution_request(self):
        """Test tool execution request schema."""
        req = ToolExecutionRequest(
            env_id="sandbox-test-123",
            tool_name="test_tool",
            tool_input={"param": "value"},
            dry_run=True,
        )
        assert req.env_id == "sandbox-test-123"
        assert req.tool_name == "test_tool"
        assert req.dry_run is True

    @pytest.mark.proof
    @pytest.mark.parametrize(
        "key",
        [
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "LD_AUDIT",
            "DYLD_INSERT_LIBRARIES",
            "GLIBC_TUNABLES",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONHOME",
            "PATH",
            "HOME",
            "BASH_ENV",
            "SANDBOX",
        ],
    )
    def test_env_vars_refuse_loader_and_interpreter_keys(self, key):
        """Keys that steer the loader, interpreter, or sandbox marker are refused."""
        with pytest.raises(ValidationError):
            SandboxEnvironmentCreate(name="t", env_vars={key: "/tmp/evil.so"})

    @pytest.mark.proof
    @pytest.mark.parametrize(
        "key", ["", "1ABC", "MY-VAR", "MY VAR", "A=B", "ld_preload", "X" * 65, "A\x00B"]
    )
    def test_env_vars_refuse_malformed_names(self, key):
        with pytest.raises(ValidationError):
            SandboxEnvironmentCreate(name="t", env_vars={key: "1"})

    @pytest.mark.proof
    def test_env_vars_bound_count_and_value_size(self):
        with pytest.raises(ValidationError):
            SandboxEnvironmentCreate(
                name="t", env_vars={f"VAR_{i}": "1" for i in range(33)}
            )
        with pytest.raises(ValidationError):
            SandboxEnvironmentCreate(name="t", env_vars={"BIG": "x" * 4097})
        with pytest.raises(ValidationError):
            SandboxEnvironmentCreate(name="t", env_vars={"NUL": "a\x00b"})

        at_limit = SandboxEnvironmentCreate(
            name="t", env_vars={f"VAR_{i}": "x" * 4096 for i in range(32)}
        )
        assert len(at_limit.env_vars) == 32

    @pytest.mark.proof
    def test_env_vars_allow_ordinary_keys(self):
        req = SandboxEnvironmentCreate(
            name="t", env_vars={"MY_FLAG": "1", "_PRIVATE": "x", "FEATURE_X2": "on"}
        )
        assert req.env_vars == {"MY_FLAG": "1", "_PRIVATE": "x", "FEATURE_X2": "on"}

    @pytest.mark.proof
    def test_environment_name_is_bounded(self):
        with pytest.raises(ValidationError):
            SandboxEnvironmentCreate(name="n" * 129)
        assert SandboxEnvironmentCreate(name="n" * 128).name == "n" * 128


class TestBehavioralSandboxEngine:
    """Test the behavioral sandbox engine."""

    @pytest.mark.anyio
    async def test_create_environment(self, engine, sample_env_request):
        """Test environment creation."""
        env = await engine.create_environment(sample_env_request)

        assert env.env_id.startswith("sandbox-")
        assert env.name == "test-environment"
        assert env.environment_type == SandboxEnvironmentType.PYTHON_SUBPROCESS
        assert env.status == "created"
        assert env.timeout_seconds == 10
        assert env.memory_limit_mb == 128

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_create_mcp_sandbox_environment(self, engine):
        """Test MCP sandbox environment creation."""
        req = SandboxEnvironmentCreate(
            name="mcp-test",
            environment_type=SandboxEnvironmentType.MCP_SANDBOX,
        )
        env = await engine.create_environment(req)

        assert env.environment_type == SandboxEnvironmentType.MCP_SANDBOX
        assert env.status == "created"

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_execute_python_subprocess(self, engine, sample_env_request):
        """Test Python subprocess execution."""
        env = await engine.create_environment(sample_env_request)

        request = ToolExecutionRequest(
            env_id=env.env_id,
            tool_name="hello_world",
            tool_input={"code": "print('hello from sandbox')"},
            dry_run=True,
        )

        result = await engine.execute_tool(request)

        assert result.env_id == env.env_id
        assert result.tool_name == "hello_world"
        assert result.status.value in ["success", "failed"]
        assert result.started_at is not None

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_execute_mcp_sandbox(self, engine):
        """Test MCP sandbox tool execution."""
        req = SandboxEnvironmentCreate(
            name="mcp-exec-test",
            environment_type=SandboxEnvironmentType.MCP_SANDBOX,
        )
        env = await engine.create_environment(req)

        request = ToolExecutionRequest(
            env_id=env.env_id,
            tool_name="get_weather",
            tool_input={"location": "San Francisco"},
            dry_run=True,
        )

        result = await engine.execute_tool(request)

        assert result.status.value == "success"
        assert result.output is not None
        assert result.cost_estimate is not None

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_execute_with_real_code(self, engine, sample_env_request):
        """Host Python execution is blocked unless explicitly enabled."""
        env = await engine.create_environment(sample_env_request)

        request = ToolExecutionRequest(
            env_id=env.env_id,
            tool_name="calculator",
            tool_input={
                "code": "result = 2 + 2\nprint(f'Result: {result}')",
                "context": {},
            },
            dry_run=False,
        )

        result = await engine.execute_tool(request)

        assert result.status.value == "failed"
        assert result.error is not None
        assert "disabled" in result.error
        assert result.resources_used["reason"] == "python_execution_backend_disabled"

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_docker_backend_reports_unavailable_without_falling_back_to_host(
        self, engine, monkeypatch
    ):
        """Docker backend failures do not silently fall back to host execution."""

        async def missing_docker(*args, **kwargs):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_docker)

        result = await engine._execute_python_docker(
            sandbox_code="print('should not run on host')",
            timeout_seconds=1,
            memory_limit_mb=64,
            env_vars={},
            image="python:3.12-slim",
        )

        assert result["success"] is False
        assert result["resources"]["reason"] == "docker_unavailable"

    @pytest.mark.anyio
    async def test_dry_run_python_subprocess_does_not_execute_code(
        self, engine, sample_env_request
    ):
        """Dry runs return a synthetic result without invoking host Python."""
        env = await engine.create_environment(sample_env_request)

        request = ToolExecutionRequest(
            env_id=env.env_id,
            tool_name="dry_run_probe",
            tool_input={"code": "raise RuntimeError('would execute')"},
            dry_run=True,
        )

        result = await engine.execute_tool(request)

        assert result.status.value == "success"
        assert result.output["mode"] == "python_subprocess_dry_run"

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_environment_not_found(self, engine):
        """Test execution with non-existent environment."""
        request = ToolExecutionRequest(
            env_id="nonexistent-env",
            tool_name="test",
            tool_input={},
        )

        with pytest.raises(ValueError, match="not found"):
            await engine.execute_tool(request)

    @pytest.mark.anyio
    async def test_get_environment_state(self, engine, sample_env_request):
        """Test getting environment state."""
        env = await engine.create_environment(sample_env_request)

        state = await engine.get_environment_state(env.env_id)

        assert state["env_id"] == env.env_id
        assert state["status"] == "created"
        assert "executions" in state
        assert "metrics" in state

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_environment_rehydrates_from_durable_state(
        self, sample_env_request, sqlite_store, monkeypatch
    ):
        """Sandbox env state survives process-local dictionary loss."""
        monkeypatch.setattr(
            behavioral_sandbox, "get_durable_state", lambda: sqlite_store
        )

        first_engine = BehavioralSandboxEngine(redis_url="redis://localhost:6379")
        env = await first_engine.create_environment(sample_env_request)

        request = ToolExecutionRequest(
            env_id=env.env_id,
            tool_name="rehydrate_probe",
            tool_input={"code": "print('persisted')"},
            dry_run=True,
        )
        await first_engine.execute_tool(request)

        second_engine = BehavioralSandboxEngine(redis_url="redis://localhost:6379")
        state = await second_engine.get_environment_state(env.env_id)

        assert state["env_id"] == env.env_id
        assert state["metrics"]["total_executions"] == 1

        await second_engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_destroy_environment(self, engine, sample_env_request):
        """Test environment destruction."""
        env = await engine.create_environment(sample_env_request)
        env_id = env.env_id

        result = await engine.destroy_environment(env_id)
        assert result is True

        with pytest.raises(ValueError, match="not found"):
            await engine.get_environment_state(env_id)

    @pytest.mark.anyio
    async def test_multiple_executions(self, engine, sample_env_request):
        """Test multiple executions in same environment."""
        env = await engine.create_environment(sample_env_request)

        for i in range(3):
            request = ToolExecutionRequest(
                env_id=env.env_id,
                tool_name=f"test_{i}",
                tool_input={"code": f"print('test {i}')"},
                dry_run=True,
            )
            result = await engine.execute_tool(request)
            assert result.status.value in ["success", "failed"]

        state = await engine.get_environment_state(env.env_id)
        assert state["metrics"]["total_executions"] == 3

        await engine.destroy_environment(env.env_id)

    @pytest.mark.anyio
    async def test_metrics_calculation(self, engine, sample_env_request):
        """Test metrics are calculated correctly."""
        env = await engine.create_environment(sample_env_request)

        request = ToolExecutionRequest(
            env_id=env.env_id,
            tool_name="metrics_test",
            tool_input={"code": "print('test')"},
            dry_run=True,
        )

        await engine.execute_tool(request)
        await engine.execute_tool(request)

        state = await engine.get_environment_state(env.env_id)
        metrics = state["metrics"]

        assert metrics["total_executions"] == 2
        assert metrics["avg_execution_time_ms"] >= 0

        await engine.destroy_environment(env.env_id)


class TestBehavioralSandboxRouter:
    """Test the behavioral sandbox router endpoints."""

    @pytest.fixture
    def api_headers(self):
        return {"X-API-Key": "test-key"}

    @pytest.mark.anyio
    async def test_create_environment_endpoint_requires_api_key(self):
        """Behavioral sandbox endpoints require authentication."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={"name": "unauthenticated"},
            )

            assert response.status_code == 401

    @pytest.mark.anyio
    async def test_create_environment_endpoint(self, api_headers):
        """Test POST /v1/sandbox/behavioral/environments."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={
                    "name": "router-test",
                    "environment_type": "python_subprocess",
                    "timeout_seconds": 10,
                },
                headers=api_headers,
            )

            assert response.status_code == 201
            data = response.json()
            assert "env_id" in data
            assert data["name"] == "router-test"

    @pytest.mark.proof
    @pytest.mark.anyio
    async def test_create_environment_endpoint_refuses_dangerous_env_vars(
        self, api_headers
    ):
        """LD_PRELOAD/PATH/PYTHONPATH never reach the stored environment."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            for env_vars in (
                {"LD_PRELOAD": "/tmp/evil.so"},
                {"PATH": "/tmp/evil-bin"},
                {"PYTHONPATH": "/tmp/evil-lib"},
                {"SANDBOX": "false"},
            ):
                response = await client.post(
                    "/v1/sandbox/behavioral/environments",
                    json={"name": "env-guard", "env_vars": env_vars},
                    headers=api_headers,
                )
                assert response.status_code == 422, env_vars
                assert "env_id" not in response.text

            response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={"name": "n" * 129},
                headers=api_headers,
            )
            assert response.status_code == 422

            response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={"name": "env-guard", "env_vars": {"MY_FLAG": "1"}},
                headers=api_headers,
            )
            assert response.status_code == 201
            assert response.json()["name"] == "env-guard"

    @pytest.mark.anyio
    async def test_execute_tool_endpoint(self, api_headers):
        """Test POST /v1/sandbox/behavioral/execute."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={"name": "execute-test"},
                headers=api_headers,
            )
            env_id = create_response.json()["env_id"]

            response = await client.post(
                "/v1/sandbox/behavioral/execute",
                json={
                    "env_id": env_id,
                    "tool_name": "test_tool",
                    "tool_input": {"code": "print('hello')"},
                    "dry_run": True,
                },
                headers=api_headers,
            )

            assert response.status_code == 200
            data = response.json()
            assert "execution_id" in data
            assert data["env_id"] == env_id

    @pytest.mark.anyio
    async def test_get_environment_endpoint(self, api_headers):
        """Test GET /v1/sandbox/behavioral/environments/{env_id}."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={"name": "get-test"},
                headers=api_headers,
            )
            env_id = create_response.json()["env_id"]

            response = await client.get(
                f"/v1/sandbox/behavioral/environments/{env_id}",
                headers=api_headers,
            )

            assert response.status_code == 200
            data = response.json()
            assert data["env_id"] == env_id

    @pytest.mark.anyio
    async def test_delete_environment_endpoint(self, api_headers):
        """Test DELETE /v1/sandbox/behavioral/environments/{env_id}."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            create_response = await client.post(
                "/v1/sandbox/behavioral/environments",
                json={"name": "delete-test"},
                headers=api_headers,
            )
            env_id = create_response.json()["env_id"]

            response = await client.delete(
                f"/v1/sandbox/behavioral/environments/{env_id}",
                headers=api_headers,
            )

            assert response.status_code == 204

    @pytest.mark.anyio
    async def test_execute_nonexistent_environment(self, api_headers):
        """Test execution on non-existent environment returns 404."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/sandbox/behavioral/execute",
                json={
                    "env_id": "nonexistent",
                    "tool_name": "test",
                    "tool_input": {},
                },
                headers=api_headers,
            )

            assert response.status_code == 404
