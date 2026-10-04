"""
Minimal structured audit log (stdout via logging).

Use for MCP invocations and other security-sensitive actions. Downstream can
ship JSON logs to a collector without a full audit service yet.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("agent_middleware.audit")


def record_audit(event: str, **fields: Any) -> None:
    """Emit one JSON object per line on the audit logger (level INFO)."""
    payload: dict[str, Any] = {"event": event, **fields}
    logger.info("%s", json.dumps(payload, default=str))
