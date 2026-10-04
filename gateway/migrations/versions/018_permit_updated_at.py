"""Track last reservation activity on permits for budget reconciliation.

Adds `permits.updated_at` so a periodic sweep can distinguish a live in-flight
budget reservation from one orphaned by a crash mid-invocation.

Revision ID: 018_permit_updated_at
Revises: 017_audit_event_seq
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "018_permit_updated_at"
down_revision: Union[str, None] = "017_audit_event_seq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "permits",
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_permits_updated_at", "permits", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_permits_updated_at", table_name="permits")
    op.drop_column("permits", "updated_at")
