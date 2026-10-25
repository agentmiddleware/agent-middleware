"""AMW StreamableHttp MCP server for Goose.

Proxies one read tool and one write tool to the official
``@modelcontextprotocol/server-filesystem`` server. Every call is checked
against the permit policy, keyed for idempotency with the fallback key
(verified caller + tool + canonical argument hash + agent-session-id), and
receives a signed receipt. ``agent-tool-call-request-id`` is logged only.

Configuration (environment):

* ``AMW_POLICY`` - permit policy JSON (see README)
* ``AMW_STATE_DIR`` - ledger, receipts and signing key
* ``AMW_CALLERS`` - JSON map of sha256(bearer token) -> caller id
* ``AMW_FS_ROOT`` - root directory handed to the filesystem server
* ``AMW_FS_SERVER_PACKAGE`` - pinned npm spec of the filesystem server,
  default ``@modelcontextprotocol/server-filesystem@2.0.1``
* ``AMW_IDEMPOTENCY_WINDOW_SECONDS`` - replay window, default 600
* ``AMW_HOST`` / ``AMW_PORT`` - bind address, default 127.0.0.1:8791
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import permits
from .canonical import args_hash, idempotency_key, sha256_hex
from .ledger import DEFAULT_WINDOW_SECONDS, Ledger
from .receipts import ReceiptSigner, load_or_create_key

log = logging.getLogger("goose_amw")

REQUEST_ID_KEY = "agent-tool-call-request-id"
SESSION_ID_KEY = "agent-session-id"


class UpstreamError(Exception):
    """The upstream answered with a definite error: the call had no effect."""


class UpstreamNotDispatched(UpstreamError):
    """Failed before the tool call was sent (spawn, session setup, initialize).

    Nothing reached the upstream tool, so the call is safe to retry and the
    idempotency key is left exactly as it was before the attempt.
    """


class UpstreamUnknown(Exception):
    """The call was dispatched but its outcome is unknown."""


Upstream = Callable[[str, dict[str, Any]], Awaitable[str]]


FS_SERVER_PACKAGE = "@modelcontextprotocol/server-filesystem@2.0.1"


def filesystem_upstream(fs_root: str, package: str | None = None) -> Upstream:
    from mcp.client.stdio import StdioServerParameters

    spec = package or os.environ.get("AMW_FS_SERVER_PACKAGE") or FS_SERVER_PACKAGE
    return stdio_upstream(StdioServerParameters(command="npx", args=["-y", spec, fs_root]))


def stdio_upstream(params: Any) -> Upstream:
    """Call one tool on a stdio MCP server, classifying failures by dispatch point."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    async def call(tool: str, arguments: dict[str, Any]) -> str:
        dispatched = False
        res = None
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                dispatched = True
                res = await session.call_tool(tool, arguments)
        except BaseException as exc:
            if res is not None and isinstance(exc, Exception):
                # The server already answered; a teardown error does not change that.
                log.warning("ignoring stdio teardown error after a result: %r", exc)
            elif not dispatched:
                raise UpstreamNotDispatched(f"filesystem server unavailable: {exc!r}") from exc
            else:
                raise UpstreamUnknown(f"{type(exc).__name__}: {exc}") from exc
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        if res.is_error:
            raise UpstreamError(text)
        return text

    return call


@dataclass
class Config:
    policy: dict[str, Any]
    state_dir: Path
    callers: dict[str, str]
    fs_root: str
    window_seconds: float = DEFAULT_WINDOW_SECONDS

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            policy=permits.load_policy(os.environ["AMW_POLICY"]),
            state_dir=Path(os.environ["AMW_STATE_DIR"]),
            callers=json.loads(Path(os.environ["AMW_CALLERS"]).read_text()),
            fs_root=os.environ["AMW_FS_ROOT"],
            window_seconds=float(
                os.environ.get("AMW_IDEMPOTENCY_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)
            ),
        )


def verify_caller(headers: Mapping[str, str] | None, callers: dict[str, str]) -> str:
    auth = (headers or {}).get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ToolError("AMW refused: caller not verified (missing bearer token)")
    digest = sha256_hex(auth[7:].strip())
    for known, caller in callers.items():
        if hmac.compare_digest(known, digest):
            return caller
    raise ToolError("AMW refused: caller not verified (unknown token)")


def _meta(ctx: Context) -> dict[str, Any]:
    m = ctx.request_context.meta
    if m is None:
        return {}
    return dict(m) if isinstance(m, dict) else m.model_dump(by_alias=True, exclude_none=True)


