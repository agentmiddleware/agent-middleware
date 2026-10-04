"""Opt-in multi-process PostgreSQL proofs for governed MCP crash behavior.

The API workers and independent MCP partner run on loopback. The workers share
one explicitly isolated PostgreSQL database. This module is intentionally
skipped unless both opt-in flags are present; an opted-in run fails closed if
the database is not PostgreSQL, the application environment is production-like,
the Alembic revision is stale, or any application table already contains data.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TextIO

import httpx
import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.trust_mode import is_production_like_environment
from tests.test_trust_helpers import create_tool_permit, provision_agent_wallet


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_HEADER = "X-MCP-Stress-Control"
GOVERNED_MCP_ENDPOINT = "/mcp/invoke"
STRESS_TOOL = "stress-governed-tool"
REMOTE_STRESS_TOOL = "remote-stress-governed-tool"
UPSTREAM_STRESS_TOOL = "stress-upstream-tool"
_ADVISORY_LOCK_ID = int.from_bytes(b"MCPSTRES", byteorder="big", signed=False)
_STRESS_TABLES = ("mcp_stress_tool_executions", "mcp_stress_upstream_effects")


@dataclass(frozen=True)
class StressHarness:
    database_url: str
    run_id: str
    signing_private_key_b64: str
    signing_key_id: str
    control_token: str
    temp_root: Path


@dataclass
class StressWorker:
    process: subprocess.Popen[str]
    base_url: str
    marker_path: Path
    release_path: Path
    log_path: Path
    log_stream: TextIO


@dataclass
class RemotePartner:
    process: subprocess.Popen[str]
    base_url: str
    database_path: Path
    log_path: Path
    log_stream: TextIO
    bearer_token: str


@dataclass(frozen=True)
class SeededCall:
    wallet_id: str
    permit_id: str
    idempotency_key: str
    call_token: str
    headers: dict[str, str]
    body: dict[str, object]


@dataclass(frozen=True)
class OperationSnapshot:
    execution_count: int
    debit_count: int
    receipt_ids: tuple[str, ...]
    idempotency_record_id: str
    attempt_id: str | None
    attempt_state: str | None
    debit_refunded: bool
    idempotency_completed: bool


@dataclass(frozen=True)
class UpstreamSnapshot:
    """Durable disposition of one governed remote invocation."""

    effect_count: int
    dispatch_states: tuple[str, ...]
    # Whether each attempt row carries its own ledger pointer. A crash between
    # the debit commit and `attach_charge` leaves this False with a debit that
    # nonetheless exists, which is exactly the state recovery has to survive.
    dispatch_ledger_linked: tuple[bool, ...]
    debit_count: int
    refund_count: int
    receipt_outcomes: tuple[str, ...]
    permit_spent: str


def _require_explicit_isolation() -> str:
    if os.environ.get("RUN_MCP_MULTIPROCESS_TESTS") != "1":
        pytest.skip(
            "set RUN_MCP_MULTIPROCESS_TESTS=1 only for the isolated "
            "PostgreSQL multiprocess harness"
        )
    if os.environ.get("MCP_STRESS_DB_ISOLATED") != "1":
        pytest.fail(
            "MCP_STRESS_DB_ISOLATED=1 is required; refusing to inspect or "
            "mutate an unacknowledged database",
            pytrace=False,
        )

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.fail("DATABASE_URL is required for the opt-in harness", pytrace=False)
    if not database_url.startswith(
        ("postgres://", "postgresql://", "postgresql+asyncpg://")
    ):
        pytest.fail(
            "the opt-in harness requires a PostgreSQL DATABASE_URL",
            pytrace=False,
        )
    if os.environ.get("STATE_BACKEND", "").strip().lower() != "postgres":
        pytest.fail(
            "STATE_BACKEND=postgres is required for the opt-in harness",
            pytrace=False,
        )
    if is_production_like_environment(os.environ.get("ENVIRONMENT")):
        pytest.fail(
            "the multiprocess harness refuses production-like ENVIRONMENT values",
            pytrace=False,
        )

    railway_project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    expected_project_id = os.environ.get(
        "MCP_STRESS_EXPECTED_RAILWAY_PROJECT_ID", ""
    ).strip()
    if railway_project_id or expected_project_id:
        if not railway_project_id or railway_project_id != expected_project_id:
            pytest.fail(
                "Railway runs must set MCP_STRESS_EXPECTED_RAILWAY_PROJECT_ID "
                "to the disposable project's exact RAILWAY_PROJECT_ID",
                pytrace=False,
            )
    return database_url


def _tree_heads() -> set[str]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return set(ScriptDirectory.from_config(config).get_heads())


async def _assert_migrated_empty_database(connection: AsyncConnection) -> None:
    table_names = set(
        await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    )
    if "alembic_version" not in table_names:
        pytest.fail(
            "isolated PostgreSQL database is not Alembic-managed; run "
            "`alembic upgrade head` first",
            pytrace=False,
        )

    current_heads = set(
        (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars()
    )
    expected_heads = _tree_heads()
    if current_heads != expected_heads:
        pytest.fail(
            "isolated PostgreSQL database is not at the repository migration "
            "head "
            f"(current={sorted(current_heads)}, expected={sorted(expected_heads)})",
            pytrace=False,
        )

    for stress_table in _STRESS_TABLES:
        if stress_table in table_names:
            pytest.fail(
                f"{stress_table} already exists; refusing to reuse a prior stress run",
                pytrace=False,
            )

    populated: list[str] = []
    for table_name in sorted(table_names - {"alembic_version"}):
        # table_name comes only from SQLAlchemy's database inspector.
        count = await connection.scalar(text(f'SELECT COUNT(*) FROM "{table_name}"'))
        if int(count or 0):
            populated.append(table_name)
    if populated:
        pytest.fail(
            "isolated PostgreSQL database contains application data in: "
            + ", ".join(populated)
            + "; refusing to run destructive fault injection",
            pytrace=False,
        )


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def stress_harness(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[StressHarness]:
    database_url = _require_explicit_isolation()

    from app.db.database import get_engine

    engine = get_engine()
    if engine is None or engine.dialect.name != "postgresql":
        pytest.fail(
            "configured database engine is not PostgreSQL",
            pytrace=False,
        )

    connection = await engine.connect()
    lock_acquired = False
    table_created = False
    try:
        lock_acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _ADVISORY_LOCK_ID},
            )
        )
        if not lock_acquired:
            pytest.fail(
                "another MCP multiprocess stress run holds the database lock",
                pytrace=False,
            )

        await _assert_migrated_empty_database(connection)
        await connection.execute(
            text(
                """
                CREATE TABLE mcp_stress_tool_executions (
                    execution_id BIGSERIAL PRIMARY KEY,
                    call_token TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
                        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
                )
                """
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX ix_mcp_stress_tool_call_token "
                "ON mcp_stress_tool_executions (call_token)"
            )
        )
        # Records the simulated remote side effect. Like the local table above
        # it tolerates duplicate call tokens: a redispatch after ambiguity has
        # to be countable, not silently deduplicated by a constraint.
        await connection.execute(
            text(
                """
                CREATE TABLE mcp_stress_upstream_effects (
                    effect_id BIGSERIAL PRIMARY KEY,
                    call_token TEXT NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
                        DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
                )
                """
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX ix_mcp_stress_upstream_call_token "
                "ON mcp_stress_upstream_effects (call_token)"
            )
        )
        await connection.commit()
        table_created = True

        run_id = f"mp-{uuid.uuid4().hex[:12]}"
        yield StressHarness(
            database_url=database_url,
            run_id=run_id,
            signing_private_key_b64=base64.b64encode(secrets.token_bytes(32)).decode(
                "ascii"
            ),
            signing_key_id=f"stress-signing-{run_id}",
            control_token=secrets.token_urlsafe(32),
            temp_root=tmp_path_factory.mktemp("mcp-postgres-multiprocess"),
        )
    finally:
        try:
            if table_created:
                for stress_table in _STRESS_TABLES:
                    await connection.execute(
                        text(f"DROP TABLE IF EXISTS {stress_table}")
                    )
                await connection.commit()
        finally:
            try:
                if lock_acquired:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": _ADVISORY_LOCK_ID},
                    )
            finally:
                await connection.close()


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _worker_output(worker: StressWorker) -> str:
    if not worker.log_stream.closed:
        worker.log_stream.flush()
    try:
        return worker.log_path.read_text(encoding="utf-8")[-12000:]
    except OSError:
        return "<worker log unavailable>"


def _stop_worker(worker: StressWorker) -> None:
    try:
        if worker.process.poll() is None:
            worker.process.terminate()
            try:
                worker.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                worker.process.wait(timeout=3)
    finally:
        if not worker.log_stream.closed:
            worker.log_stream.close()


def _kill_worker(worker: StressWorker) -> None:
    """Terminate the worker without graceful shutdown or cleanup hooks."""
    try:
        if worker.process.poll() is None:
            worker.process.kill()
            worker.process.wait(timeout=3)
    finally:
        if not worker.log_stream.closed:
            worker.log_stream.close()


def _partner_output(partner: RemotePartner) -> str:
    if not partner.log_stream.closed:
        partner.log_stream.flush()
    try:
        return partner.log_path.read_text(encoding="utf-8")[-12000:]
    except OSError:
        return "<partner log unavailable>"


def _stop_partner(partner: RemotePartner) -> None:
    try:
        if partner.process.poll() is None:
            partner.process.terminate()
            try:
                partner.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                partner.process.kill()
                partner.process.wait(timeout=3)
    finally:
        if not partner.log_stream.closed:
            partner.log_stream.close()


async def _wait_for_partner(
    partner: RemotePartner,
    control_token: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    headers = {CONTROL_HEADER: control_token}
    async with httpx.AsyncClient(base_url=partner.base_url, timeout=1) as client:
        while asyncio.get_running_loop().time() < deadline:
            if partner.process.poll() is not None:
                pytest.fail(
                    "remote partner exited during startup:\n"
                    + _partner_output(partner),
                    pytrace=False,
                )
            try:
                response = await client.get("/__stress/health", headers=headers)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    pytest.fail(
        "remote partner did not become ready:\n" + _partner_output(partner),
        pytrace=False,
    )


async def _start_remote_partner(
    harness: StressHarness,
    *,
    app_module: str = "tests.support.mcp_remote_partner_app:app",
) -> RemotePartner:
    port = _unused_loopback_port()
    bearer_token = secrets.token_urlsafe(32)
    database_path = harness.temp_root / "remote-partner.sqlite3"
    log_path = harness.temp_root / "remote-partner.log"
    log_stream = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "MCP_REMOTE_PARTNER_ALLOWED_HOST": f"127.0.0.1:{port}",
            "MCP_REMOTE_PARTNER_BEARER_TOKEN": bearer_token,
            "MCP_REMOTE_PARTNER_CONTROL_TOKEN": harness.control_token,
            "MCP_REMOTE_PARTNER_DB_PATH": str(database_path),
            "MCP_REMOTE_PARTNER_HOLD_RESPONSE_DIR": str(
                harness.temp_root / "held-partner-responses"
            ),
            "PYTHONUNBUFFERED": "1",
        }
    )
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                app_module,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--lifespan",
                "on",
                "--no-access-log",
            ],
            cwd=REPO_ROOT,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except BaseException:
        log_stream.close()
        raise
    partner = RemotePartner(
        process=process,
        base_url=f"http://127.0.0.1:{port}",
        database_path=database_path,
        log_path=log_path,
        log_stream=log_stream,
        bearer_token=bearer_token,
    )
    try:
        await _wait_for_partner(partner, harness.control_token)
    except BaseException:
        _stop_partner(partner)
        raise
    return partner


async def _wait_for_worker(worker: StressWorker, control_token: str) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    headers = {CONTROL_HEADER: control_token}
    async with httpx.AsyncClient(base_url=worker.base_url, timeout=1) as client:
        while asyncio.get_running_loop().time() < deadline:
            if worker.process.poll() is not None:
                pytest.fail(
                    "stress worker exited during startup:\n" + _worker_output(worker),
                    pytrace=False,
                )
            try:
                response = await client.get("/__stress/pid", headers=headers)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    pytest.fail(
        "stress worker did not become ready:\n" + _worker_output(worker),
        pytrace=False,
    )


async def _start_worker(
    harness: StressHarness,
    *,
    name: str,
    fault_point: str = "",
    remote_partner: RemotePartner | None = None,
    action_mode: bool = False,
) -> StressWorker:
    port = _unused_loopback_port()
    marker_path = harness.temp_root / f"{name}.marker.json"
    release_path = harness.temp_root / f"{name}.release"
    log_path = harness.temp_root / f"{name}.log"
    log_stream = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "MCP_STRESS_ACTION_MODE": "1" if action_mode else "0",
            "ALLOW_LEGACY_UNPERMITTED_MCP": "false",
            "ALLOW_METADATA_CREATE_ALL": "false",
            "DATABASE_URL": harness.database_url,
            "ENABLE_DOGFOOD_TOOL": "false",
            "ENABLE_PROOF_SURFACES": "false",
            "ENVIRONMENT": "test",
            "MCP_STRESS_CONTROL_TOKEN": harness.control_token,
            "MCP_STRESS_DB_ISOLATED": "1",
            "MCP_STRESS_FAULT_ACTION": "pause",
            "MCP_STRESS_FAULT_POINT": fault_point,
            "MCP_STRESS_FAULT_REPEAT": "once",
            "MCP_STRESS_MARKER_PATH": str(marker_path),
            "MCP_STRESS_RELEASE_PATH": str(release_path),
            "MCP_UPSTREAM_BEARER_TOKEN": (
                remote_partner.bearer_token if remote_partner else ""
            ),
            # The independent partner needs time to return its acknowledgement.
            # Main's in-process upstream stress executor does not consult these
            # values, so keep its contention probes quick.
            "MCP_UPSTREAM_CALL_TIMEOUT_SECONDS": ("10" if remote_partner else "0.5"),
            "MCP_UPSTREAM_CONNECT_TIMEOUT_SECONDS": ("5" if remote_partner else "0.5"),
            "MCP_UPSTREAM_CREDITS_PER_CALL": "2",
            "MCP_UPSTREAM_ENABLED": "true" if remote_partner else "false",
            "MCP_UPSTREAM_PUBLIC_TOOL_ID": (
                REMOTE_STRESS_TOOL if remote_partner else ""
            ),
            "MCP_UPSTREAM_TOOL_NAME": "partner.write" if remote_partner else "",
            "MCP_UPSTREAM_URL": (
                f"{remote_partner.base_url}/mcp" if remote_partner else ""
            ),
            "PUBLIC_URL": f"http://127.0.0.1:{port}",
            "PYTHONUNBUFFERED": "1",
            "REDIS_URL": "",
            "RUN_MCP_MULTIPROCESS_TESTS": "1",
            "SENTINEL_API_KEY": "",
            "SENTINEL_API_URL": "",
            "SIMULATION_MODE_HUMAN_APPROVAL": "true",
            "STATE_BACKEND": "postgres",
            "STATE_NAMESPACE": harness.run_id,
            "STRIPE_SECRET_KEY": "",
            "TRUST_MODE_ENABLED": "true",
            "TRUST_SIGNING_KEY_ID": harness.signing_key_id,
            "TRUST_SIGNING_PRIVATE_KEY_B64": harness.signing_private_key_b64,
            "VALID_API_KEYS": "test-key",
        }
    )
    if action_mode:
        environment["ENABLE_STANDARD_MCP_ENDPOINT"] = "true"
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.support.mcp_stress_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--lifespan",
                "on",
                "--no-access-log",
            ],
            cwd=REPO_ROOT,
            env=environment,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except BaseException:
        log_stream.close()
        raise
    worker = StressWorker(
        process=process,
        base_url=f"http://127.0.0.1:{port}",
        marker_path=marker_path,
        release_path=release_path,
        log_path=log_path,
        log_stream=log_stream,
    )
    try:
        await _wait_for_worker(worker, harness.control_token)
    except BaseException:
        _stop_worker(worker)
        raise
    return worker


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def remote_partner(
    stress_harness: StressHarness,
) -> AsyncIterator[RemotePartner]:
    partner = await _start_remote_partner(stress_harness)
    try:
        yield partner
    finally:
        _stop_partner(partner)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def steady_worker(stress_harness: StressHarness) -> AsyncIterator[StressWorker]:
    worker = await _start_worker(stress_harness, name="steady")
    try:
        yield worker
    finally:
        _stop_worker(worker)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def remote_steady_worker(
    stress_harness: StressHarness,
    remote_partner: RemotePartner,
) -> AsyncIterator[StressWorker]:
    worker = await _start_worker(
        stress_harness,
        name="remote-steady",
        remote_partner=remote_partner,
    )
    try:
        yield worker
    finally:
        _stop_worker(worker)


async def _seed_call(
    harness: StressHarness,
    steady_worker: StressWorker,
    *,
    scenario: str,
    tool_name: str = STRESS_TOOL,
) -> SeededCall:
    suffix = uuid.uuid4().hex[:12]
    call_token = f"{harness.run_id}-{scenario}-{suffix}"
    idempotency_key = f"invoke-{scenario}-{suffix}"
    async with httpx.AsyncClient(base_url=steady_worker.base_url, timeout=20) as client:
        provisioned = await provision_agent_wallet(client)
        permit = await create_tool_permit(
            client,
            wallet_id=provisioned["agent_wallet_id"],
            key_id=provisioned["key_id"],
            tool_name=tool_name,
            max_credits=20,
            idem_key=f"permit-{scenario}-{suffix}",
        )
    body: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": f"request-{scenario}-{suffix}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"call_token": call_token},
            "mcpContext": {
                "wallet_id": provisioned["agent_wallet_id"],
                "permit_id": permit["permit_id"],
                "idempotency_key": idempotency_key,
            },
        },
    }
    return SeededCall(
        wallet_id=provisioned["agent_wallet_id"],
        permit_id=permit["permit_id"],
        idempotency_key=idempotency_key,
        call_token=call_token,
        headers=provisioned["agent_headers"],
        body=body,
    )


async def _invoke(worker: StressWorker, seeded: SeededCall) -> httpx.Response:
    async with httpx.AsyncClient(base_url=worker.base_url, timeout=30) as client:
        return await client.post(
            "/mcp/messages",
            json=seeded.body,
            headers=seeded.headers,
        )


async def _wait_for_marker(worker: StressWorker) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        if worker.process.poll() is not None:
            pytest.fail(
                "fault worker exited before writing its marker:\n"
                + _worker_output(worker),
                pytrace=False,
            )
        try:
            return json.loads(worker.marker_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            await asyncio.sleep(0.02)
    pytest.fail(
        "fault marker was not written:\n" + _worker_output(worker),
        pytrace=False,
    )


def _release_worker(worker: StressWorker) -> None:
    worker.release_path.write_text("release\n", encoding="utf-8")


def _assert_in_progress(response: httpx.Response) -> None:
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["message"] == "idempotency_in_progress"


async def _reconcile(
    harness: StressHarness,
    worker: StressWorker,
) -> dict[str, object]:
    async with httpx.AsyncClient(base_url=worker.base_url, timeout=20) as client:
        response = await client.post(
            "/__stress/reconcile",
            params={"idle_seconds": 0},
            headers={CONTROL_HEADER: harness.control_token},
        )
    assert response.status_code == 200
    return response.json()


async def _snapshot(seeded: SeededCall) -> OperationSnapshot:
    from app.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        record_row = (
            await session.execute(
                text(
                    """
                    SELECT record_id, response_json
                    FROM idempotency_records
                    WHERE wallet_id = :wallet_id
                      AND endpoint = :endpoint
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "wallet_id": seeded.wallet_id,
                    "endpoint": GOVERNED_MCP_ENDPOINT,
                    "idempotency_key": seeded.idempotency_key,
                },
            )
        ).one()
        record_id = str(record_row.record_id)
        execution_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM mcp_stress_tool_executions "
                        "WHERE call_token = :call_token"
                    ),
                    {"call_token": seeded.call_token},
                )
            ).scalar_one()
        )
        debit_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM ledger_entries "
                        "WHERE operation_key = :record_id AND action = 'debit'"
                    ),
                    {"record_id": record_id},
                )
            ).scalar_one()
        )
        receipt_ids = tuple(
            (
                await session.execute(
                    text(
                        "SELECT receipt_id FROM receipts "
                        "WHERE idempotency_record_id = :record_id "
                        "ORDER BY receipt_id"
                    ),
                    {"record_id": record_id},
                )
            ).scalars()
        )
        attempt_row = (
            await session.execute(
                text(
                    """
                    SELECT attempt_id, state, debit_refunded_at
                    FROM mcp_dispatch_attempts
                    WHERE idempotency_record_id = :record_id
                    """
                ),
                {"record_id": record_id},
            )
        ).one_or_none()
    return OperationSnapshot(
        execution_count=execution_count,
        debit_count=debit_count,
        receipt_ids=receipt_ids,
        idempotency_record_id=record_id,
        attempt_id=(str(attempt_row.attempt_id) if attempt_row is not None else None),
        attempt_state=(str(attempt_row.state) if attempt_row is not None else None),
        debit_refunded=(
            attempt_row is not None and attempt_row.debit_refunded_at is not None
        ),
        idempotency_completed=record_row.response_json is not None,
    )


