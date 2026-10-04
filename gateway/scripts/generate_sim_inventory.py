#!/usr/bin/env python3
"""
Regenerate simulation + MCP honesty inventory (Phase 0).

Writes:
  - docs/simulations-inventory.md
  - docs/sim-inventory.json

Do not edit the markdown by hand. CI runs with --check to prevent drift.

Usage:
  python scripts/generate_sim_inventory.py
  python scripts/generate_sim_inventory.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _inventory_payload() -> dict:
    from app.core.config import Settings
    from app.core.runtime_mode import (
        SERVICE_NAMES,
        get_simulation_modes,
        simulation_settings_field,
    )
    from app.services.mcp_generator import get_mcp_generator
    from app.services.mcp_phase9_tools import ensure_phase9_registered

    sim_modes = get_simulation_modes()
    rows = []
    for svc in sorted(SERVICE_NAMES):
        setting = simulation_settings_field(svc)
        field = Settings.model_fields.get(setting)
        default = field.default if field is not None else None
        rows.append(
            {
                "runtime_service": svc,
                "settings_attribute": setting,
                "default_simulated": bool(default) if default is not None else None,
                "current_simulated": sim_modes.get(svc),
            }
        )

    ensure_phase9_registered()
    gen = get_mcp_generator()

    async def _tools() -> list[dict]:
        return (await gen.generate_tools_json_async(include_persistent=False))["tools"]

    tools = asyncio.run(_tools())
    tool_rows = []
    for t in tools:
        ann = t.get("annotations") or {}
        tool_rows.append(
            {
                "name": t.get("name"),
                "simulation": ann.get("simulation"),
                "integration_status": ann.get("integrationStatus"),
                "runtime_service": ann.get("runtimeService"),
                "category": ann.get("category"),
            }
        )
    tool_rows.sort(key=lambda x: x["name"] or "")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "simulation_modes": sim_modes,
        "services": rows,
        "mcp_tools_local": tool_rows,
        "mcp_tool_count": len(tool_rows),
    }


def _render_markdown(payload: dict) -> str:
    lines = [
        "# Simulation & MCP honesty inventory",
        "",
        "**Auto-generated.** Do not edit by hand. Regenerate with:",
        "",
        "```bash",
        "python scripts/generate_sim_inventory.py",
        "```",
        "",
        f"- **Local MCP tools:** {payload['mcp_tool_count']}",
        "",
        "_Canonical timestamp and machine-readable snapshot: `docs/sim-inventory.json`._",
        "",
        "## Runtime pillars (`SIMULATION_MODE_*`)",
        "",
        "| Runtime service | Settings field | Current simulated |",
        "|-----------------|----------------|-------------------|",
    ]
    for row in payload["services"]:
        cur = row["current_simulated"]
        lines.append(
            f"| `{row['runtime_service']}` | `{row['settings_attribute']}` | `{cur}` |"
        )
    lines.extend(
        [
            "",
            "## MCP tools (local registry only)",
            "",
            "| Tool | simulation | integrationStatus | runtimeService | category |",
            "|------|------------|-------------------|----------------|----------|",
        ]
    )
    for tr in payload["mcp_tools_local"]:
        lines.append(
            f"| `{tr['name']}` | `{tr['simulation']}` | `{tr['integration_status']}` | "
            f"`{tr['runtime_service']}` | `{tr['category']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _write(payload: dict, md_path: Path, json_path: Path) -> None:
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if files differ from freshly generated output.",
    )
    args = ap.parse_args()

    md_path = _REPO_ROOT / "docs" / "simulations-inventory.md"
    json_path = _REPO_ROOT / "docs" / "sim-inventory.json"

    payload = _inventory_payload()

    if args.check:
        if not md_path.is_file() or not json_path.is_file():
            print("Missing inventory files; run without --check", file=sys.stderr)
            return 1
        existing_json = json.loads(json_path.read_text(encoding="utf-8"))
        gen_json = json.loads(json.dumps(payload, sort_keys=True))
        exist_stripped = {k: v for k, v in existing_json.items() if k != "generated_at"}
        new_stripped = {k: v for k, v in gen_json.items() if k != "generated_at"}
        if exist_stripped != new_stripped:
            print(
                "docs/sim-inventory.json is stale. Run: "
                "python scripts/generate_sim_inventory.py",
                file=sys.stderr,
            )
            return 1
        if md_path.read_text(encoding="utf-8") != _render_markdown(payload):
            print(
                "docs/simulations-inventory.md is stale. Run: "
                "python scripts/generate_sim_inventory.py",
                file=sys.stderr,
            )
            return 1
        print("Simulation inventory is up to date.")
        return 0

    _write(payload, md_path, json_path)
    print(f"Wrote {md_path} and {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