class Gateway:
    """Permit -> idempotency -> invoke -> receipt for each proxied call."""

    def __init__(
        self,
        config: Config,
        upstream: Upstream | None = None,
        after_commit: Callable[[str, str], Awaitable[None]] | None = None,
    ):
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.ledger = Ledger(config.state_dir / "ledger.sqlite3", config.window_seconds)
        stale = self.ledger.mark_stale_in_flight_unknown()
        if stale:
            log.warning("marked %d interrupted call(s) failed_unknown", stale)
        self.signer = ReceiptSigner(
            load_or_create_key(config.state_dir / "receipt_signing_key.pem"),
            config.state_dir / "receipts.jsonl",
        )
        (config.state_dir / "receipt_public_key.b64").write_text(self.signer.public_key_b64)
        self.upstream = upstream or filesystem_upstream(config.fs_root)
        # Test-only seam: tests inject faults here. Never set in production.
        self.after_commit = after_commit

    async def handle(self, tool: str, arguments: dict[str, Any], ctx: Context, *, write: bool):
        meta = _meta(ctx)
        request_id = meta.get(REQUEST_ID_KEY)
        session_id = meta.get(SESSION_ID_KEY) or None
        ahash = args_hash(arguments)
        base = {"protocol": ctx.protocol_version, "agent_tool_call_request_id": request_id}
        caller = verify_caller(ctx.headers, self.config.callers)
        base.update(caller=caller, session_id=session_id)

        decision = permits.check(self.config.policy, tool, arguments)
        if not decision.allowed:
            self.signer.issue(
                tool=tool,
                args_hash=ahash,
                decision="deny",
                outcome="not_run",
                reason=decision.reason,
                **base,
            )
            raise ToolError(f"AMW permit denied: {decision.reason}")

        if not write:
            return await self._run(tool, arguments, ahash, base, key=None)

        if not session_id:
            reason = (
                f"{tool} needs _meta['{SESSION_ID_KEY}'] to be deduplicated safely; "
                "the client sent none, so the write was not run"
            )
            self.signer.issue(
                tool=tool,
                args_hash=ahash,
                decision="refuse",
                outcome="not_run",
                reason=reason,
                **base,
            )
            raise ToolError(f"AMW refused: {reason}")

        key = idempotency_key(caller=caller, tool=tool, arguments=arguments, session_id=session_id)
        base["idempotency_key"] = key

        claim = self.ledger.claim(key, tool=tool, args_hash=ahash, request_id=request_id)
        if claim.action == "replay":
            self.signer.issue(
                tool=tool, args_hash=ahash, decision="allow", outcome="replayed", **base
            )
            return claim.result
        if claim.action == "refuse":
            self.signer.issue(
                tool=tool, args_hash=ahash, decision="refuse", outcome=claim.status, **base
            )
            raise ToolError(
                f"AMW refused to re-run: an earlier identical {tool} call is {claim.status}; "
                "its outcome is unknown, so it will not run again automatically"
            )
        return await self._run(tool, arguments, ahash, base, key=key, claim=claim)

    async def _run(self, tool, arguments, ahash, base, *, key, claim=None):
        try:
            result = await self.upstream(tool, arguments)
        except UpstreamNotDispatched as exc:
            if key:
                self.ledger.release(key, claim)
            self.signer.issue(
                tool=tool, args_hash=ahash, decision="allow", outcome="not_dispatched", **base
            )
            raise ToolError(f"upstream unavailable, safe to retry: {exc}") from exc
        except UpstreamError as exc:
            if key:
                # The filesystem server writes in place, so an error result for a
                # write may come after a partial write: never claim it had no effect.
                self.ledger.finish(key, "failed_unknown")
                self.signer.issue(
                    tool=tool, args_hash=ahash, decision="allow", outcome="failed_unknown", **base
                )
                raise ToolError(
                    f"upstream error; the write may be partly applied and will not re-run: {exc}"
                ) from exc
            self.signer.issue(
                tool=tool, args_hash=ahash, decision="allow", outcome="failed", **base
            )
            raise ToolError(f"upstream error: {exc}") from exc
        except BaseException:
            if key:
                self.ledger.finish(key, "failed_unknown")
            self.signer.issue(
                tool=tool, args_hash=ahash, decision="allow", outcome="failed_unknown", **base
            )
            raise
        if key:
            self.ledger.finish(key, "completed", result)
        self.signer.issue(tool=tool, args_hash=ahash, decision="allow", outcome="completed", **base)
        if key and self.after_commit is not None:
            await self.after_commit(tool, key)
        return result


def build_server(gateway: Gateway) -> MCPServer:
    mcp = MCPServer("amw")

    @mcp.tool()
    async def read_text_file(path: str, ctx: Context) -> str:
        """Read a UTF-8 text file through AMW (permit-checked, receipted)."""
        return await gateway.handle("read_text_file", {"path": path}, ctx, write=False)

    @mcp.tool()
    async def write_file(path: str, content: str, ctx: Context) -> str:
        """Write a text file through AMW (permit-checked, idempotent, receipted)."""
        args = {"path": path, "content": content}
        return await gateway.handle("write_file", args, ctx, write=True)

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = build_server(Gateway(Config.from_env()))
    server.run(
        transport="streamable-http",
        host=os.environ.get("AMW_HOST", "127.0.0.1"),
        port=int(os.environ.get("AMW_PORT", "8791")),
    )


if __name__ == "__main__":
    main()
