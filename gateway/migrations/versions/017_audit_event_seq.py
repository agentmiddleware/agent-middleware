"""Per-wallet monotonic sequence for control-plane audit events.

Adds a stable ordering key (`seq`) to the audit hash chain so equal or
clock-skewed `created_at` values cannot reorder the chain on verification.

Revision ID: 017_audit_event_seq
Revises: 016_trust_primitives
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "017_audit_event_seq"
down_revision: Union[str, None] = "016_trust_primitives"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "control_plane_audit_events",
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_control_plane_audit_events_seq",
        "control_plane_audit_events",
        ["seq"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_control_plane_audit_events_seq",
        table_name="control_plane_audit_events",
    )
    op.drop_column("control_plane_audit_events", "seq")
