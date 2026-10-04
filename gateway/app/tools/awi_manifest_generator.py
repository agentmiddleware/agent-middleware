"""
AWI Manifest Generator — Phase 8
=================================
CLI tool that generates /.well-known/awi.json from existing FastAPI/Django/Express apps.

Website owners can use this to expose their existing APIs as an AWI-compliant interface
without changing their human-facing UI.
"""

import argparse
import inspect
import json
from pathlib import Path
from typing import Any


def _path_matches(path: str, pattern: str) -> bool:
    """Match a route path against an action_map pattern.

    ``"/"`` matches only the root. Any other pattern must be the path's final
    segment(s), so ``/api/cart/add`` matches ``/cart/add`` while ``/format``
    does not match ``/form`` and ``/research`` does not match ``/search``.
    """
    if pattern == "/":
        return path == "/"
    return path.rstrip("/").endswith(pattern)


class ManifestGenerator:
    """Generate AWI manifest from Python/FastAPI applications."""

    def __init__(self, framework: str = "fastapi"):
        self.framework = framework
        self.actions: list[dict[str, Any]] = []
        self.route_mappings: dict[str, str] = {}

    def scan_fastapi_app(self, app) -> dict[str, Any]:
        """Scan a FastAPI application and extract routes for AWI mapping."""
        manifest: dict[str, Any] = {
            "name": getattr(app, "title", "Unknown API"),
            "version": getattr(app, "version", "1.0.0"),
            "awi_version": "1.0.0",
            "framework": "fastapi",
            "description": getattr(app, "description", ""),
            "base_url": "/",
            "actions": [],
            "representations": [
                {"type": "summary", "description": "Concise summary of page state"},
                {"type": "full_dom", "description": "Complete DOM structure"},
                {"type": "embedding", "description": "Semantic embedding of content"},
                {
                    "type": "low_res_screenshot",
                    "description": "Low-resolution screenshot",
                },
                {
                    "type": "accessibility_tree",
                    "description": "Accessibility tree representation",
                },
            ],
            "endpoints": [],
            "security": {
                "auth_required": True,
                "wallet_required": True,
                "kyc_required": True,
            },
        }

        for route in app.routes:
            if hasattr(route, "path") and hasattr(route, "methods"):
                endpoint = {
                    "path": route.path,
                    "methods": list(route.methods),
                    "name": getattr(route, "name", route.path),
                }

                if hasattr(route, "endpoint") and callable(route.endpoint):
                    doc = inspect.getdoc(route.endpoint)
                    if doc:
                        endpoint["description"] = doc.split("\n")[0]

                manifest["endpoints"].append(endpoint)

                action = self._route_to_action(route)
                if action:
                    self.actions.append(action)
                    self.route_mappings[action["awi_action"]] = route.path

        manifest["actions"] = self.actions
        manifest["route_mappings"] = self.route_mappings

        return manifest

    def _route_to_action(self, route) -> dict[str, Any] | None:
        """Convert a FastAPI route to an AWI action."""
        path = route.path
        methods = list(route.methods) if hasattr(route, "methods") else ["GET"]

        action_map = {
            ("POST", "/search"): ("search_and_sort", "Search for items"),
            ("POST", "/cart/add"): ("add_to_cart", "Add item to cart"),
            ("POST", "/checkout"): ("checkout", "Complete checkout"),
            ("POST", "/login"): ("login", "Authenticate user"),
            ("POST", "/logout"): ("logout", "End session"),
            ("POST", "/form"): ("fill_form", "Fill out form"),
            ("GET", "/"): ("navigate_to", "Navigate to page"),
        }

        for (method, pattern), (action, desc) in action_map.items():
            if method in methods and _path_matches(path, pattern):
                return {
                    "awi_action": action,
                    "route": path,
                    "method": method,
                    "description": desc,
                    "preconditions": [],
                    "postconditions": [],
                }

        if "POST" in methods:
            return {
                "awi_action": "custom_action",
                "route": path,
                "method": "POST",
                "description": f"Custom action at {path}",
                "preconditions": [],
                "postconditions": [],
            }

        return None

    def generate_from_openapi(self, openapi_spec: dict) -> dict[str, Any]:
        """Generate AWI manifest from OpenAPI spec."""
        manifest = {
            "name": openapi_spec.get("info", {}).get("title", "API"),
            "version": openapi_spec.get("info", {}).get("version", "1.0.0"),
            "awi_version": "1.0.0",
            "framework": "openapi",
            "description": openapi_spec.get("info", {}).get("description", ""),
            "base_url": openapi_spec.get("servers", [{}])[0].get("url", "/"),
            "actions": [],
            "representations": [
                {"type": "summary", "description": "Concise summary"},
                {"type": "full_dom", "description": "Complete DOM"},
                {"type": "embedding", "description": "Semantic embedding"},
            ],
            "security": {
                "auth_required": True,
                "wallet_required": True,
            },
        }

        paths = openapi_spec.get("paths", {})
        for path, methods in paths.items():
            for method, spec in methods.items():
                if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                    endpoint = {
                        "path": path,
                        "methods": [method.upper()],
                        "description": spec.get("summary", spec.get("description", "")),
                    }
                    manifest.setdefault("endpoints", []).append(endpoint)

        return manifest

    def save_manifest(self, manifest: dict, output_path: Path):
        """Save manifest to file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as f:
            json.dump(manifest, f, indent=2)

        print(f"AWI manifest generated: {output_path}")
        print(f"  - {len(manifest.get('actions', []))} actions mapped")
        print(f"  - {len(manifest.get('endpoints', []))} endpoints")

        return manifest


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Generate AWI manifest from existing applications"
    )
    parser.add_argument(
        "--framework",
        choices=["fastapi", "django", "express", "openapi"],
        default="fastapi",
        help="Framework to scan",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path(".well-known/awi.json"),
        help="Output path for manifest",
    )
    parser.add_argument(
        "--app-module",
        help="Python module containing the FastAPI app (e.g., main:app)",
    )
    parser.add_argument(
        "--openapi-spec",
        type=Path,
        help="Path to OpenAPI spec JSON file",
    )

    args = parser.parse_args()

    generator = ManifestGenerator(framework=args.framework)

    if args.openapi_spec:
        with open(args.openapi_spec) as f:
            spec = json.load(f)
        manifest = generator.generate_from_openapi(spec)
    elif args.app_module:
        import importlib

        module_name, app_name = args.app_module.split(":")
        module = importlib.import_module(module_name)
        app = getattr(module, app_name)
        manifest = generator.scan_fastapi_app(app)
    else:
        manifest = {
            "name": "My API",
            "version": "1.0.0",
            "awi_version": "1.0.0",
            "framework": args.framework,
            "actions": [],
            "representations": [
                {"type": "summary"},
                {"type": "embedding"},
            ],
        }

    generator.save_manifest(manifest, args.output)


if __name__ == "__main__":
    main()