async def _partner_executions(
    harness: StressHarness,
    partner: RemotePartner,
    call_token: str,
) -> list[dict[str, object]]:
    async with httpx.AsyncClient(base_url=partner.base_url, timeout=5) as client:
        response = await client.get(
            "/__stress/executions",
            params={"call_token": call_token},
            headers={CONTROL_HEADER: harness.control_token},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(payload["executions"])
    return payload["executions"]


def _assert_delivery_uncertain(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200
    error = response.json()["error"]
    assert error["code"] == -32005
    assert error["message"] == "delivery_uncertain"
    receipt = error["data"]["receipt"]
    assert receipt["outcome"] == "delivery_uncertain"
    return receipt


async def _upstream_snapshot(seeded: SeededCall) -> UpstreamSnapshot:
    from app.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        record_id = (
            await session.execute(
                text(
                    """
                    SELECT record_id
                    FROM idempotency_records
                    WHERE wallet_id = :wallet_id
                      AND endpoint = :endpoint
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "wallet_id": seeded.wallet_id,
                    "endpoint": GOVERNED_MCP_ENDPOINT,
                    "idempotency_key": seeded.idempotency_key,
                },
            )
        ).scalar_one()
        effect_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM mcp_stress_upstream_effects "
                        "WHERE call_token = :call_token"
                    ),
                    {"call_token": seeded.call_token},
                )
            ).scalar_one()
        )
        attempt_rows = (
            await session.execute(
                text(
                    "SELECT state, ledger_entry_id FROM mcp_dispatch_attempts "
                    "WHERE idempotency_record_id = :record_id "
                    "ORDER BY attempt_id"
                ),
                {"record_id": record_id},
            )
        ).all()
        dispatch_states = tuple(row.state for row in attempt_rows)
        dispatch_ledger_linked = tuple(
            row.ledger_entry_id is not None for row in attempt_rows
        )
        debit_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM ledger_entries "
                        "WHERE operation_key = :record_id AND action = 'debit'"
                    ),
                    {"record_id": record_id},
                )
            ).scalar_one()
        )
        refund_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM ledger_entries "
                        "WHERE wallet_id = :wallet_id AND action = 'refund'"
                    ),
                    {"wallet_id": seeded.wallet_id},
                )
            ).scalar_one()
        )
        receipt_outcomes = tuple(
            (
                await session.execute(
                    text(
                        "SELECT outcome FROM receipts "
                        "WHERE idempotency_record_id = :record_id "
                        "ORDER BY receipt_id"
                    ),
                    {"record_id": record_id},
                )
            ).scalars()
        )
        permit_spent = (
            await session.execute(
                text("SELECT spent_credits FROM permits WHERE permit_id = :permit_id"),
                {"permit_id": seeded.permit_id},
            )
        ).scalar_one()
    return UpstreamSnapshot(
        effect_count=effect_count,
        dispatch_states=dispatch_states,
        dispatch_ledger_linked=dispatch_ledger_linked,
        debit_count=debit_count,
        refund_count=refund_count,
        receipt_outcomes=receipt_outcomes,
        # Normalized so the comparison is on value, not on the scale the
        # driver happens to return ("0E-8" and "0.00000000" are one number).
        permit_spent=str(Decimal(permit_spent).normalize()),
    )


async def _assert_signed_terminal_evidence(
    seeded: SeededCall,
    *,
    receipt_outcome: str,
    reason_code: str,
    attempt_state: str,
    attempt_error_code: str,
    terminal_result: dict[str, object],
) -> str:
    """A recovered attempt must carry verifiable signed evidence.

    Reconstructing state after a kill is only worth something if the evidence
    it mints still verifies, so the full bundle is rebuilt and every check is
    required to pass -- signature, permit binding, audit-chain linkage, ledger
    linkage, and the dispatch linkage that binds receipt to durable state.
    """
    from app.core.auth import AuthContext
    from app.db.database import get_session_factory
    from app.services.receipts import get_receipt_service
    from app.trust.evidence import build_receipt_evidence

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT r.receipt_id,
                           r.outcome,
                           r.reason_code,
                           r.response_hash,
                           r.ledger_entry_id,
                           a.state,
                           a.response_hash AS attempt_response_hash,
                           a.result_json AS attempt_result_json,
                           a.error_code AS attempt_error_code
                    FROM receipts r
                    JOIN mcp_dispatch_attempts a
                      ON a.attempt_id = r.dispatch_attempt_id
                    JOIN idempotency_records i
                      ON i.record_id = r.idempotency_record_id
                    WHERE i.wallet_id = :wallet_id
                      AND i.endpoint = :endpoint
                      AND i.idempotency_key = :idempotency_key
                    """
                ),
                {
                    "wallet_id": seeded.wallet_id,
                    "endpoint": GOVERNED_MCP_ENDPOINT,
                    "idempotency_key": seeded.idempotency_key,
                },
            )
        ).one()

    assert row.outcome == receipt_outcome
    assert row.reason_code == reason_code
    assert row.state == attempt_state
    assert row.attempt_error_code == attempt_error_code
    # No upstream response was ever obtained. The retained bytes are the
    # middleware's own terminal marker, and the receipt must hash exactly
    # those -- never a fabricated upstream result.
    assert json.loads(row.attempt_result_json) == terminal_result
    assert row.response_hash == row.attempt_response_hash
    # Whether the charge was kept or refunded, the receipt has to name the
    # debit the disposition was computed from.
    assert row.ledger_entry_id is not None

    receipt = await get_receipt_service().get_receipt(row.receipt_id)
    assert receipt is not None
    evidence = await build_receipt_evidence(
        receipt=receipt,
        auth=AuthContext(source="test", raw_key="", is_bootstrap_admin=True),
    )
    failed = [
        f"{check.name}: {check.reason}"
        for check in evidence.checks
        if check.status == "failed"
    ]
    assert not failed, f"recovered evidence did not verify: {failed}"
    assert evidence.valid
    return row.receipt_id


def _assert_replayed_terminal(
    response: httpx.Response,
    *,
    reason: str,
    receipt_id: str,
) -> None:
    """A retry must replay the recovered terminal disposition, and say which.

    Asserting merely that *some* error came back is not enough: an unrepaired
    record answers ``idempotency_in_progress``, which is also an error, so a
    loose check would pass on a reconciliation that never happened. The reason
    and the signed receipt identity are what distinguish "resolved to this
    outcome" from "still stuck".
    """
    assert response.status_code == 200
    payload = response.json()
    assert "result" not in payload, f"terminal replay returned a success: {payload}"
    error = payload["error"]
    assert error["message"] == reason, (
        f"expected replayed reason {reason!r}, got {error['message']!r}"
    )
    replayed_receipt = error.get("data", {}).get("receipt", {})
    assert replayed_receipt.get("receipt_id") == receipt_id, (
        "replay did not carry the recovered receipt: "
        f"{replayed_receipt.get('receipt_id')!r} != {receipt_id!r}"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_two_processes_serialize_one_governed_side_effect(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
) -> None:
    async with httpx.AsyncClient(base_url=steady_worker.base_url, timeout=5) as client:
        missing = await client.get("/__stress/pid")
        wrong = await client.get(
            "/__stress/pid",
            headers={CONTROL_HEADER: "wrong-control-token"},
        )
        missing_reconcile = await client.post("/__stress/reconcile")
        wrong_reconcile = await client.post(
            "/__stress/reconcile",
            headers={CONTROL_HEADER: "wrong-control-token"},
        )
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing_reconcile.status_code == 403
    assert wrong_reconcile.status_code == 403

    seeded = await _seed_call(stress_harness, steady_worker, scenario="overlap")
    fault_worker = await _start_worker(
        stress_harness,
        name=f"overlap-{uuid.uuid4().hex[:8]}",
        fault_point="after_tool_side_effect",
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == "after_tool_side_effect"

        competing = await _invoke(steady_worker, seeded)
        _assert_in_progress(competing)

        _release_worker(fault_worker)
        first = await asyncio.wait_for(first_task, timeout=20)
        assert first.status_code == 200
        assert "result" in first.json()

        replay = await _invoke(steady_worker, seeded)
        assert replay.status_code == 200
        assert replay.json()["result"] == first.json()["result"]

        snapshot = await _snapshot(seeded)
        assert snapshot.execution_count == 1
        assert snapshot.debit_count == 1
        assert len(snapshot.receipt_ids) == 1
        assert (
            first.json()["result"]["receipt"]["receipt_id"] == snapshot.receipt_ids[0]
        )
    finally:
        _release_worker(fault_worker)
        _stop_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_receipt_commit_survives_worker_death_and_reconciles(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
) -> None:
    seeded = await _seed_call(stress_harness, steady_worker, scenario="receipt-crash")
    fault_worker = await _start_worker(
        stress_harness,
        name=f"receipt-crash-{uuid.uuid4().hex[:8]}",
        fault_point="after_receipt_commit",
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == "after_receipt_commit"
        receipt_id = str(marker["context"]["receipt_id"])  # type: ignore[index]

        _stop_worker(fault_worker)
        await asyncio.gather(first_task, return_exceptions=True)

        blocked = await _invoke(steady_worker, seeded)
        _assert_in_progress(blocked)

        # Asserted against this record rather than the sweep's counters: those
        # are cumulative over every record the shared database still holds, so
        # reading them would couple this scenario to the order the module
        # happens to run in.
        assert (await _local_crash_state(seeded))["completed"] is False
        await _reconcile(stress_harness, steady_worker)
        assert (await _local_crash_state(seeded))["completed"] is True

        replay = await _invoke(steady_worker, seeded)
        assert replay.status_code == 200
        result = replay.json()["result"]
        assert result["reconciled"] is True
        assert result["outcome"] == "success"
        assert result["isError"] is False
        assert result["receipt_id"] == receipt_id

        snapshot = await _snapshot(seeded)
        assert snapshot.execution_count == 1
        assert snapshot.debit_count == 1
        assert snapshot.receipt_ids == (receipt_id,)
    finally:
        _stop_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_post_side_effect_crash_requires_review_without_redispatch(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
) -> None:
    seeded = await _seed_call(stress_harness, steady_worker, scenario="manual-review")
    fault_worker = await _start_worker(
        stress_harness,
        name=f"manual-review-{uuid.uuid4().hex[:8]}",
        fault_point="after_tool_side_effect",
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == "after_tool_side_effect"

        _stop_worker(fault_worker)
        await asyncio.gather(first_task, return_exceptions=True)

        blocked = await _invoke(steady_worker, seeded)
        _assert_in_progress(blocked)
        before = await _snapshot(seeded)
        assert before.execution_count == 1
        assert before.debit_count == 1
        assert before.receipt_ids == ()

        # Per record, not the cumulative sweep counters: this scenario's proof
        # is that its own record is left untouched, which is order-independent.
        await _reconcile(stress_harness, steady_worker)
        assert (await _local_crash_state(seeded))["completed"] is False

        replay = await _invoke(steady_worker, seeded)
        _assert_in_progress(replay)
        after = await _snapshot(seeded)
        assert after == before
    finally:
        _stop_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)


def test_remote_partner_execution_count_is_not_capped(tmp_path: Path) -> None:
    database_path = tmp_path / "remote-partner-count.sqlite3"
    environment = os.environ.copy()
    environment.update(
        {
            "MCP_REMOTE_PARTNER_ALLOWED_HOST": "testserver",
            "MCP_REMOTE_PARTNER_BEARER_TOKEN": "saturation-bearer",
            "MCP_REMOTE_PARTNER_CONTROL_TOKEN": "saturation-control",
            "MCP_REMOTE_PARTNER_DB_PATH": str(database_path),
        }
    )
    script = (
        "from starlette.testclient import TestClient\n"
        "from tests.support import mcp_remote_partner_app as partner\n"
        "with partner._connect() as connection:\n"
        "    connection.executemany(\n"
        '        "INSERT INTO mcp_remote_partner_executions '
        "(call_token, invocation_id, idempotency_key, worker_pid) "
        'VALUES (?, ?, ?, ?)",\n'
        '        [(f"call-{index}", "invocation", "idempotency", 1234) '
        "for index in range(1001)],\n"
        "    )\n"
        "    connection.commit()\n"
        "client = TestClient(partner.app)\n"
        "response = client.get(\n"
        '    "/__stress/health",\n'
        '    headers={partner.CONTROL_HEADER: "saturation-control"},\n'
        ")\n"
        "assert response.status_code == 200\n"
        'assert response.json()["execution_count"] == 1001\n'
        "assert len(partner._execution_rows()) == 1000\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.asyncio(loop_scope="session")
async def test_two_gateway_processes_share_one_remote_execution_and_replay(
    stress_harness: StressHarness,
    remote_partner: RemotePartner,
    remote_steady_worker: StressWorker,
) -> None:
    """One remote winner is replayed by an independent gateway process."""
    seeded = await _seed_call(
        stress_harness,
        remote_steady_worker,
        scenario="remote-overlap",
        tool_name=REMOTE_STRESS_TOOL,
    )
    owner_worker = await _start_worker(
        stress_harness,
        name=f"remote-overlap-{uuid.uuid4().hex[:8]}",
        fault_point="after_upstream_ack_before_terminal",
        remote_partner=remote_partner,
    )
    assert owner_worker.process.pid != remote_steady_worker.process.pid

    owner_task: asyncio.Task[httpx.Response] | None = None
    try:
        owner_task = asyncio.create_task(_invoke(owner_worker, seeded))
        marker = await _wait_for_marker(owner_worker)
        assert marker["point"] == "after_upstream_ack_before_terminal"

        before = await _snapshot(seeded)
        executions = await _partner_executions(
            stress_harness,
            remote_partner,
            seeded.call_token,
        )
        assert len(executions) == 1
        assert executions[0]["invocation_id"] == before.idempotency_record_id
        assert executions[0]["idempotency_key"] == seeded.idempotency_key
        assert executions[0]["worker_pid"] != marker["pid"]
        assert before.debit_count == 1
        assert before.receipt_ids == ()
        assert before.attempt_state == "dispatch_claimed"
        assert before.idempotency_completed is False

        competing = await _invoke(remote_steady_worker, seeded)
        _assert_in_progress(competing)
        assert (
            len(
                await _partner_executions(
                    stress_harness,
                    remote_partner,
                    seeded.call_token,
                )
            )
            == 1
        )

        _release_worker(owner_worker)
        owner_response = await asyncio.wait_for(owner_task, timeout=20)
        assert owner_response.status_code == 200
        owner_result = owner_response.json()["result"]

        replay = await _invoke(remote_steady_worker, seeded)
        assert replay.status_code == 200
        assert replay.json()["result"] == owner_result

        final_executions = await _partner_executions(
            stress_harness,
            remote_partner,
            seeded.call_token,
        )
        assert len(final_executions) == 1
        assert final_executions == executions

        after = await _snapshot(seeded)
        assert after.execution_count == 0
        assert after.debit_count == 1
        assert len(after.receipt_ids) == 1
        assert after.attempt_state == "succeeded"
        assert after.debit_refunded is False
        assert after.idempotency_completed is True
        receipt_id = owner_result["receipt"]["receipt_id"]
        assert after.receipt_ids == (receipt_id,)
        assert replay.json()["result"]["receipt"]["receipt_id"] == receipt_id
    finally:
        _release_worker(owner_worker)
        _stop_worker(owner_worker)
        if owner_task is not None:
            await asyncio.gather(owner_task, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_remote_claim_commit_before_send_crash_never_redispatches(
    stress_harness: StressHarness,
    remote_partner: RemotePartner,
    remote_steady_worker: StressWorker,
) -> None:
    seeded = await _seed_call(
        stress_harness,
        remote_steady_worker,
        scenario="remote-claim-crash",
        tool_name=REMOTE_STRESS_TOOL,
    )
    fault_worker = await _start_worker(
        stress_harness,
        name=f"remote-claim-crash-{uuid.uuid4().hex[:8]}",
        fault_point="after_dispatch_claim",
        remote_partner=remote_partner,
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == "after_dispatch_claim"

        before = await _snapshot(seeded)
        assert before.execution_count == 0
        assert before.debit_count == 1
        assert before.receipt_ids == ()
        assert before.attempt_state == "dispatch_claimed"
        assert before.debit_refunded is False
        assert before.idempotency_completed is False
        assert (
            await _partner_executions(stress_harness, remote_partner, seeded.call_token)
            == []
        )

        _kill_worker(fault_worker)
        await asyncio.gather(first_task, return_exceptions=True)

        blocked = await _invoke(remote_steady_worker, seeded)
        _assert_in_progress(blocked)
        assert (
            await _partner_executions(stress_harness, remote_partner, seeded.call_token)
            == []
        )

        reconciliation = await _reconcile(stress_harness, remote_steady_worker)
        assert reconciliation["dispatch_uncertain"] == 1
        assert reconciliation["dispatch_failed_attempt_ids"] == []

        replay = await _invoke(remote_steady_worker, seeded)
        receipt = _assert_delivery_uncertain(replay)
        after = await _snapshot(seeded)
        assert after.execution_count == 0
        assert after.debit_count == 1
        assert after.attempt_state == "delivery_uncertain"
        assert after.debit_refunded is False
        assert after.idempotency_completed is True
        assert after.receipt_ids == (receipt["receipt_id"],)
        assert (
            await _partner_executions(stress_harness, remote_partner, seeded.call_token)
            == []
        )
    finally:
        _kill_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_remote_ack_before_terminal_commit_crash_becomes_uncertain_without_redispatch(
    stress_harness: StressHarness,
    remote_partner: RemotePartner,
    remote_steady_worker: StressWorker,
) -> None:
    seeded = await _seed_call(
        stress_harness,
        remote_steady_worker,
        scenario="remote-ack-crash",
        tool_name=REMOTE_STRESS_TOOL,
    )
    fault_worker = await _start_worker(
        stress_harness,
        name=f"remote-ack-crash-{uuid.uuid4().hex[:8]}",
        fault_point="after_upstream_ack_before_terminal",
        remote_partner=remote_partner,
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == "after_upstream_ack_before_terminal"

        before = await _snapshot(seeded)
        executions = await _partner_executions(
            stress_harness, remote_partner, seeded.call_token
        )
        assert len(executions) == 1
        execution = executions[0]
        assert execution["invocation_id"] == before.idempotency_record_id
        assert execution["idempotency_key"] == seeded.idempotency_key
        assert execution["worker_pid"] != marker["pid"]
        assert before.execution_count == 0
        assert before.debit_count == 1
        assert before.receipt_ids == ()
        assert before.attempt_state == "dispatch_claimed"
        assert before.debit_refunded is False
        assert before.idempotency_completed is False

        _kill_worker(fault_worker)
        await asyncio.gather(first_task, return_exceptions=True)

        blocked = await _invoke(remote_steady_worker, seeded)
        _assert_in_progress(blocked)
        assert (
            len(
                await _partner_executions(
                    stress_harness, remote_partner, seeded.call_token
                )
            )
            == 1
        )

        reconciliation = await _reconcile(stress_harness, remote_steady_worker)
        assert reconciliation["dispatch_uncertain"] == 1
        assert reconciliation["dispatch_failed_attempt_ids"] == []

        replay = await _invoke(remote_steady_worker, seeded)
        receipt = _assert_delivery_uncertain(replay)
        after = await _snapshot(seeded)
        assert after.execution_count == 0
        assert after.debit_count == 1
        assert after.attempt_state == "delivery_uncertain"
        assert after.debit_refunded is False
        assert after.idempotency_completed is True
        assert after.receipt_ids == (receipt["receipt_id"],)
        assert (
            len(
                await _partner_executions(
                    stress_harness, remote_partner, seeded.call_token
                )
            )
            == 1
        )
    finally:
        _kill_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)


async def _kill_governed_upstream_call(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
    *,
    scenario: str,
    fault_point: str,
) -> SeededCall:
    """Kill a worker at one post-checkpoint boundary of a remote dispatch."""
    seeded = await _seed_call(
        stress_harness,
        steady_worker,
        scenario=scenario,
        tool_name=UPSTREAM_STRESS_TOOL,
    )
    fault_worker = await _start_worker(
        stress_harness,
        name=f"{scenario}-{uuid.uuid4().hex[:8]}",
        fault_point=fault_point,
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == fault_point

        _stop_worker(fault_worker)
        await asyncio.gather(first_task, return_exceptions=True)
    finally:
        _stop_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)
    return seeded


@pytest.mark.asyncio(loop_scope="session")
async def test_kill_after_dispatch_checkpoint_is_charged_delivery_uncertain(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
) -> None:
    """A kill just past the dispatch checkpoint is ambiguous, not failed.

    The worker dies after the durable ``prepared -> dispatch_claimed`` transition but
    before the simulated remote effect runs.  No effect actually landed, yet
    the trust plane cannot know that, so the conservative disposition is the
    one that must be recorded: charged, signed ambiguous, never redispatched.
    """
    seeded = await _kill_governed_upstream_call(
        stress_harness,
        steady_worker,
        scenario="upstream-dispatch-kill",
        fault_point="after_mark_dispatched",
    )

    # Trigger reconciliation with idle_seconds=0 to mark the killed attempt as stale.
    # This simulates the proper flow: after a crash, reconciliation sweep runs
    # (either periodic or forced), then retry sees the reconciled result.
    reconciliation_before_retry = await _reconcile(stress_harness, steady_worker)
    assert reconciliation_before_retry["dispatch_dispatched_uncertain"] == 1

    # Now retry - should get the reconciled delivery_uncertain result
    first_retry = await _invoke(steady_worker, seeded)
    after = await _upstream_snapshot(seeded)
    assert after.dispatch_states == ("delivery_uncertain",)
    # The reconciler owns no upstream client; ambiguity must not resend.
    assert after.effect_count == 0
    # Charge-once, and the charge is deliberately retained under ambiguity.
    assert after.debit_count == 1
    assert after.refund_count == 0
    assert after.receipt_outcomes == ("delivery_uncertain",)
    receipt_id = await _assert_signed_terminal_evidence(
        seeded,
        receipt_outcome="delivery_uncertain",
        reason_code="delivery_uncertain",
        attempt_state="delivery_uncertain",
        attempt_error_code="delivery_uncertain",
        terminal_result={"error": "delivery_uncertain"},
    )
    _assert_replayed_terminal(
        first_retry, reason="delivery_uncertain", receipt_id=receipt_id
    )

    # The periodic reconciler sweep is now idempotent: the forced sweep above
    # already reconciled the attempt.
    reconciliation = await _reconcile(stress_harness, steady_worker)
    assert reconciliation["dispatch_dispatched_uncertain"] == 0
    assert reconciliation["dispatch_failed_attempts"] == 0

    # A client retry must replay the ambiguous disposition, never re-execute.
    replay = await _invoke(steady_worker, seeded)
    _assert_replayed_terminal(
        replay, reason="delivery_uncertain", receipt_id=receipt_id
    )
    assert await _upstream_snapshot(seeded) == after

    # Reconciliation is idempotent: a second sweep is not a second recovery.
    again = await _reconcile(stress_harness, steady_worker)
    assert again["dispatch_dispatched_uncertain"] == 0
    assert again["dispatch_failed_attempts"] == 0
    assert await _upstream_snapshot(seeded) == after


@pytest.mark.asyncio(loop_scope="session")
async def test_kill_after_remote_effect_never_redispatches_the_effect(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
) -> None:
    """The strongest row: the remote effect landed, the acknowledgement did not.

    One durable upstream effect exists and no terminal result was ever
    recorded.  Recovery must terminalize the attempt as ambiguous and leave
    the effect count at exactly one -- a second row here would mean the trust
    plane redispatched a side effect it could not prove had failed.
    """
    seeded = await _kill_governed_upstream_call(
        stress_harness,
        steady_worker,
        scenario="upstream-effect-kill",
        fault_point="after_upstream_effect",
    )

    # Trigger reconciliation with idle_seconds=0 to mark the killed attempt as stale.
    reconciliation_before_retry = await _reconcile(stress_harness, steady_worker)
    assert reconciliation_before_retry["dispatch_dispatched_uncertain"] == 1

    # Now retry - should get the reconciled delivery_uncertain result
    first_retry = await _invoke(steady_worker, seeded)
    after = await _upstream_snapshot(seeded)
    assert after.dispatch_states == ("delivery_uncertain",)
    assert after.effect_count == 1
    assert after.debit_count == 1
    assert after.refund_count == 0
    assert after.receipt_outcomes == ("delivery_uncertain",)
    receipt_id = await _assert_signed_terminal_evidence(
        seeded,
        receipt_outcome="delivery_uncertain",
        reason_code="delivery_uncertain",
        attempt_state="delivery_uncertain",
        attempt_error_code="delivery_uncertain",
        terminal_result={"error": "delivery_uncertain"},
    )
    _assert_replayed_terminal(
        first_retry, reason="delivery_uncertain", receipt_id=receipt_id
    )

    # The periodic reconciler sweep is now idempotent: the forced sweep above
    # already reconciled the attempt.
    reconciliation = await _reconcile(stress_harness, steady_worker)
    assert reconciliation["dispatch_dispatched_uncertain"] == 0
    assert reconciliation["dispatch_failed_attempts"] == 0

    replay = await _invoke(steady_worker, seeded)
    _assert_replayed_terminal(
        replay, reason="delivery_uncertain", receipt_id=receipt_id
    )
    assert await _upstream_snapshot(seeded) == after

    again = await _reconcile(stress_harness, steady_worker)
    assert again["dispatch_dispatched_uncertain"] == 0
    assert again["dispatch_failed_attempts"] == 0
    assert await _upstream_snapshot(seeded) == after


@pytest.mark.asyncio(loop_scope="session")
async def test_kill_between_debit_and_dispatch_refunds_without_dispatching(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
) -> None:
    """A kill before the dispatch checkpoint is provably non-delivered.

    The worker dies immediately after the debit commits, so the attempt is
    still `prepared` and has not yet been told which ledger entry paid for it.
    Nothing crossed the dispatch boundary, so this is the one post-charge crash
    the trust plane *can* resolve in the caller's favour: recovery must find the
    orphaned debit by its operation identity rather than by the attempt's own
    null pointer, refund it, release the reservation, and finalize the attempt
    without ever contacting the upstream server.
    """
    seeded = await _kill_governed_upstream_call(
        stress_harness,
        steady_worker,
        scenario="upstream-debit-kill",
        fault_point="after_debit_commit",
    )

    # Trigger reconciliation with idle_seconds=0 to mark the killed PREPARED attempt as stale.
    reconciliation_before_retry = await _reconcile(stress_harness, steady_worker)
    assert reconciliation_before_retry["dispatch_prepared_finalized"] == 1

    # Now retry - should get the reconciled failed_refunded result
    first_retry = await _invoke(steady_worker, seeded)
    after = await _upstream_snapshot(seeded)
    assert after.dispatch_states == ("returned_error",)
    # Recovery adopted the orphaned debit by operation identity.
    assert after.dispatch_ledger_linked == (True,)
    # Nothing was ever dispatched, so nothing may be dispatched now.
    assert after.effect_count == 0
    # Exactly one debit and exactly one compensating refund; the reservation is
    # released rather than silently retained.
    assert after.debit_count == 1
    assert after.refund_count == 1
    assert after.permit_spent == "0"
    assert after.receipt_outcomes == ("failed_refunded",)
    receipt_id = await _assert_signed_terminal_evidence(
        seeded,
        receipt_outcome="failed_refunded",
        reason_code="failed_refunded",
        attempt_state="returned_error",
        attempt_error_code="reconciled_stale_prepared",
        terminal_result={
            "error": "failed_refunded",
            "error_code": "reconciled_stale_prepared",
        },
    )
    _assert_replayed_terminal(
        first_retry, reason="failed_refunded", receipt_id=receipt_id
    )

    # The periodic reconciler sweep is now idempotent: the forced sweep above
    # already reconciled the attempt.
    reconciliation = await _reconcile(stress_harness, steady_worker)
    assert reconciliation["dispatch_prepared_finalized"] == 0
    assert reconciliation["dispatch_dispatched_uncertain"] == 0
    assert reconciliation["dispatch_failed_attempts"] == 0

    replay = await _invoke(steady_worker, seeded)
    _assert_replayed_terminal(replay, reason="failed_refunded", receipt_id=receipt_id)
    assert await _upstream_snapshot(seeded) == after

    # Refund-once: a second sweep must not issue a second compensation.
    again = await _reconcile(stress_harness, steady_worker)
    assert again["dispatch_prepared_finalized"] == 0
    assert again["dispatch_failed_attempts"] == 0
    assert await _upstream_snapshot(seeded) == after


@dataclass(frozen=True)
class CrashExpectation:
    """The durable disposition a kill at one commit boundary must leave."""

    fault_point: str
    executions: int
    debits: int
    receipts: int
    budget_reserved: bool
    released_for_retry: bool
    why: str


# The boundaries the three narrative scenarios above do not reach. Each row is
# the contract for a kill at that point, and every one of them is conservative
# by design: the local path never auto-releases a charged or executed record,
# because a local tool's side effects are not observable to the reconciler the
# way a dispatch attempt's are.
#
# Every assertion here is against the seeded call's own record. The sweep's
# counters are cumulative over the shared database -- `stress_harness` is
# module-scoped and this module deliberately does not truncate between tests,
# because a kill-and-recover scenario needs its own prior state intact -- so
# reading them would make each scenario depend on the order the module runs
# in. The narrative scenarios above were converted to the same per-record
# form for that reason.
LOCAL_CRASH_CONTRACT = (
    CrashExpectation(
        fault_point="before_permit_reserve",
        executions=0,
        debits=0,
        receipts=0,
        budget_reserved=False,
        released_for_retry=False,
        why=(
            "Provably effect-free, yet deliberately not released: the "
            "effect-free sweep is scoped to operation_kind == 'upstream_mcp' "
            "because a local tool's side-effect ordering is not knowable to "
            "the reconciler. The key stays in progress rather than risking a "
            "second execution."
        ),
    ),
    CrashExpectation(
        fault_point="after_idempotency_begin",
        executions=0,
        debits=0,
        receipts=0,
        budget_reserved=False,
        released_for_retry=False,
        why="Same effect-free window, one commit later; same fail-closed hold.",
    ),
    CrashExpectation(
        fault_point="after_permit_reserve",
        executions=0,
        debits=0,
        receipts=0,
        budget_reserved=True,
        released_for_retry=False,
        why=(
            "The reservation is stranded on a still-active permit and stays "
            "stranded. reconcile_budgets only reclaims from permits that can "
            "no longer admit a charge, since a long-running call is "
            "indistinguishable from a dead one; the agent therefore spends "
            "less than authorized, never more."
        ),
    ),
    CrashExpectation(
        fault_point="after_mark_charged",
        executions=0,
        debits=1,
        receipts=0,
        budget_reserved=True,
        released_for_retry=False,
        why=(
            "Charged before the tool ran. The money moved and no receipt can "
            "be reconstructed, so this is held for operator review rather "
            "than refunded on a guess."
        ),
    ),
    CrashExpectation(
        fault_point="before_receipt_commit",
        executions=1,
        debits=1,
        receipts=0,
        budget_reserved=True,
        released_for_retry=False,
        why=(
            "The side effect landed and the charge stands, but nothing was "
            "signed. Ambiguous, so it fails closed to review."
        ),
    ),
    CrashExpectation(
        fault_point="after_audit_commit",
        executions=1,
        debits=1,
        receipts=0,
        budget_reserved=True,
        released_for_retry=False,
        why=(
            "The audit event is durable but the receipt is not: on the local "
            "path the audit precedes the receipt, so this is still the "
            "unsigned-outcome case."
        ),
    ),
    CrashExpectation(
        fault_point="after_idempotency_complete",
        executions=1,
        debits=1,
        receipts=1,
        budget_reserved=True,
        released_for_retry=True,
        why=(
            "Everything is durable and only the response to the caller was "
            "lost. Nothing to reconcile; the retry replays the stored result."
        ),
    ),
)


async def _local_crash_state(seeded: SeededCall) -> dict[str, object]:
    """Durable disposition of one governed local invocation."""
    from app.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        record = (
            await session.execute(
                text(
                    """
                    SELECT record_id, (response_json IS NOT NULL) AS completed
                    FROM idempotency_records
                    WHERE wallet_id = :wallet_id
                      AND endpoint = :endpoint
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "wallet_id": seeded.wallet_id,
                    "endpoint": GOVERNED_MCP_ENDPOINT,
                    "idempotency_key": seeded.idempotency_key,
                },
            )
        ).first()
        if record is None:
            return {"record_present": False}
        counts = {
            "record_present": True,
            "completed": bool(record.completed),
            "executions": int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM mcp_stress_tool_executions "
                            "WHERE call_token = :call_token"
                        ),
                        {"call_token": seeded.call_token},
                    )
                ).scalar_one()
            ),
            "debits": int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM ledger_entries "
                            "WHERE operation_key = :record_id AND action = 'debit'"
                        ),
                        {"record_id": record.record_id},
                    )
                ).scalar_one()
            ),
            "receipts": int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM receipts "
                            "WHERE idempotency_record_id = :record_id"
                        ),
                        {"record_id": record.record_id},
                    )
                ).scalar_one()
            ),
            "permit_spent": str(
                Decimal(
                    (
                        await session.execute(
                            text(
                                "SELECT spent_credits FROM permits "
                                "WHERE permit_id = :permit_id"
                            ),
                            {"permit_id": seeded.permit_id},
                        )
                    ).scalar_one()
                ).normalize()
            ),
        }
    return counts


@pytest.mark.parametrize(
    "contract",
    LOCAL_CRASH_CONTRACT,
    ids=[expectation.fault_point for expectation in LOCAL_CRASH_CONTRACT],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_local_crash_boundary_disposition(
    stress_harness: StressHarness,
    steady_worker: StressWorker,
    contract: CrashExpectation,
) -> None:
    """Every instrumented commit boundary lands in exactly one known state.

    The harness instruments twelve fault points; the narrative scenarios above
    reach five. This covers the remaining seven so that "the harness exposes
    more fault points than the tests exercise" stops being true, and so that
    the conservative dispositions -- a stranded reservation, a held review --
    are pinned as intended behaviour rather than left as folklore.
    """
    seeded = await _seed_call(
        stress_harness,
        steady_worker,
        scenario=f"boundary-{contract.fault_point.replace('_', '-')}",
    )
    fault_worker = await _start_worker(
        stress_harness,
        name=f"boundary-{contract.fault_point}-{uuid.uuid4().hex[:8]}",
        fault_point=contract.fault_point,
    )
    first_task: asyncio.Task[httpx.Response] | None = None
    try:
        first_task = asyncio.create_task(_invoke(fault_worker, seeded))
        marker = await _wait_for_marker(fault_worker)
        assert marker["point"] == contract.fault_point
        _stop_worker(fault_worker)
        await asyncio.gather(first_task, return_exceptions=True)
    finally:
        _stop_worker(fault_worker)
        if first_task is not None:
            await asyncio.gather(first_task, return_exceptions=True)

    before = await _local_crash_state(seeded)
    assert before["record_present"] is True
    assert before["executions"] == contract.executions, contract.why
    assert before["debits"] == contract.debits, contract.why
    assert before["receipts"] == contract.receipts, contract.why
    assert before["permit_spent"] == ("2" if contract.budget_reserved else "0")

    # Reconciliation is asserted through this record's own disposition rather
    # than the sweep's counters: the counters are cumulative across every
    # record the shared database still holds.
    await _reconcile(stress_harness, steady_worker)
    after = await _local_crash_state(seeded)
    assert after == before, f"reconciliation must not move this state: {contract.why}"

    replay = await _invoke(steady_worker, seeded)
    if contract.released_for_retry:
        assert "result" in replay.json(), contract.why
    else:
        _assert_in_progress(replay)

    # The invariant that holds at every boundary, whatever the disposition.
    settled = await _local_crash_state(seeded)
    assert settled["executions"] <= 1
    assert settled["debits"] <= 1
    assert settled["receipts"] <= 1
    assert settled == before
