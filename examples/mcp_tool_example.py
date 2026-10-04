"""
B2A MCP Tool Example
====================

Example demonstrating how to create an MCP-enabled tool using the B2A SDK.

This file shows:
1. Creating a simple billable tool with @mcp_tool
2. Registering it with the service registry
3. Invoking it via the MCP protocol

Usage:
    # Register tools (typically done at app startup)
    python examples/mcp_tool_example.py --register

    # List available tools
    python examples/mcp_tool_example.py --list

    # Generate tools.json
    python examples/mcp_tool_example.py --generate

    # Run standalone MCP server
    python examples/mcp_tool_example.py --serve
"""

import asyncio
import json
import os
import sys

# The SDK sources live under b2a_sdk/src; inserting the repo root instead makes
# "b2a_sdk" resolve to the bare directory and the import fails.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "b2a_sdk", "src"
    ),
)

from b2a_sdk import AgentMiddlewareClient
from b2a_sdk.decorators import mcp_tool, register_mcp_tool_callback
from b2a_sdk.mcp import generate_manifest, list_tools

CLIENT = None


def init_client():
    global CLIENT
    CLIENT = AgentMiddlewareClient(
        base_url=os.getenv("B2A_API_URL", "http://localhost:8000"),
        api_key=os.getenv("B2A_API_KEY", "test-key"),
    )
    return CLIENT


@mcp_tool(
    service_id="data-processor",
    name="Data Processor",
    description="Process and transform data according to specified rules",
    category="content_factory",
    credits_per_unit=5.0,
    unit_name="operation",
)
async def process_data(operation: str, data: str) -> dict:
    """
    Process data with the specified operation.

    Args:
        operation: The operation to perform (uppercase, lowercase, reverse)
        data: The input data to process

    Returns:
        dict with the operation result and metadata
    """
    if operation == "uppercase":
        result = data.upper()
    elif operation == "lowercase":
        result = data.lower()
    elif operation == "reverse":
        result = data[::-1]
    else:
        result = data

    return {
        "original": data,
        "operation": operation,
        "result": result,
        "credits_charged": 5.0,
    }


@mcp_tool(
    service_id="url-summarizer",
    name="URL Summarizer",
    description="Summarize content from a URL",
    category="oracle",
    credits_per_unit=10.0,
    unit_name="summary",
)
async def summarize_url(url: str, max_length: int = 200) -> dict:
    """
    Summarize content from a URL.

    Args:
        url: The URL to summarize
        max_length: Maximum summary length in characters

    Returns:
        dict with summary and metadata
    """
    return {
        "url": url,
        "summary": f"Summary of {url} (truncated to {max_length} chars)...",
        "max_length": max_length,
        "credits_charged": 10.0,
    }


@mcp_tool(
    service_id="image-generator",
    name="Image Generator",
    description="Generate images from text prompts",
    category="content_factory",
    credits_per_unit=25.0,
    unit_name="image",
)
async def generate_image(
    prompt: str, style: str = "default", size: str = "512x512"
) -> dict:
    """
    Generate an image from a text prompt.

    Args:
        prompt: Text description of the desired image
        style: Art style (default, realistic, abstract, cartoon)
        size: Output image size

    Returns:
        dict with image URL and metadata
    """
    return {
        "prompt": prompt,
        "style": style,
        "size": size,
        "image_url": f"https://placeholder.com/generated/{hash(prompt)}.png",
        "credits_charged": 25.0,
    }


def on_registration(service_id: str, func, input_schema, output_schema):
    """Callback when a tool is registered."""
    print(f"Registered tool: {service_id}")
    print(
        f"  Input schema: {json.dumps(input_schema, indent=2) if input_schema else 'None'}"
    )


async def register_tools():
    """Register all tools with the backend."""
    register_mcp_tool_callback(on_registration)

    print("Tools registered:")
    print(f"  - {process_data._b2a_mcp_metadata}")
    print(f"  - {summarize_url._b2a_mcp_metadata}")
    print(f"  - {generate_image._b2a_mcp_metadata}")

    print("\nTo invoke these tools:")
    print("  1. Fetch the MCP manifest:")
    print("     curl http://localhost:8000/mcp/tools.json")
    print("\n  2. Call a tool via MCP:")
    print("     curl -X POST http://localhost:8000/mcp/messages \\")
    print("       -H 'Content-Type: application/json' \\")
    print(
        '       -d \'{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "data-processor", "arguments": {"operation": "uppercase", "data": "hello"}}}\''
    )


async def main_list():
    """List available tools."""
    init_client()
    list_tools(
        api_url=os.getenv("B2A_API_URL", "http://localhost:8000"),
    )


async def main_generate():
    """Generate tools.json."""
    manifest = generate_manifest(
        output_path="tools.json",
        api_url=os.getenv("B2A_API_URL", "http://localhost:8000"),
    )
    print(f"Generated tools.json with {len(manifest.get('tools', []))} tools")


async def main_serve():
    """Run standalone MCP server."""
    from b2a_sdk.mcp import serve

    serve(
        transport="stdio",
        api_url=os.getenv("B2A_API_URL", "http://localhost:8000"),
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="B2A MCP Tool Example")
    parser.add_argument("--register", action="store_true", help="Register tools")
    parser.add_argument("--list", action="store_true", help="List available tools")
    parser.add_argument("--generate", action="store_true", help="Generate tools.json")
    parser.add_argument(
        "--serve", action="store_true", help="Run standalone MCP server"
    )
    args = parser.parse_args()

    if args.register:
        asyncio.run(register_tools())
    elif args.list:
        asyncio.run(main_list())
    elif args.generate:
        asyncio.run(main_generate())
    elif args.serve:
        asyncio.run(main_serve())
    else:
        parser.print_help()
        print("\nExample usage:")
        print("  python examples/mcp_tool_example.py --register  # Register tools")
        print("  python examples/mcp_tool_example.py --list     # List tools")
        print("  python examples/mcp_tool_example.py --generate # Generate tools.json")
        print("  python examples/mcp_tool_example.py --serve   # Run MCP server")
