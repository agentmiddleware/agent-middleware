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
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None


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
        url = f"{url}?category={category}"

    if httpx is None:
        raise RuntimeError("httpx required. Install: pip install httpx")

    response = httpx.get(url, timeout=30.0)
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

    print(f"\\nAvailable MCP Tools ({len(tools)})")
    print("=" * 60)

    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "No description")
        credits = tool.get("annotations", {}).get("creditsPerCall", 1.0)
        unit = tool.get("annotations", {}).get("unitName", "call")
        cat = tool.get("annotations", {}).get("category", "custom")

        print(f"\\n  {name}")
        print(f"    Description: {desc[:60]}...")
        print(f"    Cost: {credits} credits/{unit}")
        print(f"    Category: {cat}")

    print("\\n" + "=" * 60)


async def serve_async(
    transport: str = "stdio",
    port: int = 8001,
    api_url: str = DEFAULT_API_URL,
) -> None:
    """
    Serve as an MCP server using the specified transport.

    Args:
        transport: "stdio" or "sse"
        port: Port for SSE transport
        api_url: B2A API URL
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print('Error: mcp package required. Install: pip install "b2a-sdk[mcp]"')
        return

    manifest = generate_manifest(api_url=api_url)
    tools = manifest.get("tools", [])

    mcp = FastMCP("B2A Marketplace")

    async def call_tool(service_id: str, input_data: dict, wallet_id: str, api_key: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{api_url}/v1/billing/services/{service_id}/invoke",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"caller_wallet_id": wallet_id, "input_data": input_data},
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()

    for tool in tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")

        async def create_handler(svc_id: str, svc_name: str):
            async def handler(**kwargs) -> dict:
                return await call_tool(
                    service_id=svc_id,
                    input_data={"input_data": kwargs},
                    wallet_id=os.getenv("B2A_WALLET_ID", ""),
                    api_key=os.getenv("B2A_API_KEY", ""),
                )

            return handler

        mcp.add_tool(name, desc, await create_handler(name, desc))

    print(f"Starting MCP server with {len(tools)} tools...")
    print(f"Transport: {transport}")
    if transport == "sse":
        print(f"Port: {port}")
    print("\\nConfigure with:")
    print("  export B2A_API_KEY=<your-key>")
    print("  export B2A_WALLET_ID=<your-wallet>")
    print(f"  export B2A_API_URL={api_url}")
    print()

    mcp.run(transport=transport, port=port)


def serve(
    transport: str = "stdio",
    port: int = 8001,
    api_url: str = DEFAULT_API_URL,
) -> None:
    """Sync wrapper for serve_async."""
    asyncio.run(serve_async(transport=transport, port=port, api_url=api_url))


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
        serve(transport=args.transport, port=args.port, api_url=args.api_url)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
