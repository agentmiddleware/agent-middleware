"""The deliberately unintegrated sample application.

One module, one agent, no protection. Kept tiny so that a reader can hold the
whole "before" picture in their head while judging the "after".
"""

from __future__ import annotations

from failure_lab.integration_check.sample_app.agent import (
    DEFAULT_TOOL_PATH,
    RefundIntent,
    UnprotectedRefundAgent,
)

__all__ = ["DEFAULT_TOOL_PATH", "RefundIntent", "UnprotectedRefundAgent"]
