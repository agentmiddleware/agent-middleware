"""Independent action partner: naive effects or explicitly retained native keys.

Only this partner writes its SQLite store. A native: call token selects a
fixture business operation identity; key: selects forwarded native-key dedupe.
Everything else deliberately duplicates. These modes are test controls, not a
claim that arbitrary MCP servers honor metadata.
"""

import asyncio
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from tests.support import mcp_remote_partner_app as partner

with partner._connect() as db:
    db.execute(
        "CREATE TABLE IF NOT EXISTS action_requests (call_token TEXT, native_key TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS action_native_keys (native_key TEXT PRIMARY KEY, execution_id INTEGER NOT NULL)"
    )


def persist_execution(*, call_token, invocation_id, idempotency_key):
    native = (
        call_token
        if call_token.startswith("native:")
        else idempotency_key
        if call_token.startswith("key:")
        else None
    )
    with partner._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO action_requests VALUES (?, ?)", (call_token, idempotency_key)
        )
        old = db.execute(
            "SELECT execution_id FROM action_native_keys WHERE native_key = ?",
            (native,),
        ).fetchone()
        if old:
            return int(old[0])
        cursor = db.execute(
            "INSERT INTO mcp_remote_partner_executions (call_token,invocation_id,idempotency_key,worker_pid) VALUES (?,?,?,?)",
            (call_token, invocation_id, idempotency_key, os.getpid()),
        )
        result = int(cursor.lastrowid)
        if native:
            db.execute("INSERT INTO action_native_keys VALUES (?,?)", (native, result))
        return result


partner._persist_execution = persist_execution


def counters(call_token):
    with partner._connect() as db:
        return {
            "requests": db.execute(
                "SELECT COUNT(*) FROM action_requests WHERE call_token = ?",
                (call_token,),
            ).fetchone()[0],
            "effects": partner._execution_count(call_token),
            "native_keys": [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT native_key FROM action_requests WHERE call_token = ?",
                    (call_token,),
                )
            ],
        }


@partner.server.custom_route(
    "/__action/counters", methods=["GET"], include_in_schema=False
)
async def action_counters(request: Request):
    if not partner._control_authorized(request):
        return JSONResponse({"detail": "denied"}, status_code=403)
    return JSONResponse(
        await asyncio.to_thread(counters, request.query_params.get("call_token"))
    )


app = partner._BearerAuthMiddleware(partner.server.streamable_http_app())
