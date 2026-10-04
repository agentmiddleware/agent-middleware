"""Add raw_payload_hash to oracle crawl targets (Phase 1 durable crawl)."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "010_oracle_crawl_payload_hash"
down_revision: Union[str, None] = "009_security_auth_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "oracle_crawl_targets",
        sa.Column("raw_payload_hash", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("oracle_crawl_targets", "raw_payload_hash")
