"""Independent MCP partner used by the opt-in process-kill acceptance tests.

The gateway and this partner run in separate Uvicorn processes. Each tool call
commits an observable side effect to a dedicated SQLite file before returning,
so killing a gateway worker cannot erase or fabricate the partner's execution
count. Duplicate call tokens are intentionally allowed: the harness must see a
redispatch rather than having a downstream uniqueness constraint hide it.
This is a synthetic fixture, not evidence of partner-owned customer validation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse


CONTROL_HEADER = "X-MCP-Stress-Control"
PARTNER_TOOL_NAME = "partner.write"
_INVOCATION_META_KEY = "io.agentmiddleware/invocation_id"
_IDEMPOTENCY_META_KEY = "io.agentmiddleware/idempotency_key"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise RuntimeError(f"{name} must be a non-empty value without whitespace")
    return value


_DATABASE_PATH = Path(_required_environment("MCP_REMOTE_PARTNER_DB_PATH"))
if not _DATABASE_PATH.is_absolute():
    raise RuntimeError("MCP_REMOTE_PARTNER_DB_PATH must be an absolute path")

_CONTROL_TOKEN = _required_environment("MCP_REMOTE_PARTNER_CONTROL_TOKEN")
_BEARER_TOKEN = _required_environment("MCP_REMOTE_PARTNER_BEARER_TOKEN")
_ALLOWED_HOST = _required_environment("MCP_REMOTE_PARTNER_ALLOWED_HOST")
_hold_response_directory = os.environ.get("MCP_REMOTE_PARTNER_HOLD_RESPONSE_DIR", "")
_HOLD_RESPONSE_DIRECTORY = (
    Path(_hold_response_directory) if _hold_response_directory else None
)
if _HOLD_RESPONSE_DIRECTORY is not None and not _HOLD_RESPONSE_DIRECTORY.is_absolute():
    raise RuntimeError("MCP_REMOTE_PARTNER_HOLD_RESPONSE_DIR must be an absolute path")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def _initialize_database() -> None:
    _DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_remote_partner_executions (
                execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_token TEXT NOT NULL,
                invocation_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                worker_pid INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_mcp_remote_partner_call_token
            ON mcp_remote_partner_executions (call_token)
            """
        )
        connection.commit()


def _persist_execution(
    *,
    call_token: str,
    invocation_id: str,
    idempotency_key: str,
) -> int:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO mcp_remote_partner_executions (
                call_token,
                invocation_id,
                idempotency_key,
                worker_pid
            )
            VALUES (?, ?, ?, ?)
            """,
            (call_token, invocation_id, idempotency_key, os.getpid()),
        )
        connection.commit()
        execution_id = cursor.lastrowid
    if execution_id is None:
        raise RuntimeError("mcp_remote_partner_execution_id_missing")
    return int(execution_id)


def _execution_rows(call_token: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT execution_id, call_token, invocation_id, idempotency_key, "
        "worker_pid, created_at FROM mcp_remote_partner_executions"
    )
    parameters: tuple[str, ...] = ()
    if call_token is not None:
        sql += " WHERE call_token = ?"
        parameters = (call_token,)
    sql += " ORDER BY execution_id LIMIT 1000"
    with _connect() as connection:
        rows = connection.execute(sql, parameters).fetchall()
    return [dict(row) for row in rows]


def _execution_count(call_token: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM mcp_remote_partner_executions"
    parameters: tuple[str, ...] = ()
    if call_token is not None:
        sql += " WHERE call_token = ?"
        parameters = (call_token,)
    with _connect() as connection:
        count = connection.execute(sql, parameters).fetchone()[0]
    return int(count)


def _control_authorized(request: Request) -> bool:
    provided = request.headers.get(CONTROL_HEADER)
    return provided is not None and hmac.compare_digest(provided, _CONTROL_TOKEN)


_initialize_database()

server = FastMCP(
    "agent-middleware-remote-stress-partner",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(allowed_hosts=[_ALLOWED_HOST]),
)


@server.tool(
    name=PARTNER_TOOL_NAME,
    description="Commit one duplicate-visible partner side effect for crash testing",
)
async def partner_write(call_token: str, ctx: Context) -> dict[str, Any]:
    if not call_token or len(call_token) > 512:
        raise ValueError("call_token must contain between 1 and 512 characters")

    metadata = ctx.request_context.meta
    metadata_payload = (
        metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
        if metadata is not None
        else {}
    )
    invocation_id = metadata_payload.get(_INVOCATION_META_KEY)
    idempotency_key = metadata_payload.get(_IDEMPOTENCY_META_KEY)
    if not isinstance(invocation_id, str) or not invocation_id:
        raise ValueError("forwarded invocation metadata is required")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("forwarded idempotency metadata is required")

    execution_id = await asyncio.to_thread(
        _persist_execution,
        call_token=call_token,
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
    )
    if _HOLD_RESPONSE_DIRECTORY is not None:
        # Only the exact token selected by the parent test loses its response.
        # Keep the control endpoints live so the committed effect is observable.
        token_hash = hashlib.sha256(call_token.encode("utf-8")).hexdigest()
        hold_path = _HOLD_RESPONSE_DIRECTORY / token_hash
        while hold_path.exists():
            await asyncio.sleep(0.02)
    return {
        "call_token": call_token,
        "execution_id": execution_id,
        "partner_pid": os.getpid(),
    }


@server.custom_route(
    "/__stress/health",
    methods=["GET"],
    include_in_schema=False,
)
async def stress_health(request: Request) -> JSONResponse:
    if not _control_authorized(request):
        return JSONResponse(
            {"detail": "mcp_remote_partner_control_denied"},
            status_code=403,
        )
    execution_count = await asyncio.to_thread(_execution_count)
    return JSONResponse(
        {
            "status": "ok",
            "pid": os.getpid(),
            "tool_name": PARTNER_TOOL_NAME,
            "execution_count": execution_count,
        }
    )


@server.custom_route(
    "/__stress/executions",
    methods=["GET"],
    include_in_schema=False,
)
async def stress_executions(request: Request) -> JSONResponse:
    if not _control_authorized(request):
        return JSONResponse(
            {"detail": "mcp_remote_partner_control_denied"},
            status_code=403,
        )
    call_token = request.query_params.get("call_token")
    if call_token is not None and (not call_token or len(call_token) > 512):
        return JSONResponse(
            {"detail": "mcp_remote_partner_call_token_invalid"},
            status_code=400,
        )
    rows = await asyncio.to_thread(_execution_rows, call_token)
    return JSONResponse({"count": len(rows), "executions": rows})


class _BearerAuthMiddleware:
    """Require the configured gateway credential only on the MCP transport."""

    def __init__(self, wrapped_app: Any) -> None:
        self._wrapped_app = wrapped_app
        self._expected = f"Bearer {_BEARER_TOKEN}".encode("utf-8")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            authorization_values = [
                value
                for key, value in scope.get("headers", [])
                if key.lower() == b"authorization"
            ]
            authorized = len(authorization_values) == 1 and hmac.compare_digest(
                authorization_values[0],
                self._expected,
            )
            if not authorized:
                response = JSONResponse(
                    {"detail": "mcp_remote_partner_unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._wrapped_app(scope, receive, send)


app = _BearerAuthMiddleware(server.streamable_http_app())
