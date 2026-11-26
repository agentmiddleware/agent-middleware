"""Lock the gateway ruff gate to the Python SDK rule set.

CI runs ``ruff check`` with ``gateway/ruff.toml``. Selecting only Pyflakes (F)
lets bug classes such as loop-variable closures (B023), ``zip`` without
``strict`` (B905), and late imports (E402) pass.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
_SDK_SELECT = {"E", "F", "W", "I", "N", "UP", "B", "C4"}
# Rules the lint audit called out by name. A per-code ignore would hide them.
_MUST_REPORT = {"B023", "B905", "E402"}


def test_ruff_selects_the_sdk_rule_set() -> None:
    config = tomllib.loads((_GATEWAY_ROOT / "ruff.toml").read_text(encoding="utf-8"))
    lint = config["lint"]
    select = set(lint["select"])
    ignore = set(lint.get("ignore", []))

    assert _SDK_SELECT <= select
    assert _MUST_REPORT.isdisjoint(ignore)
    assert "F" in select
