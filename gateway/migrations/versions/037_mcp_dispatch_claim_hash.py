"""Add a nullable claim hash to governed MCP dispatch attempts.

The additive nullable upgrade preserves attempts created by earlier workers
and intentionally performs no data update or backfill.

Revision ID: 037_mcp_dispatch_claim_hash
Revises: 036_permit_request_hash_anchor
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "037_mcp_dispatch_claim_hash"
down_revision: Union[str, None] = "036_permit_request_hash_anchor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_dispatch_attempts") as batch_op:
        batch_op.add_column(
            sa.Column("dispatch_claim_hash", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    attempts = sa.table(
        "mcp_dispatch_attempts",
        sa.column("state", sa.String(length=32)),
    )
    # Older workers treat ``dispatched`` as an already-sent ambiguous attempt.
    # Preserve that fail-closed meaning before removing the claim evidence they
    # cannot understand; leaving ``dispatch_claimed`` would strand the row.
    op.execute(
        attempts.update()
        .where(attempts.c.state == "dispatch_claimed")
        .values(state="dispatched")
    )
    with op.batch_alter_table("mcp_dispatch_attempts") as batch_op:
        batch_op.drop_column("dispatch_claim_hash")
