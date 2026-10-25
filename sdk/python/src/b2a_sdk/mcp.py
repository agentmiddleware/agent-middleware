"""
Legacy B2A MCP CLI & Helpers
=====================

Legacy discovery and serving helpers. Standalone generation is retired.
Use the governed POST /mcp/messages flow documented in docs/quickstart.md.

Usage:
    python -m b2a_sdk.mcp generate --output tools.json
    python -m b2a_sdk.mcp serve --transport stdio
    python -m b2a_sdk.mcp list
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

DEFAULT_API_URL = os.getenv("B2A_API_URL", "http://localhost:8000")
_ASCII_PRINTABLE_MIN = 32
_ASCII_PRINTABLE_MAX = 126


def _printable_ascii_idempotency_key(idempotency_key: str) -> str:
    """Return the caller's key unchanged, or raise before anything is sent.

    The gateway compares ``Idempotency-Key`` verbatim and never trims it
    (``validate_client_idempotency_key`` in gateway/app/services/idempotency.py),
    while HTTP servers drop leading and trailing header whitespace on the wire.
    Silently trimming here, or letting the wire trim, would let ``" k"`` and
    ``"k"`` alias to one key. So this bridge rejects edge whitespace instead
    of trimming it, unlike ``AgentMiddlewareClient``, which trims. httpx
    encodes header values as ASCII, so anything outside printable ASCII is
    rejected too rather than failing with ``UnicodeEncodeError``.
    """
    if not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key must be a string")
    if not idempotency_key.strip():
        raise ValueError("idempotency_key must not be blank")
    if idempotency_key != idempotency_key.strip():
        raise ValueError("idempotency_key must not have leading or trailing whitespace")
    if any(
        ord(char) < _ASCII_PRINTABLE_MIN or ord(char) > _ASCII_PRINTABLE_MAX
        for char in idempotency_key
    ):
        raise ValueError("idempotency_key must be printable ASCII")
    return idempotency_key


def _gateway_server_error(response: httpx.Response, keyed: bool) -> dict[str, Any]:
    """Structured content for a gateway HTTP 5xx.

    FastMCP turns a raised exception into a bare ``ToolError`` string, which
    drops the gateway's error code and correlation id. Returning them as data
    keeps both. ``retryable`` is true only when the caller supplied an
    idempotency key: an unkeyed call gets a fresh server-minted key on the
    gateway, so retrying it is a new call that can execute and charge again.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    sources = [body]
    error = body.get("error")
    if isinstance(error, dict):
        sources = [error.get("data") if isinstance(error.get("data"), dict) else {}, error, body]

    def first_str(field: str) -> str | None:
        for source in sources:
            value = source.get(field)
            if isinstance(value, str) and value:
                return value
        return None

    code = first_str("code") or "internal_error"
    correlation_id = (
        first_str("correlation_id")
        or response.headers.get("X-Correlation-ID")
        or response.headers.get("X-Request-ID")
    )
    structured: dict[str, Any] = {
        "error": code,
        "code": code,
        "message": first_str("message") or f"POST /mcp failed: HTTP {response.status_code}",
        "correlation_id": correlation_id,
        "http_status": response.status_code,
        "retryable": keyed,
    }
    if not keyed:
        structured["hint"] = "Supply an idempotency_key to retry safely."
    return structured


def generate_manifest(
    output_path: str | None = None,
    api_url: str = DEFAULT_API_URL,
    category: str | None = None,
) -> dict[str, Any]:
    """
    Fetch the MCP tools.json manifest from the API.

    Args:
        output_path: Optional path to save the manifest
        api_url: B2A API URL
        category: Optional category filter

    Returns:
        The tools.json manifest
    """
    url = f"{api_url}/mcp/tools.json"
    if category:
        url = f"{url}?{urlencode({'category': category})}"

    response = httpx.get(url, timeout=30.0, follow_redirects=False)
    response.raise_for_status()
    manifest = response.json()

    if output_path:
        with open(output_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Manifest saved to: {output_path}")

    return manifest


def generate_standalone_server(
    output_path: str,
    title: str = "B2A MCP Server",
    description: str = "Custom MCP server from B2A marketplace",
    services: list[dict] | None = None,
    api_url: str = DEFAULT_API_URL,
) -> None:
    """Refuse retired ungoverned code generation without discovery or file writes.

    The callable and CLI arguments remain accepted so existing callers receive
    an explicit support-boundary error. This helper never implemented the
    permit/receipt authorization loop.
    """
    raise RuntimeError(
        "Standalone MCP generation is retired; use the governed "
        "POST /mcp/messages flow in docs/quickstart.md instead."
    )


def list_tools(
    api_url: str = DEFAULT_API_URL,
    category: str | None = None,
) -> None:
    """List all available MCP tools."""
    manifest = generate_manifest(api_url=api_url, category=category)
    tools = manifest.get("tools", [])

    print()
    print(f"Available MCP Tools ({len(tools)})")
    print("=" * 60)

    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "No description")
        credits = tool.get("annotations", {}).get("creditsPerCall", 1.0)
        unit = tool.get("annotations", {}).get("unitName", "call")
        cat = tool.get("annotations", {}).get("category", "custom")

        print()
        print(f"  {name}")
        print(f"    Description: {desc[:60]}...")
        print(f"    Cost: {credits} credits/{unit}")
        print(f"    Category: {cat}")

    print()
    print("=" * 60)


