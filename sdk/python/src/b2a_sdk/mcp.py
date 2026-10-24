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
        wallet_id: str,
        api_key: str,
        permit_id: str | None,
        idempotency_key: str | None,
    ) -> Any:
        mcp_context = {"wallet_id": wallet_id}
        if permit_id is not None:
            mcp_context["permit_id"] = permit_id
        if idempotency_key is not None:
            mcp_context["idempotency_key"] = idempotency_key
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{api_url}/mcp/tools/{service_id}/invoke",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={
                    "name": service_id,
                    "arguments": input_data,
                    "mcp_context": mcp_context,
                },
                timeout=30.0,
                follow_redirects=False,
            )
            if response.status_code in {400, 403, 202}:
                try:
                    detail = response.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                error = detail.get("error") if isinstance(detail, dict) else detail
                if response.status_code == 403 and error == "permit_required":
                    guidance = "Supply permit_id and idempotency_key as MCP tool arguments."
                    return CallToolResult(
                        content=[TextContent(type="text", text=f"permit_required: {guidance}")],
                        isError=True,
                        structuredContent={"error": "permit_required", "guidance": guidance},
                    )
                if response.status_code == 400 and error == "idempotency_key_required":
                    guidance = "Supply idempotency_key with permit_id as MCP tool arguments."
                    return CallToolResult(
                        content=[TextContent(type="text", text=f"{error}: {guidance}")],
                        isError=True,
                        structuredContent={"error": error, "guidance": guidance},
                    )
                if response.status_code == 202 and error == "human_approval_pending":
                    approval = detail.get("approval", {}) if isinstance(detail, dict) else {}
                    guidance = "Retry with the same permit_id and idempotency_key after approval."
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=f"{error}: {json.dumps(approval)}. {guidance}",
                            )
                        ],
                        isError=True,
                        structuredContent={
                            "error": error,
                            "approval": approval,
                            "guidance": guidance,
                        },
                    )
            response.raise_for_status()
            result = response.json()
            return result if legacy_result_format else CallToolResult.model_validate(result)

    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")

        async def create_handler(svc_id: str, svc_name: str):
            async def handler(
                permit_id: str | None = None,
                idempotency_key: str | None = None,
                **kwargs: dict[str, Any],
            ) -> Any:
                return await call_tool(
                    service_id=svc_id,
                    input_data=kwargs.pop("kwargs", kwargs),
                    wallet_id=os.getenv("B2A_WALLET_ID", ""),
                    api_key=os.getenv("B2A_API_KEY", ""),
                    permit_id=permit_id,
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
    print("  export B2A_WALLET_ID=<your-wallet>", file=output)
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
