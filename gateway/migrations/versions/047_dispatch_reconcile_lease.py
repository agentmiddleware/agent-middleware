"""Add provider-status reconciliation columns to governed MCP dispatch attempts.

Additive and nullable (the probe counter defaults to zero). Existing
``delivery_uncertain`` rows upgrade to an unresolved reconcile status, which
is exactly their meaning today: parked for manual review. No data update or
backfill is performed, and ``state`` is never touched.

Revision ID: 047_dispatch_reconcile_lease
Revises: 046_api_key_usage_events

Ported from the archived agent-middleware-api chain with the same revision
id so databases already stamped at 047-050 by that service stay on a known
revision. Here it follows this repo's 046. Columns and indexes are added only
when missing.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "047_dispatch_reconcile_lease"
down_revision: Union[str, None] = "046_api_key_usage_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    (
        "reconcile_status",
        lambda: sa.Column("reconcile_status", sa.String(length=32), nullable=True),
    ),
    (
        "reconcile_lease_owner",
        lambda: sa.Column("reconcile_lease_owner", sa.String(length=64), nullable=True),
    ),
    (
        "reconcile_lease_expires_at",
        lambda: sa.Column("reconcile_lease_expires_at", sa.DateTime(), nullable=True),
    ),
    (
        "reconcile_next_probe_at",
        lambda: sa.Column("reconcile_next_probe_at", sa.DateTime(), nullable=True),
    ),
    (
        "reconcile_probe_count",
        lambda: sa.Column(
            "reconcile_probe_count", sa.Integer(), nullable=False, server_default="0"
        ),
    ),
    ("reconciled_at", lambda: sa.Column("reconciled_at", sa.DateTime(), nullable=True)),
)
_INDEXES = (
    ("ix_mcp_dispatch_attempts_reconcile_status", "reconcile_status"),
    (
        "ix_mcp_dispatch_attempts_reconcile_lease_expires_at",
        "reconcile_lease_expires_at",
    ),
    ("ix_mcp_dispatch_attempts_reconcile_next_probe_at", "reconcile_next_probe_at"),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("mcp_dispatch_attempts")}
    indexes = {i["name"] for i in inspector.get_indexes("mcp_dispatch_attempts")}
    missing_columns = [make() for name, make in _COLUMNS if name not in columns]
    missing_indexes = [(n, c) for n, c in _INDEXES if n not in indexes]
    if not missing_columns and not missing_indexes:
        return
    with op.batch_alter_table("mcp_dispatch_attempts") as batch_op:
        for column in missing_columns:
            batch_op.add_column(column)
        for name, column in missing_indexes:
            batch_op.create_index(name, [column])


def downgrade() -> None:
    # Any non-NULL status is a durable provider decision. A refunded row's
    # refund and budget release outlive the column, and 046 accounting counts
    # every delivery_uncertain receipt as consumed, so a budget rebuild would
    # re-charge the released spend; a pending row may be half applied; and a
    # re-upgrade would re-probe every erased decision. Refuse rather than
    # silently drop any of them.
    bind = op.get_bind()
    decided = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM mcp_dispatch_attempts "
            "WHERE reconcile_status IS NOT NULL"
        )
    ).scalar_one()
    if decided:
        raise RuntimeError(
            f"047 downgrade refused: {decided} dispatch attempt(s) carry a "
            "reconcile_status decision that 046 cannot represent"
        )
    with op.batch_alter_table("mcp_dispatch_attempts") as batch_op:
        batch_op.drop_index("ix_mcp_dispatch_attempts_reconcile_next_probe_at")
        batch_op.drop_index("ix_mcp_dispatch_attempts_reconcile_lease_expires_at")
        batch_op.drop_index("ix_mcp_dispatch_attempts_reconcile_status")
        batch_op.drop_column("reconciled_at")
        batch_op.drop_column("reconcile_probe_count")
        batch_op.drop_column("reconcile_next_probe_at")
        batch_op.drop_column("reconcile_lease_expires_at")
        batch_op.drop_column("reconcile_lease_owner")
        batch_op.drop_column("reconcile_status")