async def serve_async(
    transport: Literal["stdio", "sse"] = "stdio",
    port: int = 8001,
    api_url: str = DEFAULT_API_URL,
    legacy_result_format: bool = False,
) -> None:
    """
    Serve as an MCP server using the specified transport.

    Args:
        transport: "stdio" or "sse"
        port: Port for SSE transport
        api_url: B2A API URL
        legacy_result_format: Wrap successful gateway responses as JSON text
    """
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import CallToolResult, TextContent
    except ImportError:
        print('Error: mcp package required. Install: pip install "amw-sdk[mcp]"')
        return

    manifest = generate_manifest(api_url=api_url)
    tools = manifest.get("tools", [])

    mcp = FastMCP("B2A Marketplace", port=port)

    async def call_tool(
        service_id: str,
        input_data: dict,
        api_key: str,
        idempotency_key: str | None,
    ) -> Any:
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _printable_ascii_idempotency_key(idempotency_key)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{api_url}/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": service_id, "arguments": input_data},
                },
                timeout=30.0,
                follow_redirects=False,
            )
            if 400 <= response.status_code < 500:
                try:
                    body = response.json()
                except ValueError:
                    body = {"detail": response.text}
                detail = body.get("detail") if isinstance(body, dict) else body
                error = detail.get("error") if isinstance(detail, dict) else detail
                structured = dict(body) if isinstance(body, dict) else {"detail": body}
                if isinstance(error, str):
                    structured["error"] = error
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(structured))],
                    isError=True,
                    structuredContent=structured,
                )
            if response.status_code >= 500:
                structured = _gateway_server_error(response, keyed=idempotency_key is not None)
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(structured))],
                    isError=True,
                    structuredContent=structured,
                )
            response.raise_for_status()
            envelope = response.json()
            if "error" in envelope:
                error = envelope["error"]
                structured = error if isinstance(error, dict) else {"error": error}
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(structured))],
                    isError=True,
                    structuredContent=structured,
                )
            result = envelope["result"]
            return result if legacy_result_format else CallToolResult.model_validate(result)

    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")

        async def create_handler(svc_id: str, svc_name: str):
            async def handler(
                idempotency_key: str | None = None,
                **kwargs: dict[str, Any],
            ) -> Any:
                return await call_tool(
                    service_id=svc_id,
                    input_data=kwargs.pop("kwargs", kwargs),
                    api_key=os.getenv("B2A_API_KEY", ""),
                    idempotency_key=idempotency_key,
                )

            return handler

        mcp.add_tool(await create_handler(name, desc), name=name, description=desc)

    output = sys.stderr if transport == "stdio" else sys.stdout
    print(f"Starting MCP server with {len(tools)} tools...", file=output)
    print(f"Transport: {transport}", file=output)
    if transport == "sse":
        print(f"Port: {port}", file=output)
    print(file=output)
    print("Configure with:", file=output)
    print("  export B2A_API_KEY=<your-key>", file=output)
    print(f"  export B2A_API_URL={api_url}", file=output)
    print(file=output)

    if transport == "stdio":
        await mcp.run_stdio_async()
    elif transport == "sse":
        await mcp.run_sse_async()
    else:
        raise ValueError(f"Unknown transport: {transport}")


def serve(
    transport: Literal["stdio", "sse"] = "stdio",
    port: int = 8001,
    api_url: str = DEFAULT_API_URL,
    legacy_result_format: bool = False,
) -> None:
    """Sync wrapper for serve_async."""
    asyncio.run(
        serve_async(
            transport=transport,
            port=port,
            api_url=api_url,
            legacy_result_format=legacy_result_format,
        )
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="B2A MCP CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    generate_parser = subparsers.add_parser("generate", help="Generate tools.json manifest")
    generate_parser.add_argument("--output", "-o", help="Output file path")
    generate_parser.add_argument("--api-url", default=DEFAULT_API_URL, help="B2A API URL")
    generate_parser.add_argument("--category", help="Category filter")

    standalone_parser = subparsers.add_parser(
        "standalone", help="Retired: use the governed MCP gateway"
    )
    standalone_parser.add_argument("--output", "-o", required=True, help="Output script path")
    standalone_parser.add_argument("--title", default="B2A MCP Server", help="Server title")
    standalone_parser.add_argument("--api-url", default=DEFAULT_API_URL, help="B2A API URL")

    list_parser = subparsers.add_parser("list", help="List available MCP tools")
    list_parser.add_argument("--api-url", default=DEFAULT_API_URL, help="B2A API URL")
    list_parser.add_argument("--category", help="Category filter")

    serve_parser = subparsers.add_parser("serve", help="Start MCP server")
    serve_parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse"], help="Transport"
    )
    serve_parser.add_argument("--port", type=int, default=8001, help="Port for SSE")
    serve_parser.add_argument("--api-url", default=DEFAULT_API_URL, help="B2A API URL")
    serve_parser.add_argument(
        "--legacy-result-format",
        action="store_true",
        help="Wrap successful tool results as JSON text for older MCP clients",
    )

    args = parser.parse_args()

    if args.command == "generate":
        manifest = generate_manifest(
            output_path=args.output,
            api_url=args.api_url,
            category=args.category,
        )
        print(f"Generated manifest with {len(manifest.get('tools', []))} tools")

    elif args.command == "standalone":
        try:
            generate_standalone_server(
                output_path=args.output,
                title=args.title,
                api_url=args.api_url,
            )
        except RuntimeError as exc:
            parser.error(str(exc))

    elif args.command == "list":
        list_tools(api_url=args.api_url, category=args.category)

    elif args.command == "serve":
        serve(
            transport=args.transport,
            port=args.port,
            api_url=args.api_url,
            legacy_result_format=args.legacy_result_format,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
