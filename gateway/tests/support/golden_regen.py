"""Review gate for golden-file regeneration.

The permit and receipt signing snapshots pin the exact bytes signatures
cover. Rewriting the expected output is sometimes legitimate (an
intentional format change with a schema version bump and a CHANGELOG
entry), but it must never be the easy way to make a refactor PR green.
Requiring a second, explicit acknowledgment next to the ``UPDATE_*`` flag
keeps regeneration a conscious, review-visible act: CI never sets the ack
variable, so a PR that rewrites goldens without review discussion fails.
"""

from __future__ import annotations

import os

import pytest

#: Second flag required next to UPDATE_PERMIT_GOLDEN / UPDATE_RECEIPT_GOLDEN.
REGEN_ACK_ENV_VAR = "GOLDEN_REGEN_ACK"


def require_golden_regen_ack(update_env_var: str) -> bool:
    """Return True when the caller should regenerate its golden files.

    Returns False when regeneration was not requested. Fails the test when
    regeneration was requested without the review acknowledgment, before
    the caller writes anything.
    """
    if os.environ.get(update_env_var) != "1":
        return False
    if os.environ.get(REGEN_ACK_ENV_VAR) != "1":
        pytest.fail(
            f"{update_env_var}=1 regenerates golden snapshots, which "
            f"requires {REGEN_ACK_ENV_VAR}=1 as review acknowledgment. "
            f"Regeneration is only for intentional format changes, with a "
            f"schema version bump and a CHANGELOG entry (see the test "
            f"module docstring). Never regenerate to make a refactor green."
        )
    return True
