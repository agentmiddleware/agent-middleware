"""Path-scoped permit rules shared by the proxy and the Goose PreToolUse hook."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def load_policy(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _bare(tool: str) -> str:
    # Goose prefixes extension tools as "<extension>__<tool>".
    return tool.split("__", 1)[1] if "__" in tool else tool


def check(policy: dict[str, Any], tool: str, arguments: dict[str, Any] | None) -> Decision:
    """Allow only when a rule grants ``tool`` on a path inside its folder."""
    name = _bare(tool)
    raw = (arguments or {}).get("path")
    if not isinstance(raw, str) or not raw:
        return Decision(False, f"no permit: {name} call has no path argument")
    target = Path(raw).expanduser().resolve()
    for rule in policy.get("rules", []):
        if rule.get("tool") != name:
            continue
        folder = Path(rule["folder"]).expanduser().resolve()
        if target.is_relative_to(folder):
            return Decision(True, f"permit {rule.get('id', name)} allows {name} in {folder}")
    return Decision(False, f"no permit for {name} on {target}: outside every permitted folder")
